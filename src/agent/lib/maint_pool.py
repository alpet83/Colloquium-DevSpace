# maint_pool.py — очередь maint-задач: не более одной активной (queued+running) на project_id, lease, stdout-прогресс.
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

PROGRESS_PREFIX = "MAINT_POOL_PROGRESS "

# Снимок оркестратора пула (пишет core_maint_loop); читает GET /api/core/status.
MAINT_POOL_STATUS_PATH = os.environ.get("MAINT_POOL_STATUS_PATH", "/app/data/maint_pool_status.json")


def emit_progress(
    job_id: int,
    project_id: int,
    stage: str,
    *,
    worker_id: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    """Строка в stdout для внешнего watchdog (grep MAINT_POOL_PROGRESS)."""
    rec: dict[str, Any] = {
        "ts": time.time(),
        "job_id": job_id,
        "project_id": project_id,
        "stage": stage,
        "worker_id": worker_id,
    }
    if extra:
        rec.update(extra)
    sys.stdout.write(PROGRESS_PREFIX + json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    try:
        sys.stdout.flush()
    except Exception:
        pass


def _is_pg(engine) -> bool:
    return engine.url.get_backend_name().startswith("postgresql")


def ensure_maint_pool_tables(engine) -> None:
    """Создаёт таблицы и частичный уникальный индекс «один активный job на проект»."""
    pg = _is_pg(engine)
    with engine.begin() as conn:
        if pg:
            conn.execute(
                text("""
                CREATE TABLE IF NOT EXISTS maint_pool_jobs (
                    job_id BIGSERIAL PRIMARY KEY,
                    project_id BIGINT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'reconcile_tick',
                    priority INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'queued',
                    worker_id TEXT,
                    lease_expires_at BIGINT,
                    progress_json TEXT,
                    error TEXT,
                    created_at BIGINT NOT NULL,
                    started_at BIGINT,
                    finished_at BIGINT
                )
                """)
            )
        else:
            conn.execute(
                text("""
                CREATE TABLE IF NOT EXISTS maint_pool_jobs (
                    job_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'reconcile_tick',
                    priority INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'queued',
                    worker_id TEXT,
                    lease_expires_at INTEGER,
                    progress_json TEXT,
                    error TEXT,
                    created_at INTEGER NOT NULL,
                    started_at INTEGER,
                    finished_at INTEGER
                )
                """)
            )
        conn.execute(
            text(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_maint_pool_one_active_per_project
                ON maint_pool_jobs (project_id)
                WHERE status IN ('queued', 'running')
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS idx_maint_pool_status_job
                ON maint_pool_jobs (status, job_id)
                """
            )
        )
        if pg:
            conn.execute(
                text("""
                CREATE TABLE IF NOT EXISTS maint_scan_cooldown (
                    project_id BIGINT PRIMARY KEY,
                    last_mono DOUBLE PRECISION NOT NULL
                )
                """)
            )
        else:
            conn.execute(
                text("""
                CREATE TABLE IF NOT EXISTS maint_scan_cooldown (
                    project_id INTEGER PRIMARY KEY,
                    last_mono REAL NOT NULL
                )
                """)
            )


def enqueue_reconcile_tick_jobs(engine, projects: list[tuple[int, str, float]]) -> int:
    """Ставит в очередь reconcile_tick для выбранных проектов."""
    if not projects:
        return 0
    inserted = 0
    for project_id, _name, _score in projects:
        if enqueue_maint_job(engine, int(project_id), "reconcile_tick", priority=0) == "queued":
            inserted += 1
    return inserted


def enqueue_maint_job(engine, project_id: int, kind: str, *, priority: int | None = None) -> str:
    """
    Одна активная задача на project_id (queued|running). kind: reconcile_tick | code_index.
    code_index по умолчанию priority=10, reconcile_tick=0 — выше при claim.
    Возвращает 'queued' или 'duplicate'.
    """
    k = str(kind or "").strip().lower()
    if k not in ("reconcile_tick", "code_index"):
        raise ValueError(f"unsupported maint job kind: {kind!r}")
    now_ts = int(time.time())
    pri = int(priority) if priority is not None else (10 if k == "code_index" else 0)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO maint_pool_jobs (project_id, kind, status, created_at, priority)
                    VALUES (:pid, :kind, 'queued', :ts, :pri)
                    """
                ),
                {"pid": int(project_id), "kind": k, "ts": now_ts, "pri": pri},
            )
        return "queued"
    except IntegrityError:
        return "duplicate"


def code_index_active(engine, project_id: int) -> bool:
    """В maint_pool_jobs есть queued|running задача ``kind=code_index`` для ``project_id``."""
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT 1 FROM maint_pool_jobs
                WHERE project_id = :pid AND kind = 'code_index'
                  AND status IN ('queued', 'running')
                LIMIT 1
                """
            ),
            {"pid": int(project_id)},
        ).fetchone()
    return row is not None


def claim_next_job(engine, worker_id: str, lease_sec: int) -> dict[str, Any] | None:
    """Атомарно берёт одну queued-задачу; проект не пересекается с уже running."""
    now_ts = int(time.time())
    lease_until = now_ts + max(30, int(lease_sec))
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE maint_pool_jobs
                SET status = 'queued', worker_id = NULL, lease_expires_at = NULL
                WHERE status = 'running'
                  AND lease_expires_at IS NOT NULL
                  AND lease_expires_at < :now
                """
            ),
            {"now": now_ts},
        )
        row = conn.execute(
            text(
                """
                SELECT job_id, project_id, kind FROM maint_pool_jobs
                WHERE status = 'queued'
                  AND project_id NOT IN (
                      SELECT project_id FROM maint_pool_jobs WHERE status = 'running'
                  )
                ORDER BY priority DESC, job_id ASC
                LIMIT 1
                """
            )
        ).fetchone()
        if not row:
            return None
        job_id, project_id, kind = int(row[0]), int(row[1]), str(row[2] or "reconcile_tick")
        prog = json.dumps({"stage": "claimed", "ts": now_ts}, ensure_ascii=False)
        res = conn.execute(
            text(
                """
                UPDATE maint_pool_jobs
                SET status = 'running',
                    worker_id = :w,
                    lease_expires_at = :le,
                    started_at = COALESCE(started_at, :now),
                    progress_json = :prog
                WHERE job_id = :jid AND status = 'queued'
                """
            ),
            {
                "w": worker_id,
                "le": lease_until,
                "now": now_ts,
                "jid": job_id,
                "prog": prog,
            },
        )
        try:
            ok = int(res.rowcount or 0) == 1
        except Exception:
            ok = True
        if not ok:
            return None
        return {"job_id": job_id, "project_id": project_id, "kind": kind}


