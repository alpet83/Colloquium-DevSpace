#!/usr/bin/env python3
"""Semi-autonomous maintenance loop for active projects.

Single process cycle (или пул при CORE_MAINT_POOL_WORKERS>1):
- choose active projects (sessions.active_project + recent context activity),
- run find snapshot,
- compare with attached_files links,
- optionally mutate DB (degrade/recover/add links; purge @-links when missing_ttl exhausted),
- optionally trigger lazy project scan with cooldown.

Пул: оркестратор ставит строки в maint_pool_jobs (не более одной queued/running на project_id);
подпроцессы делают claim + run_tick_for_project; прогресс — строки ``MAINT_POOL_PROGRESS`` в stdout воркера.
Ленивый scan/find выполняет только воркер с взятым job; без задачи воркер только sleep (``CORE_MAINT_POOL_IDLE_SLEEP_SEC``).
Логи: общий каталог ``/app/logs/core_maint/``; оркестратор — префикс ``core_maint``, воркер слота N — ``core_maint_wN`` (без pid в имени).
"""
from __future__ import annotations

import argparse
import atexit
import datetime as dt
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from lib import maint_pool as _maint_pool

_THIS = Path(__file__).resolve()
_AGENT_ROOT = _THIS.parents[1]
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))

import globals
from lib.basic_logger import BasicLogger
from lib.file_type_detector import is_acceptable_file
from lib.file_link_prefix import sql_link_prefixed_params, strip_storage_prefix
from lib.project_scan_filter import ProjectScanFilter
from managers.db import Database
from managers.files import FileManager
from managers.project import ProjectManager
from managers.runtime_config import get_bool, get_float, get_int


def _build_core_maint_log() -> BasicLogger:
    """Один каталог core_maint; воркеры отличаются только префиксом файла (core_maint_w<slot>)."""
    if "CORE_MAINT_POOL_WORKER_SLOT" in os.environ:
        slot = (os.environ.get("CORE_MAINT_POOL_WORKER_SLOT") or "0").strip().replace("/", "_").replace("\\", "_")[:16]
        return BasicLogger("core_maint", f"core_maint_w{slot}", sys.stdout)
    return globals.get_logger("core_maint")


log = _build_core_maint_log()

_pool_worker_procs: list[subprocess.Popen[Any]] = []


def _pool_workers() -> int:
    """N>1: оркестратор ставит задачи в maint_pool_jobs, подпроцессы-воркеры их исполняют."""
    return get_int("CORE_MAINT_POOL_WORKERS", 1, 1, 32)


def _pool_idle_sleep_sec() -> float:
    """Пауза воркера без задачи (passive): find/scan по проектам здесь не выполняются."""
    return get_float("CORE_MAINT_POOL_IDLE_SLEEP_SEC", 4.0, 0.25, 120.0)


def _is_maint_pool_worker() -> bool:
    """Подпроцесс-пул: в env задан CORE_MAINT_POOL_WORKER_SLOT (0..N-1)."""
    return "CORE_MAINT_POOL_WORKER_SLOT" in os.environ


def _maint_pool_worker_slot() -> int:
    try:
        return max(0, int(os.environ.get("CORE_MAINT_POOL_WORKER_SLOT", "0")))
    except ValueError:
        return 0


def _pool_orchestrator(*, tick_once: bool = False) -> bool:
    return _pool_workers() > 1 and not _is_maint_pool_worker() and not tick_once


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


def _rel_excluded_from_maint_snapshot(rel: str, scan_filter: ProjectScanFilter | None = None) -> bool:
    if rel.startswith("backups/"):
        return True
    if rel == MCP_HEARTBEAT_FILENAME or rel.endswith("/" + MCP_HEARTBEAT_FILENAME):
        return True
    if scan_filter is not None and scan_filter.is_excluded(rel):
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
    scan_filter = ProjectScanFilter(project_root, logger=log)
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
        if _rel_excluded_from_maint_snapshot(rel, scan_filter):
            continue
        if not is_acceptable_file(p):
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
    scan_filter = ProjectScanFilter(project_root, logger=log)
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
        if _rel_excluded_from_maint_snapshot(rel, scan_filter):
            continue
        if not is_acceptable_file(p):
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


def _lazy_scan_if_due_pool(
    db: Database,
    project_id: int,
    *,
    budget_sec: float,
    cooldown_sec: float,
) -> bool:
    """Ленивый scan с cooldown в maint_scan_cooldown (для пула воркеров без общего last_scan)."""
    now = time.monotonic()
    row = db.fetch_one(
        "SELECT last_mono FROM maint_scan_cooldown WHERE project_id = :pid",
        {"pid": int(project_id)},
    )
    last = float(row[0]) if row and row[0] is not None else 0.0
    if (now - last) < max(1.0, cooldown_sec):
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
    db.execute(
        """
        INSERT INTO maint_scan_cooldown (project_id, last_mono)
        VALUES (:pid, :m)
        ON CONFLICT(project_id) DO UPDATE SET last_mono = excluded.last_mono
        """,
        {"pid": int(project_id), "m": now},
    )
    return True


