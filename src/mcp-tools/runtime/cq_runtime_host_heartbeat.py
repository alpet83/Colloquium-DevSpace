"""Запись `.cqds_mcp_active.pid` на машине, где крутится `cqds_mcp_mini.py` (хост Cursor).

PID в файле — ``os.getpid()`` процесса Python MCP runtime.

Каталоги проектов на хосте **не задаются напрямую через env**: по умолчанию вызывается
``docker inspect`` для контейнера CQDS (имя по умолчанию ``cqds-core``, см. ``container_name`` в compose), из поля
``Mounts`` извлекаются bind-монты с ``Destination`` ``/app/projects`` или ``/app/projects/<name>``,
и heartbeat пишется в соответствующие ``Source`` на хосте.

Опциональный fallback: ``CQDS_MCP_HEARTBEAT_PROJECTS_DIR`` (родитель подкаталогов-проектов),
если inspect не дал путей или Docker недоступен.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path

MCP_HEARTBEAT_FILENAME = ".cqds_mcp_active.pid"

_PROJECTS_MOUNT_PREFIX = "/app/projects"

# Кэш результатов inspect, чтобы не дергать docker каждые 55 с.
_INSPECT_CACHE_ROOTS: list[Path] | None = None
_INSPECT_CACHE_MONO: float = 0.0


def _interval_sec() -> float:
    raw = os.environ.get("CQDS_MCP_HEARTBEAT_INTERVAL_SEC", "55")
    try:
        v = float(raw)
    except ValueError:
        v = 55.0
    return max(15.0, min(v, 3600.0))


def _inspect_cache_sec() -> float:
    raw = os.environ.get("CQDS_MCP_HEARTBEAT_INSPECT_CACHE_SEC", "45")
    try:
        v = float(raw)
    except ValueError:
        v = 45.0
    return max(5.0, min(v, 600.0))


def heartbeat_enabled() -> bool:
    return os.environ.get("CQDS_MCP_PROJECT_HEARTBEAT", "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def heartbeat_docker_container() -> str:
    return (os.environ.get("CQDS_MCP_HEARTBEAT_DOCKER_CONTAINER") or "cqds-core").strip()


def fallback_projects_parent() -> Path | None:
    raw = (os.environ.get("CQDS_MCP_HEARTBEAT_PROJECTS_DIR", "") or "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser().resolve()
    if not p.is_dir():
        return None
    return p


def fallback_projects_parent_configured() -> bool:
    return (os.environ.get("CQDS_MCP_HEARTBEAT_PROJECTS_DIR", "") or "").strip() != ""


def should_run_heartbeat_loop() -> bool:
    return heartbeat_enabled()


def _normalize_dest(dest: str) -> str:
    return (dest or "").replace("\\", "/").rstrip("/")


def project_roots_from_mounts(mounts: list[dict[str, object]]) -> list[Path]:
    """Собрать хостовые корни проектов из ``Mounts`` одного контейнера."""
    parent_src: Path | None = None
    per_project: list[Path] = []
    for m in mounts:
        if not isinstance(m, dict):
            continue
        if m.get("Type") != "bind":
            continue
        src = m.get("Source")
        dest = _normalize_dest(str(m.get("Destination") or ""))
        if not src or not dest:
            continue
        host_path = Path(str(src))
        if dest == _PROJECTS_MOUNT_PREFIX:
            parent_src = host_path
        elif dest.startswith(_PROJECTS_MOUNT_PREFIX + "/"):
            rest = dest[len(_PROJECTS_MOUNT_PREFIX) :].lstrip("/")
            if rest and "/" not in rest:
                per_project.append(host_path)
    roots: list[Path] = []
    seen: set[Path] = set()
    for p in per_project:
        try:
            rp = p.resolve()
        except OSError:
            rp = p
        if rp.is_dir() and rp not in seen:
            seen.add(rp)
            roots.append(rp)
    if parent_src is not None:
        try:
            par = parent_src.resolve()
        except OSError:
            par = parent_src
        if par.is_dir():
            try:
                for child in par.iterdir():
                    if not child.is_dir():
                        continue
                    try:
                        rc = child.resolve()
                    except OSError:
                        rc = child
                    if rc not in seen:
                        seen.add(rc)
                        roots.append(rc)
            except OSError:
                pass
    return roots


async def _docker_inspect_mounts(container: str, log: logging.Logger) -> list[dict[str, object]] | None:
    exe = shutil.which("docker")
    if not exe:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            exe,
            "inspect",
            container,
            "--format",
            "{{json .Mounts}}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
    except Exception as exc:
        log.warning("MCP host heartbeat: docker inspect failed: %s", exc)
        return None
    if proc.returncode != 0:
        err = (stderr or b"").decode("utf-8", errors="replace")[:400]
        log.warning("MCP host heartbeat: docker inspect rc=%s stderr=%s", proc.returncode, err)
        return None
    try:
        data = json.loads((stdout or b"").decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        log.warning("MCP host heartbeat: docker inspect JSON: %s", exc)
        return None
    if not isinstance(data, list):
        return None
    return [x for x in data if isinstance(x, dict)]


def _roots_from_fallback_parent(parent: Path) -> list[Path]:
    roots: list[Path] = []
    if not parent.is_dir():
        return roots
    try:
        for child in parent.iterdir():
            if child.is_dir():
                roots.append(child.resolve() if child.exists() else child)
    except OSError:
        pass
    return roots


async def discover_project_roots(log: logging.Logger) -> list[Path]:
    """Хостовые каталоги проектов: docker inspect → опционально fallback env."""
    global _INSPECT_CACHE_ROOTS, _INSPECT_CACHE_MONO
    now = time.monotonic()
    ttl = _inspect_cache_sec()
    if _INSPECT_CACHE_ROOTS is not None and (now - _INSPECT_CACHE_MONO) < ttl:
        return [p for p in _INSPECT_CACHE_ROOTS if p.is_dir()]

    container = heartbeat_docker_container()
    mounts = await _docker_inspect_mounts(container, log)
    roots: list[Path] = []
    if mounts is not None:
        roots = project_roots_from_mounts(mounts)
    if not roots:
        fb = fallback_projects_parent()
        if fb is not None:
            roots = _roots_from_fallback_parent(fb)
            if roots:
                log.debug("MCP host heartbeat: using fallback CQDS_MCP_HEARTBEAT_PROJECTS_DIR=%s", fb)

    _INSPECT_CACHE_ROOTS = list(roots)
    _INSPECT_CACHE_MONO = now
    return [p for p in roots if p.is_dir()]


async def host_project_heartbeat_loop(log: logging.Logger) -> None:
    """Периодически обновляет heartbeat-файл в корне каждого обнаруженного проекта на хосте."""
    log.info(
        "MCP host heartbeat: docker_container=%s file=%s interval=%.1fs runtime_pid=%s",
        heartbeat_docker_container(),
        MCP_HEARTBEAT_FILENAME,
        _interval_sec(),
        os.getpid(),
    )
    empty_logged = False
    while True:
        try:
            await asyncio.sleep(_interval_sec())
            if not heartbeat_enabled():
                continue
            roots = await discover_project_roots(log)
            if not roots:
                if not empty_logged:
                    log.warning(
                        "MCP host heartbeat: no host project roots "
                        "(check container name %s, bind mounts to /app/projects, or set CQDS_MCP_HEARTBEAT_PROJECTS_DIR)",
                        heartbeat_docker_container(),
                    )
                    empty_logged = True
                continue
            empty_logged = False
            pid = os.getpid()
            ts = int(time.time())
            payload = f"{pid}\n{ts}\n"
            for root in roots:
                hp = root / MCP_HEARTBEAT_FILENAME
                try:
                    hp.write_text(payload, encoding="utf-8")
                except OSError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("MCP host heartbeat error: %s", exc)
