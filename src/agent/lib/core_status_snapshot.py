# core_status_snapshot.py — агрегат для /core/status (снаружи GET /api/core/status через nginx).
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import globals as g
from lib import maint_pool as mp
from managers.db import Database

log = g.get_logger("core_status")


def _read_maint_orchestrator_file() -> dict[str, Any]:
    path = Path(mp.MAINT_POOL_STATUS_PATH)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"error": f"read_failed: {exc}"}


def _maint_pool_active_jobs(db: Database, now: float) -> list[dict[str, Any]]:
    """Задачи maint-пула в queued|running: параметры, progress_json, секунды с started_at (running) или ожидания (queued)."""
    out: list[dict[str, Any]] = []
    try:
        mp.ensure_maint_pool_tables(db.engine)
        raw = db.fetch_all(
            """
            SELECT j.job_id, j.project_id, j.kind, j.status, j.priority, j.worker_id,
                   j.created_at, j.started_at, j.finished_at, j.lease_expires_at,
                   j.progress_json, j.error, p.project_name
            FROM maint_pool_jobs j
            LEFT JOIN projects p ON p.id = j.project_id
            WHERE j.status IN ('queued', 'running')
            ORDER BY j.priority DESC, j.job_id ASC
            LIMIT 64
            """
        )
        for row in raw or []:
            (
                job_id,
                pid,
                kind,
                status,
                pri,
                wid,
                created_at,
                started_at,
                _finished_at,
                lease_exp,
                prog_raw,
                err,
                pname,
            ) = row
            prog: dict[str, Any] | None = None
            if prog_raw:
                try:
                    prog = json.loads(str(prog_raw))
                except Exception:
                    prog = {"_parse_error": True, "raw": str(prog_raw)[:400]}
            st = str(status or "")
            started_i = int(started_at) if started_at is not None else None
            created_i = int(created_at) if created_at is not None else None
            busy_sec = round(now - float(started_i), 3) if started_i is not None and st == "running" else None
            queued_wait_sec = (
                round(now - float(created_i), 3) if created_i is not None and st == "queued" else None
            )
            lease_rem: int | None = None
            if lease_exp is not None:
                try:
                    lease_rem = max(0, int(lease_exp) - int(now))
                except Exception:
                    lease_rem = None
            err_s = str(err) if err else None
            if err_s and len(err_s) > 500:
                err_s = err_s[:500] + "…"
            out.append(
                {
                    "job_id": int(job_id),
                    "project_id": int(pid),
                    "project_name": str(pname) if pname else None,
                    "kind": str(kind or ""),
                    "status": st,
                    "priority": int(pri or 0),
                    "worker_id": str(wid) if wid else None,
                    "created_at_unix": created_i,
                    "started_at_unix": started_i,
                    "busy_sec": busy_sec,
                    "queued_wait_sec": queued_wait_sec,
                    "lease_expires_at_unix": int(lease_exp) if lease_exp is not None else None,
                    "lease_remaining_sec": lease_rem,
                    "progress": prog,
                    "error": err_s,
                }
            )
    except Exception as exc:
        log.debug("maint_pool active jobs: %s", str(exc))
    return out


def _maint_job_aggregates(db: Database) -> tuple[dict[str, int], dict[str, int]]:
    """(counts_by_status, running_counts_by_kind)."""
    by_status: dict[str, int] = {}
    by_kind_running: dict[str, int] = {}
    try:
        mp.ensure_maint_pool_tables(db.engine)
        rows = db.fetch_all("SELECT status, COUNT(*) AS c FROM maint_pool_jobs GROUP BY status")
        for row in rows or []:
            by_status[str(row[0])] = int(row[1])
        rows2 = db.fetch_all(
            """
            SELECT kind, COUNT(*) AS c FROM maint_pool_jobs
            WHERE status = 'running' GROUP BY kind
            """
        )
        for row in rows2 or []:
            by_kind_running[str(row[0] or "unknown")] = int(row[1])
    except Exception as exc:
        log.debug("maint_pool aggregate: %s", str(exc))
    return by_status, by_kind_running


def _nightly_restart_row(db: Database) -> dict[str, Any] | None:
    try:
        row = db.fetch_one(
            """
            SELECT enabled, cron_expr, timezone
            FROM scheduled_jobs
            WHERE name = :n
            LIMIT 1
            """,
            {"n": "core_nightly_restart"},
        )
        if not row:
            return None
        return {
            "enabled": bool(row[0]),
            "cron_expr": str(row[1] or ""),
            "timezone": str(row[2] or ""),
        }
    except Exception:
        return None


def _project_scans_running() -> int:
    state = getattr(g, "project_scan_state", None)
    if not isinstance(state, dict):
        return 0
    n = 0
    for v in state.values():
        if isinstance(v, dict) and v.get("running"):
            n += 1
    return n


def build_core_status_payload(maint_child: dict[str, Any]) -> dict[str, Any]:
    """maint_child: {pid, alive} из server.get_maint_child_state()."""
    db = Database.get_database()
    started = getattr(g, "CORE_SERVER_STARTED_AT", None)
    now = time.time()
    uptime = round(now - float(started), 3) if started is not None else None

    by_status, by_kind_running = _maint_job_aggregates(db)
    active_jobs = _maint_pool_active_jobs(db, now)
    orch = _read_maint_orchestrator_file()
    try:
        pool_cfg = int(os.environ.get("CORE_MAINT_POOL_WORKERS", "1"))
    except ValueError:
        pool_cfg = 1

    return {
        "server": {
            "started_at_unix": float(started) if started is not None else None,
            "uptime_sec": uptime,
        },
        "maint_child_process": dict(maint_child),
        "maint_pool": {
            "core_maint_pool_workers_env": pool_cfg,
            "orchestrator_snapshot_file": mp.MAINT_POOL_STATUS_PATH,
            "orchestrator": orch if orch else None,
            "jobs_by_status": by_status,
            "running_jobs_by_kind": by_kind_running,
            "active_jobs": active_jobs,
            "active_jobs_note": (
                "Только БД maint_pool_jobs (воркеры core_maint_loop; MCP по умолчанию ставит code_index через POST /project/maint_enqueue). "
                "Долгий синхронный GET /project/code_index только из клиента без maint_enqueue — в active_jobs не виден "
                "(состояние в процессе клиента: cq_files_ctl#index_job_status при локальной очереди MCP)."
            ),
        },
        "background": {
            "project_scans_running": _project_scans_running(),
            "maint_pool_running_total": int(by_status.get("running", 0)),
            "maint_pool_running_by_kind": by_kind_running,
        },
        "scheduled_nightly_restart": _nightly_restart_row(db),
    }