def _maybe_pool_progress(
    cb: Callable[..., None] | None,
    stage: str,
    *,
    force: bool = False,
    **extra: Any,
) -> None:
    if cb is None:
        return
    cb(stage, force=force, **extra)


def run_tick_for_project(
    db: Database,
    project_id: int,
    project_name: str,
    score: float,
    *,
    last_scan: dict[int, float] | None,
    use_db_cooldown: bool,
    progress_cb: Callable[..., None] | None,
    once: bool = False,
) -> None:
    fm = FileManager()
    mutate = _mutate_enabled()
    scan_on = _scan_enabled()
    budget_sec = _proj_budget_sec()
    timeout_sec = _find_timeout_sec()

    t0 = time.monotonic()
    _maybe_pool_progress(progress_cb, "start", force=True, project_name=project_name)
    project_root = _project_dir(project_name)
    fs_set: set[str] = set()
    found_ok = False
    try:
        _maybe_pool_progress(progress_cb, "find_begin", force=True, root=str(project_root))
        fs_set, found_ok = _find_files_snapshot(project_root, timeout_sec)
        _maybe_pool_progress(
            progress_cb,
            "find_done",
            force=True,
            find_count=len(fs_set),
            find_ok=bool(found_ok),
        )
    except Exception as e:
        log.warn("CORE_MAINT find failed project_id=%d name=%s: %s", project_id, project_name, str(e))
        _maybe_pool_progress(progress_cb, "find_error", force=True, error=str(e)[:300])
        raise

    db_links = _fetch_db_links(db, project_id)
    db_set = set(db_links.keys())

    db_only = sorted(db_set - fs_set)
    fs_only = sorted(fs_set - db_set)
    both = sorted(fs_set & db_set)
    _maybe_pool_progress(
        progress_cb,
        "reconcile_begin",
        force=True,
        db_only=len(db_only),
        fs_only=len(fs_only),
        both=len(both),
    )

    degraded = 0
    purged = 0
    recovered = 0
    added = 0
    do_purge = _purge_stale_links_enabled()
    step = 0
    if mutate:
        for rel in db_only:
            step += 1
            if progress_cb and step % 400 == 0:
                _maybe_pool_progress(progress_cb, "reconcile_db_only", rel=rel, step=step, total=len(db_only))
            fid, ttl_prev = db_links[rel]
            if _degrade_or_purge_missing(
                db, fm, fid, ttl_prev, project_id=project_id, rel=rel, purge=do_purge
            ) == "purged":
                purged += 1
            else:
                degraded += 1
        for rel in both:
            step += 1
            if progress_cb and step % 400 == 0:
                _maybe_pool_progress(progress_cb, "reconcile_both", step=step)
            fid, ttl_prev = db_links[rel]
            if ttl_prev < fm.missing_ttl_max:
                _recover_present(db, fm, fid, ttl_prev)
                recovered += 1
        for rel in fs_only:
            step += 1
            if progress_cb and step % 400 == 0:
                _maybe_pool_progress(progress_cb, "reconcile_fs_only", step=step)
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
            if use_db_cooldown:
                _maybe_pool_progress(progress_cb, "lazy_scan_maybe", force=True)
                scanned = _lazy_scan_if_due_pool(
                    db,
                    project_id,
                    budget_sec=budget_sec,
                    cooldown_sec=_scan_cooldown_sec(),
                )
            else:
                assert last_scan is not None
                scanned = _lazy_scan_if_due(
                    project_id=project_id,
                    last_scan=last_scan,
                    cooldown_sec=_scan_cooldown_sec(),
                    budget_sec=budget_sec,
                )
        except Exception as e:
            log.warn("CORE_MAINT lazy scan failed project_id=%d: %s", project_id, str(e))
            _maybe_pool_progress(progress_cb, "lazy_scan_error", force=True, error=str(e)[:300])

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
    _maybe_pool_progress(
        progress_cb,
        "done",
        force=True,
        elapsed_ms=elapsed_ms,
        scanned=bool(scanned),
    )
    if once and elapsed_ms > int(max(1.0, budget_sec) * 1000.0):
        log.warn("CORE_MAINT project over budget project_id=%d elapsed_ms=%d", project_id, elapsed_ms)


def run_tick(last_scan: dict[int, float], *, once: bool = False) -> None:
    db = Database.get_database()
    selected = _select_active_projects(db, _active_hours(), _max_projects())
    if not selected:
        log.debug("CORE_MAINT no active projects")
        return

    if _pool_orchestrator(tick_once=once):
        _maint_pool.ensure_maint_pool_tables(db.engine)
        n = _maint_pool.enqueue_reconcile_tick_jobs(db.engine, selected)
        if n:
            log.info("CORE_MAINT pool enqueue new_jobs=%d projects=%d", n, len(selected))
        return

    for project_id, project_name, score in selected:
        run_tick_for_project(
            db,
            project_id,
            project_name,
            score,
            last_scan=last_scan,
            use_db_cooldown=False,
            progress_cb=None,
            once=once,
        )


def _shutdown_pool_workers() -> None:
    global _pool_worker_procs
    for p in _pool_worker_procs:
        try:
            if p.poll() is None:
                p.terminate()
        except Exception:
            pass
    for p in _pool_worker_procs:
        try:
            p.wait(timeout=4)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
    _pool_worker_procs.clear()