def touch_job_lease(engine, job_id: int, lease_sec: int) -> None:
    """Продлевает lease (heartbeat длинной задачи)."""
    now_ts = int(time.time())
    lease_until = now_ts + max(30, int(lease_sec))
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE maint_pool_jobs
                SET lease_expires_at = :le, progress_json = :prog
                WHERE job_id = :jid AND status = 'running'
                """
            ),
            {
                "le": lease_until,
                "jid": int(job_id),
                "prog": json.dumps({"stage": "heartbeat", "ts": now_ts}, ensure_ascii=False),
            },
        )


def update_job_progress_db(engine, job_id: int, stage: str, extra: dict[str, Any] | None = None) -> None:
    payload = {"stage": stage, "ts": int(time.time())}
    if extra:
        payload.update(extra)
    blob = json.dumps(payload, ensure_ascii=False, default=str)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE maint_pool_jobs SET progress_json = :p WHERE job_id = :jid AND status = 'running'"),
            {"p": blob, "jid": int(job_id)},
        )


def complete_job(engine, job_id: int) -> None:
    now_ts = int(time.time())
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE maint_pool_jobs
                SET status = 'done', finished_at = :ts, lease_expires_at = NULL, progress_json = :p
                WHERE job_id = :jid
                """
            ),
            {
                "ts": now_ts,
                "jid": int(job_id),
                "p": json.dumps({"stage": "done", "ts": now_ts}, ensure_ascii=False),
            },
        )


def fail_job(engine, job_id: int, err: str) -> None:
    now_ts = int(time.time())
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE maint_pool_jobs
                SET status = 'error', finished_at = :ts, lease_expires_at = NULL,
                    error = :err, progress_json = :p
                WHERE job_id = :jid
                """
            ),
            {
                "ts": now_ts,
                "jid": int(job_id),
                "err": (err or "")[:4000],
                "p": json.dumps({"stage": "error", "ts": now_ts}, ensure_ascii=False),
            },
        )


def default_worker_id() -> str:
    host = (os.environ.get("HOSTNAME") or os.environ.get("COMPUTERNAME") or "host").strip()
    return f"{host}-{os.getpid()}"


def pool_lease_sec() -> int:
    try:
        return max(60, int(os.environ.get("CORE_MAINT_POOL_LEASE_SEC", "600")))
    except ValueError:
        return 600


def pool_progress_interval_sec() -> float:
    try:
        return max(2.0, float(os.environ.get("CORE_MAINT_POOL_PROGRESS_SEC", "8")))
    except ValueError:
        return 8.0
