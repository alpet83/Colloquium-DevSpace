#!/usr/bin/env python3
"""Semi-autonomous maintenance loop for active projects.

Single process cycle:
- choose active projects (sessions.active_project + recent context activity),
- run find snapshot,
- compare with attached_files links,
- optionally mutate DB (degrade/recover/add links; purge @-links when missing_ttl exhausted),
- optionally trigger lazy project scan with cooldown.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

_THIS = Path(__file__).resolve()
_AGENT_ROOT = _THIS.parents[1]
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))

import globals
from lib.file_link_prefix import sql_link_prefixed_params, strip_storage_prefix
from managers.db import Database
from managers.files import FileManager
from managers.project import ProjectManager
from managers.runtime_config import get_bool, get_float, get_int

log = globals.get_logger("core_maint")

# После self-test: фактический режим (inotify может быть понижен до active).
_effective_maint_mode: str | None = None
# В режиме inotify после частичного провала self-test: абсолютные пути для периодического find+reconcile.
_maint_failed_subtrees: list[tuple[int, str, Path]] = []
# Дамп inotify (CORE_MAINT_INOTIFY_DUMP): накопление уникальных строк за окно.
_inotify_dump_bucket: set[str] = set()
_inotify_dump_rc_counts: dict[int, int] = {}
_inotify_dump_window_start: float = 0.0
# Последнее событие inotify по heartbeat-файлу MCP (пишет cqds_mcp_mini на хосте, см. cq_runtime_host_heartbeat).
_mcp_heartbeat_last_mono: float = 0.0
_maint_process_start_mono: float = 0.0

# Имя синхронизировано с фоновой задачей MCP; не участвует в селфтесте `.cqds_maint_probe_*`.
MCP_HEARTBEAT_FILENAME = ".cqds_mcp_active.pid"


def _now_ts() -> int:
    return int(dt.datetime.now(dt.timezone.utc).timestamp())


def _active_hours() -> int:
    return get_int("CORE_MAINT_ACTIVE_HOURS", 6, 1, 168)


def _max_projects() -> int:
    return get_int("CORE_MAINT_MAX_PROJECTS_PER_TICK", 3, 1, 100)


def _tick_sleep() -> float:
    return get_float("CORE_MAINT_INTERVAL_SEC", 20.0, 2.0, 3600.0)


def _proj_budget_sec() -> float:
    return get_float("CORE_MAINT_PROJECT_BUDGET_SEC", 12.0, 1.0, 300.0)


def _find_timeout_sec() -> float:
    return get_float("CORE_MAINT_FIND_TIMEOUT_SEC", 10.0, 1.0, 120.0)


def _find_slow_log_threshold_sec() -> float:
    """Логировать длительность вызова find, если subprocess длился не меньше этого порога."""
    return get_float("CORE_MAINT_FIND_SLOW_LOG_SEC", 1.0, 0.1, 300.0)


def _scan_cooldown_sec() -> float:
    return get_float("CORE_MAINT_SCAN_COOLDOWN_SEC", 300.0, 10.0, 86_400.0)


def _maint_enabled() -> bool:
    return get_bool("CORE_MAINT_ENABLED", default=False)


def _mutate_enabled() -> bool:
    return get_bool("CORE_MAINT_MUTATE", default=False)


def _purge_stale_links_enabled() -> bool:
    """При mutate: удалять строку attached_files, когда следующий missing_ttl был бы 0."""
    return get_bool("CORE_MAINT_PURGE_STALE_LINKS", default=True)


def _scan_enabled() -> bool:
    return get_bool("CORE_MAINT_SCAN_ENABLED", default=True)


def _maint_mode() -> str:
    raw = (os.getenv("CORE_MAINT_MODE", "") or "").strip().lower()
    if raw in ("active", "inotify"):
        return raw
    return "inotify"


def _selftest_enabled() -> bool:
    return get_bool("CORE_MAINT_INOTIFY_SELFTEST", default=True)


def _selftest_max_projects() -> int:
    return get_int("CORE_MAINT_SELFTEST_MAX_PROJECTS", 64, 1, 500)


def _inotify_timeout_sec() -> float:
    return get_float("CORE_MAINT_INOTIFY_TIMEOUT_SEC", 20.0, 1.0, 3600.0)


def _inotify_force_active_sec() -> float:
    return get_float("CORE_MAINT_INOTIFY_FORCE_ACTIVE_SEC", 300.0, 5.0, 86_400.0)


def _poll_failed_subtrees_sec() -> float:
    """Периодический poll только для путей, проваливших inotify self-test (гибрид)."""
    return get_float("CORE_MAINT_POLL_FAILED_SEC", 60.0, 5.0, 86_400.0)


def _mcp_heartbeat_tune_enabled() -> bool:
    """Сужать интервалы active/poll, если давно не было inotify по ``.cqds_mcp_active.pid``."""
    return get_bool("CORE_MAINT_MCP_HEARTBEAT_TUNE", default=True)


def _mcp_heartbeat_stale_sec() -> float:
    return get_float("CORE_MAINT_MCP_HEARTBEAT_STALE_SEC", 180.0, 30.0, 7200.0)


def _mcp_heartbeat_grace_sec() -> float:
    """После старта процесса не считать heartbeat «просроченным» (MCP ещё не успел писать)."""
    return get_float("CORE_MAINT_MCP_HEARTBEAT_GRACE_SEC", 120.0, 0.0, 3600.0)


def _inotify_force_degraded_sec() -> float:
    return get_float("CORE_MAINT_INOTIFY_FORCE_DEGRADED_SEC", 45.0, 5.0, 600.0)


def _poll_failed_degraded_sec() -> float:
    return get_float("CORE_MAINT_POLL_FAILED_DEGRADED_SEC", 25.0, 5.0, 300.0)


def _inotify_dump_enabled() -> bool:
    """Полный вывод stdout/stderr каждого inotifywait в основном цикле (отладка)."""
    return get_bool("CORE_MAINT_INOTIFY_DUMP", default=False)


def _inotify_dump_window_sec() -> float:
    return get_float("CORE_MAINT_INOTIFY_DUMP_WINDOW_SEC", 60.0, 5.0, 600.0)


def _log_inotify_wait_dump(proc: subprocess.CompletedProcess) -> None:
    global _inotify_dump_bucket, _inotify_dump_rc_counts, _inotify_dump_window_start
    if not _inotify_dump_enabled():
        return
    now = time.monotonic()
    if _inotify_dump_window_start <= 0.0:
        _inotify_dump_window_start = now
    rc = int(proc.returncode)
    _inotify_dump_rc_counts[rc] = _inotify_dump_rc_counts.get(rc, 0) + 1
    for line in (proc.stdout or "").splitlines():
        s = line.strip()
        if s:
            _inotify_dump_bucket.add(s)
    for line in (proc.stderr or "").splitlines():
        s = line.strip()
        if s:
            _inotify_dump_bucket.add("stderr: " + s)
    win = _inotify_dump_window_sec()
    if now - _inotify_dump_window_start < win:
        return
    if _inotify_dump_bucket or _inotify_dump_rc_counts:
        log.info(
            "CORE_MAINT inotify_dump_%ds rc_hist=%s unique_lines=%d",
            int(win),
            dict(sorted(_inotify_dump_rc_counts.items())),
            len(_inotify_dump_bucket),
        )
        for ln in sorted(_inotify_dump_bucket):
            log.info("CORE_MAINT inotify_dump: %s", ln)
    _inotify_dump_bucket = set()
    _inotify_dump_rc_counts = {}
    _inotify_dump_window_start = now


def _inotify_heartbeat_stale() -> bool:
    global _mcp_heartbeat_last_mono, _maint_process_start_mono
    if not _mcp_heartbeat_tune_enabled():
        return False
    now = time.monotonic()
    if _maint_process_start_mono <= 0.0:
        return False
    if (now - _maint_process_start_mono) < _mcp_heartbeat_grace_sec():
        return False
    if _mcp_heartbeat_last_mono <= 0.0:
        return True
    return (now - _mcp_heartbeat_last_mono) >= _mcp_heartbeat_stale_sec()


def _effective_inotify_force_active_sec() -> float:
    base = _inotify_force_active_sec()
    if _inotify_heartbeat_stale():
        return min(base, _inotify_force_degraded_sec())
    return base


def _effective_poll_failed_subtrees_sec() -> float:
    base = _poll_failed_subtrees_sec()
    if _inotify_heartbeat_stale():
        return min(base, _poll_failed_degraded_sec())
    return base


def _rel_excluded_from_maint_snapshot(rel: str) -> bool:
    if rel.startswith("backups/"):
        return True
    if rel == MCP_HEARTBEAT_FILENAME or rel.endswith("/" + MCP_HEARTBEAT_FILENAME):
        return True
    return False


def _normalize_rel(path: Path, root: Path) -> str | None:
    try:
        rel = path.relative_to(root)
    except Exception:
        return None
    s = str(rel).replace("\\", "/").lstrip("/")
    return s or None


def _project_dir(project_name: str) -> Path:
    return Path("/app/projects") / str(project_name)


def _subprocess_find_run(
    cmd: list[str],
    timeout_sec: float,
    *,
    scope: str,
    path_for_log: str,
) -> subprocess.CompletedProcess:
    t0 = time.monotonic()
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=max(1.0, timeout_sec),
        check=False,
    )
    elapsed = time.monotonic() - t0
    if elapsed >= _find_slow_log_threshold_sec():
        log.info(
            "CORE_MAINT find_slow scope=%s path=%s elapsed_sec=%.3f find_rc=%d",
            scope,
            path_for_log,
            elapsed,
            int(proc.returncode),
        )
    return proc


def _find_files_snapshot(project_root: Path, timeout_sec: float) -> tuple[set[str], bool]:
    if not project_root.exists():
        return set(), False
    cmd = ["find", str(project_root), "-type", "f"]
    proc = _subprocess_find_run(
        cmd,
        timeout_sec,
        scope="project_root",
        path_for_log=str(project_root),
    )
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()[:400]
        raise RuntimeError(f"find failed rc={proc.returncode}: {stderr}")
    rows = set()
    for line in (proc.stdout or "").splitlines():
        p = Path(line.strip())
        rel = _normalize_rel(p, project_root)
        if not rel:
            continue
        if _rel_excluded_from_maint_snapshot(rel):
            continue
        rows.add(rel)
    return rows, True


def _find_files_snapshot_under(project_root: Path, subtree: Path, timeout_sec: float) -> tuple[set[str], bool]:
    """Snapshot файлов только под subtree; пути относительно project_root."""
    if not project_root.exists():
        return set(), False
    if not subtree.exists() or not subtree.is_dir():
        return set(), False
    try:
        subtree.resolve().relative_to(project_root.resolve())
    except Exception:
        raise RuntimeError(f"subtree not under project_root: {subtree} vs {project_root}")
    cmd = ["find", str(subtree), "-type", "f"]
    proc = _subprocess_find_run(
        cmd,
        timeout_sec,
        scope="subtree",
        path_for_log=str(subtree),
    )
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()[:400]
        raise RuntimeError(f"find failed rc={proc.returncode}: {stderr}")
    rows: set[str] = set()
    for line in (proc.stdout or "").splitlines():
        p = Path(line.strip())
        rel = _normalize_rel(p, project_root)
        if not rel:
            continue
        if _rel_excluded_from_maint_snapshot(rel):
            continue
        rows.add(rel)
    return rows, True


# Отладочный лог по событию на test.tmp (не путать с селфтест-пробником `.cqds_maint_probe_*`).
def _inotify_path_is_probe_tmp(watch_path: str, name: str) -> bool:
    w = (watch_path or "").strip().replace("\\", "/")
    f = (name or "").strip().replace("\\", "/")
    if f == "test.tmp" or f.endswith("/test.tmp"):
        return True
    tw = w.rstrip("/")
    if tw == "test.tmp" or tw.endswith("/test.tmp"):
        return True
    if f:
        combined = f"{tw}/{f.lstrip('/')}"
        return combined == "test.tmp" or combined.endswith("/test.tmp")
    return False


def _inotify_ev_is_create_or_delete(ev: str) -> bool:
    """CREATE/DELETE файла или MOVED_* (не логируем CLOSE_WRITE и т.п.)."""
    u = (ev or "").upper()
    if "DELETE" in u:
        return True
    if "MOVED_TO" in u or "MOVED_FROM" in u:
        return True
    if "CREATE" in u:
        return "ISDIR" not in u
    return False


def _log_debug_inotify_probe_tmp(stdout: str) -> None:
    for raw in (stdout or "").splitlines():
        line = raw.strip()
        if not line or "test.tmp" not in line:
            continue
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        ev, w = parts[0], parts[1]
        f = parts[2] if len(parts) > 2 else ""
        if not _inotify_path_is_probe_tmp(w, f):
            continue
        if not _inotify_ev_is_create_or_delete(ev):
            continue
        log.debug("CORE_MAINT inotify probe_tmp: %s", line)


def _inotify_path_is_mcp_heartbeat(watch_path: str, name: str) -> bool:
    w = (watch_path or "").strip().replace("\\", "/")
    f = (name or "").strip().replace("\\", "/")
    if f == MCP_HEARTBEAT_FILENAME or f.endswith("/" + MCP_HEARTBEAT_FILENAME):
        return True
    tw = w.rstrip("/")
    return tw == MCP_HEARTBEAT_FILENAME or tw.endswith("/" + MCP_HEARTBEAT_FILENAME)


def _mcp_heartbeat_inotify_events_interest(ev: str) -> bool:
    """События записи heartbeat MCP (не используются в селфтесте maint_probe)."""
    u = (ev or "").upper()
    return "CLOSE_WRITE" in u or "CREATE" in u or "MOVED_TO" in u


def _record_mcp_heartbeat_from_inotify_stdout(stdout: str) -> None:
    global _mcp_heartbeat_last_mono
    for raw in (stdout or "").splitlines():
        line = raw.strip()
        if not line or MCP_HEARTBEAT_FILENAME not in line:
            continue
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        ev, w = parts[0], parts[1]
        f = parts[2] if len(parts) > 2 else ""
        if not _inotify_path_is_mcp_heartbeat(w, f):
            continue
        if not _mcp_heartbeat_inotify_events_interest(ev):
            continue
        _mcp_heartbeat_last_mono = time.monotonic()
        log.debug("CORE_MAINT mcp_heartbeat inotify: %s", line)


def _wait_inotify_event(timeout_sec: float) -> bool:
    """Block for FS events under /app/projects; return True if at least one event occurred."""
    if shutil.which("inotifywait") is None:
        raise RuntimeError("inotifywait not found")
    root = "/app/projects"
    cmd = [
        "inotifywait",
        "-r",
        "-e",
        "create,delete,move,close_write",
        "--format",
        "%e %w %f",
        "-t",
        str(max(1, int(timeout_sec))),
        root,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    _log_inotify_wait_dump(proc)
    # rc=0 => event; rc=2 => timeout (no events)
    if proc.returncode == 0:
        out = proc.stdout or ""
        _record_mcp_heartbeat_from_inotify_stdout(out)
        _log_debug_inotify_probe_tmp(out)
        return True
    if proc.returncode == 2:
        return False
    stderr = (proc.stderr or "").strip()[:300]
    raise RuntimeError(f"inotifywait rc={proc.returncode}: {stderr}")


def _mount_subdirs(project_root: Path) -> list[Path]:
    """Подкаталоги на другом st_dev (часто bind-mount) — отдельная проверка inotify."""
    out: list[Path] = []
    try:
        parent_dev = project_root.stat().st_dev
    except OSError:
        return out
    try:
        for child in project_root.iterdir():
            if not child.is_dir():
                continue
            try:
                if child.stat().st_dev != parent_dev:
                    out.append(child)
            except OSError:
                continue
    except OSError:
        pass
    return sorted(out, key=lambda p: str(p))


def _inotify_probe_dir(watch: Path) -> tuple[bool, str]:
    """Создать и сразу удалить тестовый файл; ожидаем строки CREATE и DELETE от inotifywait -m.

    Несуществующий путь → (True, skip_missing): не считается провалом inotify (нет что смотреть).
    Каталог ``/app/projects`` для режима ожидания в цикле проверяется отдельно в self-test.
    """
    if shutil.which("inotifywait") is None:
        return False, "no_inotifywait"
    if not watch.exists() or not watch.is_dir():
        return True, "skip_missing"
    proc = subprocess.Popen(
        [
            "inotifywait",
            "-q",
            "-m",
            "-e",
            "create,delete,moved_to,moved_from",
            "--format",
            "%e",
            str(watch),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    out_txt = ""
    err_txt = ""
    try:
        time.sleep(0.2)
        name = f".cqds_maint_probe_{os.getpid()}_{int(time.time() * 1000)}"
        probe = watch / name
        probe.write_text("p", encoding="utf-8")
        time.sleep(0.05)
        probe.unlink(missing_ok=True)
        time.sleep(0.4)
    except OSError as e:
        err_txt = str(e)
    finally:
        proc.terminate()
        try:
            out_txt, err2 = proc.communicate(timeout=4)
        except subprocess.TimeoutExpired:
            proc.kill()
            out_txt, err2 = proc.communicate()
        err_txt = err_txt or (err2 or "")[:300]

    lines = [ln.strip().upper() for ln in (out_txt or "").splitlines() if ln.strip()]
    has_c = any("CREATE" in ln for ln in lines)
    has_d = any("DELETE" in ln for ln in lines)
    ok = has_c and has_d
    detail = f"create={int(has_c)} delete={int(has_d)} n={len(lines)} err={err_txt!r}"
    return ok, detail


def _run_inotify_selftest(db: Database) -> tuple[str, list[tuple[int, str, Path]]]:
    """Вернуть (effective_loop, failed_subtrees).

    ``active`` — нет inotifywait, нет каталога ``/app/projects`` (нужен для ``inotifywait -r``),
    провал пробы /app/projects без проектов, или провалены корни всех существующих каталогов проектов.

    ``inotify`` — иначе; ``failed_subtrees`` — абсолютные пути (корень проекта и/или
    bind-mount подкаталоги), где проба не увидела CREATE+DELETE: для них периодический
    find+reconcile без отключения глобального inotify.

    Примечание: ``_inotify_probe_dir`` для несуществующего пути возвращает ``skip_missing`` (успех),
    чтобы не падать на пустых путях; основной цикл всегда вешает inotify на ``/app/projects``,
    поэтому отсутствие этого каталога проверяется здесь явно.
    """
    failed: list[tuple[int, str, Path]] = []
    if shutil.which("inotifywait") is None:
        log.warn("CORE_MAINT inotify selftest: inotifywait missing → active")
        return "active", []
    projects_parent = Path("/app/projects")
    if not projects_parent.is_dir():
        log.warn(
            "CORE_MAINT inotify selftest: %s missing or not a directory → active "
            "(main loop uses inotifywait -r on this path; probe alone would skip_missing)",
            projects_parent,
        )
        return "active", []
    rows = db.fetch_all("SELECT id, project_name FROM projects WHERE id > 0 ORDER BY id")
    projects = [(int(r[0]), str(r[1])) for r in (rows or []) if r and r[1]]
    cap = _selftest_max_projects()
    if len(projects) > cap:
        projects = projects[:cap]
        log.info("CORE_MAINT inotify selftest: capped projects n=%d", cap)

    if not projects:
        ok, detail = _inotify_probe_dir(Path("/app/projects"))
        if not ok:
            log.warn("CORE_MAINT inotify selftest /app/projects failed: %s", detail)
            return "active", []
        return "inotify", []

    roots_status: list[bool] = []
    for pid, pname in projects:
        root = _project_dir(pname)
        if not root.is_dir():
            continue
        root_ok, detail = _inotify_probe_dir(root)
        roots_status.append(root_ok)
        label = str(root)
        if not root_ok:
            failed.append((pid, pname, root))
            log.warn("CORE_MAINT inotify selftest FAIL %s: %s", label, detail)
            continue
        log.debug("CORE_MAINT inotify selftest ok %s: %s", label, detail)
        for sub in _mount_subdirs(root):
            ok_sub, det_sub = _inotify_probe_dir(sub)
            if not ok_sub:
                failed.append((pid, pname, sub))
                log.warn("CORE_MAINT inotify selftest FAIL %s: %s", str(sub), det_sub)
            else:
                log.debug("CORE_MAINT inotify selftest ok %s: %s", str(sub), det_sub)

    if roots_status and all(not x for x in roots_status):
        log.warn(
            "CORE_MAINT inotify selftest: all existing project roots failed → active mode for this process"
        )
        return "active", []

    if failed:
        log.info(
            "CORE_MAINT inotify hybrid: keeping inotify; periodic find for n=%d failed subtree(s)",
            len(failed),
        )
    return "inotify", failed


def _resolve_effective_mode(db: Database) -> str:
    global _effective_maint_mode, _maint_failed_subtrees
    if _effective_maint_mode is not None:
        return _effective_maint_mode
    want = _maint_mode()
    if want != "inotify":
        _maint_failed_subtrees = []
        _effective_maint_mode = want
        return want
    if not _selftest_enabled():
        _maint_failed_subtrees = []
        _effective_maint_mode = "inotify"
        log.info("CORE_MAINT inotify selftest skipped (CORE_MAINT_INOTIFY_SELFTEST=0)")
        return _effective_maint_mode
    eff, failed_list = _run_inotify_selftest(db)
    _maint_failed_subtrees = list(failed_list)
    _effective_maint_mode = eff
    return _effective_maint_mode


def _select_active_projects(db: Database, active_hours: int, top_k: int) -> list[tuple[int, str, float]]:
    score: dict[int, float] = {}

    # A: explicit active projects in user sessions.
    try:
        rows = db.fetch_all(
            """
            SELECT active_project, COUNT(*)
            FROM sessions
            WHERE active_project IS NOT NULL AND active_project > 0
            GROUP BY active_project
            """
        )
        for r in rows or []:
            pid = int(r[0] or 0)
            cnt = int(r[1] or 0)
            if pid > 0:
                score[pid] = score.get(pid, 0.0) + 1000.0 + float(cnt)
    except Exception as e:
        log.warn("CORE_MAINT sessions active selector failed: %s", str(e))

    # B: fallback recent context activity.
    since_ts = _now_ts() - int(active_hours) * 3600
    try:
        rows = db.fetch_all(
            """
            SELECT project_id, COUNT(*)
            FROM context_cache_metrics
            WHERE ts >= :since_ts AND project_id IS NOT NULL AND project_id > 0
            GROUP BY project_id
            """,
            {"since_ts": since_ts},
        )
        for r in rows or []:
            pid = int(r[0] or 0)
            cnt = int(r[1] or 0)
            if pid > 0:
                score[pid] = score.get(pid, 0.0) + float(cnt)
    except Exception as e:
        log.warn("CORE_MAINT context activity selector failed: %s", str(e))

    if not score:
        return []

    pids = sorted(score.keys(), key=lambda x: (-score[x], x))[: max(1, int(top_k))]
    result: list[tuple[int, str, float]] = []
    for pid in pids:
        row = db.fetch_one(
            "SELECT project_name FROM projects WHERE id = :pid",
            {"pid": int(pid)},
        )
        if not row or not row[0]:
            continue
        result.append((int(pid), str(row[0]), float(score[pid])))
    return result


def _fetch_db_links(db: Database, project_id: int) -> dict[str, tuple[int, int]]:
    _ps, _pp = sql_link_prefixed_params()
    rows = db.fetch_all(
        """
        SELECT id, file_name, COALESCE(missing_ttl, 0)
        FROM attached_files
        WHERE project_id = :pid AND """
        + _ps,
        {"pid": int(project_id), **_pp},
    )
    result: dict[str, tuple[int, int]] = {}
    for row in rows or []:
        file_id = int(row[0])
        rel = strip_storage_prefix(str(row[1] or "")).replace("\\", "/").lstrip("/")
        ttl = int(row[2] or 0)
        if not rel:
            continue
        result[rel] = (file_id, ttl)
    return result


def _fetch_db_links_prefix(db: Database, project_id: int, rel_prefix: str) -> dict[str, tuple[int, int]]:
    """Подмножество ссылок, чей относительный путь под префиксом (для гибридного poll)."""
    all_links = _fetch_db_links(db, project_id)
    if not rel_prefix:
        return all_links
    pfx = rel_prefix.replace("\\", "/").strip("/")
    if not pfx:
        return all_links
    return {k: v for k, v in all_links.items() if k == pfx or k.startswith(pfx + "/")}


def _degrade_missing(db: Database, fm: FileManager, file_id: int, ttl_prev: int) -> int:
    ttl_next = max(0, min(int(ttl_prev), fm.missing_ttl_max) - 1)
    db.execute(
        """
        UPDATE attached_files
        SET missing_ttl = :ttl, missing_checked_ts = :ts
        WHERE id = :id
        """,
        {"ttl": ttl_next, "ts": _now_ts(), "id": int(file_id)},
    )
    return ttl_next


def _degrade_or_purge_missing(
    db: Database,
    fm: FileManager,
    file_id: int,
    ttl_prev: int,
    *,
    project_id: int,
    rel: str,
    purge: bool,
) -> str:
    """Декремент missing_ttl либо удаление строки, если TTL исчерпан и purge включён. Возвращает purged|degraded."""
    ttl_next = max(0, min(int(ttl_prev), fm.missing_ttl_max) - 1)
    if purge and ttl_next == 0:
        fm.purge_stale_attached_row(int(file_id), int(project_id))
        log.info(
            "CORE_MAINT purged TTL-exhausted link project_id=%d file_id=%d rel=%s",
            int(project_id),
            int(file_id),
            rel,
        )
        return "purged"
    _degrade_missing(db, fm, file_id, ttl_prev)
    return "degraded"


def _recover_present(db: Database, fm: FileManager, file_id: int, ttl_prev: int) -> None:
    if int(ttl_prev) >= int(fm.missing_ttl_max):
        return
    db.execute(
        """
        UPDATE attached_files
        SET missing_ttl = :ttl, missing_checked_ts = :ts
        WHERE id = :id
        """,
        {"ttl": int(fm.missing_ttl_max), "ts": _now_ts(), "id": int(file_id)},
    )


def _lazy_scan_if_due(project_id: int, last_scan: dict[int, float], cooldown_sec: float, budget_sec: float) -> bool:
    now = time.monotonic()
    due = (now - float(last_scan.get(project_id, 0.0))) >= max(1.0, cooldown_sec)
    if not due:
        return False
    pm = ProjectManager.get(int(project_id))
    if pm is None or pm.project_name is None:
        return False
    started = time.monotonic()
    pm.scan_project_files()
    elapsed = time.monotonic() - started
    if elapsed >= budget_sec:
        log.warn(
            "CORE_MAINT scan exceeded budget project_id=%d elapsed=%.2fs budget=%.2fs",
            int(project_id),
            elapsed,
            budget_sec,
        )
    last_scan[project_id] = now
    return True


def run_tick(last_scan: dict[int, float], *, once: bool = False) -> None:
    db = Database.get_database()
    fm = FileManager()
    mutate = _mutate_enabled()
    scan_on = _scan_enabled()
    budget_sec = _proj_budget_sec()
    timeout_sec = _find_timeout_sec()

    selected = _select_active_projects(db, _active_hours(), _max_projects())
    if not selected:
        log.debug("CORE_MAINT no active projects")
        return

    for project_id, project_name, score in selected:
        t0 = time.monotonic()
        project_root = _project_dir(project_name)
        fs_set: set[str] = set()
        found_ok = False
        try:
            fs_set, found_ok = _find_files_snapshot(project_root, timeout_sec)
        except Exception as e:
            log.warn("CORE_MAINT find failed project_id=%d name=%s: %s", project_id, project_name, str(e))
            continue

        db_links = _fetch_db_links(db, project_id)
        db_set = set(db_links.keys())

        db_only = sorted(db_set - fs_set)
        fs_only = sorted(fs_set - db_set)
        both = sorted(fs_set & db_set)

        degraded = 0
        purged = 0
        recovered = 0
        added = 0
        do_purge = _purge_stale_links_enabled()
        if mutate:
            for rel in db_only:
                fid, ttl_prev = db_links[rel]
                if _degrade_or_purge_missing(
                    db, fm, fid, ttl_prev, project_id=project_id, rel=rel, purge=do_purge
                ) == "purged":
                    purged += 1
                else:
                    degraded += 1
            for rel in both:
                fid, ttl_prev = db_links[rel]
                if ttl_prev < fm.missing_ttl_max:
                    _recover_present(db, fm, fid, ttl_prev)
                    recovered += 1
            for rel in fs_only:
                try:
                    fp = project_root / rel
                    ts = int(fp.stat().st_mtime) if fp.exists() else _now_ts()
                    fm.add_file(rel, None, timestamp=ts, project_id=project_id)
                    added += 1
                except Exception as e:
                    log.warn(
                        "CORE_MAINT add link failed project_id=%d file=%s: %s",
                        project_id,
                        rel,
                        str(e),
                    )

        scanned = False
        if scan_on and (db_only or fs_only):
            try:
                scanned = _lazy_scan_if_due(
                    project_id=project_id,
                    last_scan=last_scan,
                    cooldown_sec=_scan_cooldown_sec(),
                    budget_sec=budget_sec,
                )
            except Exception as e:
                log.warn("CORE_MAINT lazy scan failed project_id=%d: %s", project_id, str(e))

        elapsed_ms = int((time.monotonic() - t0) * 1000.0)
        mode = "mutate" if mutate else "dry_run"
        log.info(
            "CORE_MAINT project_id=%d name=%s mode=%s score=%.1f selected=1 find_ok=%s find_count=%d db_count=%d "
            "db_only=%d fs_only=%d both=%d degraded=%d purged=%d recovered=%d added=%d scanned=%s elapsed_ms=%d",
            project_id,
            project_name,
            mode,
            score,
            "1" if found_ok else "0",
            len(fs_set),
            len(db_set),
            len(db_only),
            len(fs_only),
            len(both),
            degraded,
            purged,
            recovered,
            added,
            "1" if scanned else "0",
            elapsed_ms,
        )
        if once and elapsed_ms > int(max(1.0, budget_sec) * 1000.0):
            log.warn("CORE_MAINT project over budget project_id=%d elapsed_ms=%d", project_id, elapsed_ms)


def run_poll_failed_subtrees(last_scan: dict[int, float]) -> None:
    """find+reconcile только под путями из self-test (гибрид inotify)."""
    global _maint_failed_subtrees
    if not _maint_failed_subtrees:
        return
    db = Database.get_database()
    fm = FileManager()
    mutate = _mutate_enabled()
    scan_on = _scan_enabled()
    budget_sec = _proj_budget_sec()
    timeout_sec = _find_timeout_sec()

    seen: set[tuple[int, str]] = set()
    for project_id, project_name, subtree_abs in _maint_failed_subtrees:
        key = (int(project_id), str(subtree_abs.resolve()))
        if key in seen:
            continue
        seen.add(key)

        project_root = _project_dir(project_name)
        try:
            rel_prefix = subtree_abs.resolve().relative_to(project_root.resolve())
            rel_s = str(rel_prefix).replace("\\", "/").strip("/")
        except Exception:
            log.warn("CORE_MAINT poll subtree outside project root, skip: %s", subtree_abs)
            continue

        t0 = time.monotonic()
        fs_set: set[str] = set()
        found_ok = False
        try:
            fs_set, found_ok = _find_files_snapshot_under(project_root, subtree_abs, timeout_sec)
        except Exception as e:
            log.warn(
                "CORE_MAINT poll find failed project_id=%d name=%s subtree=%s: %s",
                project_id,
                project_name,
                rel_s or ".",
                str(e),
            )
            continue

        db_links = _fetch_db_links_prefix(db, project_id, rel_s)
        db_set = set(db_links.keys())

        db_only = sorted(db_set - fs_set)
        fs_only = sorted(fs_set - db_set)
        both = sorted(fs_set & db_set)

        degraded = 0
        purged = 0
        recovered = 0
        added = 0
        do_purge = _purge_stale_links_enabled()
        if mutate:
            for rel in db_only:
                fid, ttl_prev = db_links[rel]
                if _degrade_or_purge_missing(
                    db, fm, fid, ttl_prev, project_id=project_id, rel=rel, purge=do_purge
                ) == "purged":
                    purged += 1
                else:
                    degraded += 1
            for rel in both:
                fid, ttl_prev = db_links[rel]
                if ttl_prev < fm.missing_ttl_max:
                    _recover_present(db, fm, fid, ttl_prev)
                    recovered += 1
            for rel in fs_only:
                try:
                    fp = project_root / rel
                    ts = int(fp.stat().st_mtime) if fp.exists() else _now_ts()
                    fm.add_file(rel, None, timestamp=ts, project_id=project_id)
                    added += 1
                except Exception as e:
                    log.warn(
                        "CORE_MAINT poll add link failed project_id=%d file=%s: %s",
                        project_id,
                        rel,
                        str(e),
                    )

        scanned = False
        if scan_on and (db_only or fs_only):
            try:
                scanned = _lazy_scan_if_due(
                    project_id=project_id,
                    last_scan=last_scan,
                    cooldown_sec=_scan_cooldown_sec(),
                    budget_sec=budget_sec,
                )
            except Exception as e:
                log.warn("CORE_MAINT poll lazy scan failed project_id=%d: %s", project_id, str(e))

        elapsed_ms = int((time.monotonic() - t0) * 1000.0)
        mode = "mutate" if mutate else "dry_run"
        log.info(
            "CORE_MAINT project_id=%d name=%s kind=poll_failed subtree=%s mode=%s find_ok=%s "
            "find_count=%d db_count=%d db_only=%d fs_only=%d both=%d degraded=%d purged=%d recovered=%d "
            "added=%d scanned=%s elapsed_ms=%d",
            project_id,
            project_name,
            rel_s or ".",
            mode,
            "1" if found_ok else "0",
            len(fs_set),
            len(db_set),
            len(db_only),
            len(fs_only),
            len(both),
            degraded,
            purged,
            recovered,
            added,
            "1" if scanned else "0",
            elapsed_ms,
        )


def main() -> int:
    global _maint_process_start_mono
    ap = argparse.ArgumentParser(description="Semi-autonomous core maintenance loop")
    ap.add_argument("--once", action="store_true", help="Run one tick and exit")
    args = ap.parse_args()

    db = Database.get_database()
    eff_loop = "active"
    if _maint_enabled():
        eff_loop = _resolve_effective_mode(db)

    hybrid_n = len(_maint_failed_subtrees) if _maint_enabled() and eff_loop == "inotify" else 0
    if _maint_enabled():
        _maint_process_start_mono = time.monotonic()
    log.info(
        "CORE_MAINT process started enabled=%s mutate=%s mode_req=%s mode_eff=%s selftest=%s interval=%.1fs "
        "hybrid_failed_subtrees_n=%d hybrid_poll_sec=%s mcp_heartbeat_tune=%s inotify_timeout=%.1fs",
        "1" if _maint_enabled() else "0",
        "1" if _mutate_enabled() else "0",
        _maint_mode(),
        eff_loop if _maint_enabled() else "-",
        "1" if _selftest_enabled() else "0",
        _tick_sleep(),
        hybrid_n,
        f"{_poll_failed_subtrees_sec():.1f}" if hybrid_n else "-",
        "1" if _mcp_heartbeat_tune_enabled() else "0",
        _inotify_timeout_sec() if _maint_enabled() and eff_loop == "inotify" else 0.0,
    )
    last_scan: dict[int, float] = {}
    last_force_active = time.monotonic()
    last_failed_poll = time.monotonic()
    inotify_warned = False

    if args.once:
        if _maint_enabled():
            run_tick(last_scan, once=True)
        else:
            log.info("CORE_MAINT disabled (CORE_MAINT_ENABLED=0)")
        return 0

    while True:
        try:
            if _maint_enabled():
                mode = eff_loop
                if mode == "inotify":
                    had_event = False
                    try:
                        had_event = _wait_inotify_event(_inotify_timeout_sec())
                    except Exception as e:
                        if not inotify_warned:
                            log.warn("CORE_MAINT inotify wait failed, tick every loop: %s", str(e))
                            inotify_warned = True
                        had_event = True
                    # Гибрид: периодический find только под провалившимися путями self-test.
                    if _maint_failed_subtrees:
                        poll_iv = _effective_poll_failed_subtrees_sec()
                        if (time.monotonic() - last_failed_poll) >= poll_iv:
                            run_poll_failed_subtrees(last_scan)
                            last_failed_poll = time.monotonic()
                    force_age = time.monotonic() - last_force_active
                    force_due = force_age >= _effective_inotify_force_active_sec()
                    if had_event:
                        run_tick(last_scan)
                        last_force_active = time.monotonic()
                    elif force_due:
                        if _maint_failed_subtrees:
                            run_poll_failed_subtrees(last_scan)
                            last_failed_poll = time.monotonic()
                            log.info(
                                "CORE_MAINT inotify hybrid fallback poll age=%.1fs (no full-tree tick)",
                                force_age,
                            )
                        else:
                            run_tick(last_scan)
                            log.info("CORE_MAINT inotify fallback active tick age=%.1fs", force_age)
                        last_force_active = time.monotonic()
                else:
                    run_tick(last_scan)
            else:
                log.debug("CORE_MAINT disabled tick")
        except Exception as e:
            log.excpt("CORE_MAINT tick failed", e=e)
        # active: sleep between ticks; inotify: blocking wait already throttles loop
        if not _maint_enabled():
            time.sleep(_tick_sleep())
        elif eff_loop == "active":
            time.sleep(_tick_sleep())


if __name__ == "__main__":
    raise SystemExit(main())
