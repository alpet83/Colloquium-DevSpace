# maint_code_index_job.py — полный scan + sandwiches index в кэш (процесс maint-воркера).
from __future__ import annotations

import time
from typing import Any, Callable

from fastapi import HTTPException

from lib.maint_worker_init import ensure_maint_worker_globals


def execute_code_index_maint_job(
    project_id: int,
    *,
    progress_cb: Callable[..., None] | None = None,
) -> dict[str, Any]:
    """
    Те же шаги, что GET /project/code_index, но без HTTP-сессии.
    Вызывать только из maint pool worker после ensure_maint_worker_globals().
    """
    ensure_maint_worker_globals()
    from routes import project_routes as pr

    def _p(stage: str, *, force: bool = False, **kw: Any) -> None:
        if progress_cb is None:
            return
        progress_cb(stage, force=force, **kw)

    try:
        pm, project_name = pr._resolve_project(int(project_id))
    except HTTPException as e:
        raise RuntimeError(str(e.detail)) from e

    _p("code_index_scan_begin", force=True, project_name=project_name)
    t0 = time.monotonic()
    scanned = pm.scan_project_files() or []
    scan_sec = round(time.monotonic() - t0, 3)
    _p("code_index_scan_done", force=True, scan_files=len(scanned), scan_sec=scan_sec)

    cache_probe = pr.read_project_cached_index(str(project_name))
    try:
        from lib.code_index_incremental import validate_cache as _validate_idx_cache

        ok = cache_probe is not None and _validate_idx_cache(cache_probe)
        rev = int(cache_probe.get("rebuild_revision", 0)) if isinstance(cache_probe, dict) else None
    except (TypeError, ValueError, AttributeError):
        ok, rev = False, None
    _p(
        "code_index_cache_probe",
        force=True,
        cache_present=bool(cache_probe),
        cache_valid=ok,
        rebuild_revision=rev,
    )

    _p("code_index_build_begin", force=True)
    try:
        _index_data, files_count, blocks_count, entities_count, cache_path = pr._build_project_index_sync(
            int(project_id), str(project_name)
        )
    except HTTPException as e:
        raise RuntimeError(str(e.detail)) from e

    _p(
        "code_index_build_done",
        force=True,
        files=files_count,
        blocks=blocks_count,
        entities=entities_count,
        cache_path=str(cache_path),
        last_build_kind=_index_data.get("last_build_kind"),
        rebuild_revision=_index_data.get("rebuild_revision"),
        rebuild_duration_sec=_index_data.get("rebuild_duration"),
    )
    return {
        "project_id": int(project_id),
        "project_name": str(project_name),
        "scan_files": len(scanned),
        "scan_sec": scan_sec,
        "files": int(files_count),
        "blocks": int(blocks_count),
        "entities": int(entities_count),
        "cache_path": str(cache_path),
        "last_build_kind": _index_data.get("last_build_kind"),
        "rebuild_revision": _index_data.get("rebuild_revision"),
        "rebuild_duration": _index_data.get("rebuild_duration"),
    }