def _sync_maint_pool_status_file() -> None:
    """Оркестратор пула: PID-ы живых воркеров для GET /api/core/status."""
    if not _pool_orchestrator(tick_once=False):
        return
    global _pool_worker_procs
    from pathlib import Path

    alive = [int(p.pid) for p in _pool_worker_procs if p.poll() is None]
    body = {
        "configured_slots": int(_pool_workers()),
        "worker_processes_alive": len(alive),
        "worker_pids": alive,
        "orchestrator_pid": int(os.getpid()),
        "updated_at": int(time.time()),
    }
    path = Path(_maint_pool.MAINT_POOL_STATUS_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _spawn_pool_workers(n: int) -> None:
    global _pool_worker_procs
    script = str(_THIS)
    exe = sys.executable
    for i in range(max(1, int(n))):
        env = os.environ.copy()
        env["CORE_MAINT_POOL_WORKER_SLOT"] = str(i)
        try:
            p = subprocess.Popen(
                [exe, script],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=None,
                stderr=None,
            )
            _pool_worker_procs.append(p)
            log.info("CORE_MAINT pool worker slot=%d pid=%d", i, p.pid)
        except Exception as e:
            log.warn("CORE_MAINT pool worker spawn failed slot=%d: %s", i, str(e))


def maint_pool_worker_main() -> int:
    """Подпроцесс: claim из maint_pool_jobs → run_tick_for_project (DB cooldown для lazy scan)."""
    slot = _maint_pool_worker_slot()
    db = Database.get_database()
    _maint_pool.ensure_maint_pool_tables(db.engine)
    wid = f"w{slot}-{_maint_pool.default_worker_id()}"
    lease = _maint_pool.pool_lease_sec()
    log.info("CORE_MAINT pool worker loop slot=%d id=%s", slot, wid)
    while True:
        try:
            if not _maint_enabled():
                time.sleep(max(2.0, _pool_idle_sleep_sec()))
                continue
            job = _maint_pool.claim_next_job(db.engine, wid, lease)
            if not job:
                time.sleep(_pool_idle_sleep_sec())
                continue
            jid = int(job["job_id"])
            pid = int(job["project_id"])
            row = db.fetch_one("SELECT project_name FROM projects WHERE id = :pid", {"pid": pid})
            if not row or not row[0]:
                _maint_pool.fail_job(db.engine, jid, "project not found")
                continue
            pname = str(row[0])
            score = 0.0
            kind_raw = str(job.get("kind") or "reconcile_tick").strip().lower()
            last_emit: list[float] = [0.0]
            iv = _maint_pool.pool_progress_interval_sec()

            def _cb(stage: str, *, force: bool = False, **extra: Any) -> None:
                now_m = time.monotonic()
                if not force and (now_m - last_emit[0]) < iv:
                    return
                last_emit[0] = now_m
                _maint_pool.emit_progress(jid, pid, stage, worker_id=wid, extra=extra or None)
                _maint_pool.touch_job_lease(db.engine, jid, lease)
                _maint_pool.update_job_progress_db(db.engine, jid, stage, extra)

            try:
                if kind_raw == "code_index":
                    from lib.maint_code_index_job import execute_code_index_maint_job

                    summary = execute_code_index_maint_job(pid, progress_cb=_cb)
                    log.info(
                        "CORE_MAINT pool code_index done job_id=%d project_id=%d entities=%s cache=%s",
                        jid,
                        pid,
                        summary.get("entities"),
                        summary.get("cache_path"),
                    )
                else:
                    run_tick_for_project(
                        db,
                        pid,
                        pname,
                        score,
                        last_scan=None,
                        use_db_cooldown=True,
                        progress_cb=_cb,
                        once=False,
                    )
                _maint_pool.complete_job(db.engine, jid)
            except Exception as e:
                log.warn("CORE_MAINT pool job failed job_id=%d project_id=%d kind=%s: %s", jid, pid, kind_raw, str(e))
                _maint_pool.fail_job(db.engine, jid, str(e))
        except Exception as e:
            log.excpt("CORE_MAINT pool worker loop error", e=e)
            time.sleep(1.0)


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

    if _is_maint_pool_worker():
        maint_pool_worker_main()
        return 0

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
    if _maint_enabled() and _pool_orchestrator(tick_once=args.once) and not _is_maint_pool_worker():
        _maint_pool.ensure_maint_pool_tables(db.engine)
        nw = _pool_workers()
        _spawn_pool_workers(nw)
        atexit.register(_shutdown_pool_workers)

        def _sig_pool(_signum: int, _frame: Any) -> None:
            _shutdown_pool_workers()
            sys.exit(0)

        signal.signal(signal.SIGTERM, _sig_pool)
        signal.signal(signal.SIGINT, _sig_pool)
        log.info("CORE_MAINT pool orchestrator spawned_workers=%d (stdout MAINT_POOL_PROGRESS from workers)", nw)
        _sync_maint_pool_status_file()
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
            _sync_maint_pool_status_file()
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
