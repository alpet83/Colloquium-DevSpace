# Планировщик в процессе ядра (APScheduler): pg_dump, shell, учёт пропусков в журнале.
# Расписание по умолчанию для дампа — константы ниже + сид в БД; дальше только правка scheduled_jobs (SQL/API).
from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import globals
from managers.db import Database

log = globals.get_logger("core_scheduler")

_OUT_TAIL = 8000
_MISS_JOB_ID = "__miss_interval_check__"
_SCHED: CoreScheduler | None = None

# Сид только при пустой таблице + Postgres. Дальше — правка scheduled_jobs (SQL / будущий API / дамп БД).
_DEFAULT_PG_DUMP_CRON = "0 3 * * *"  # раз в сутки, 03:00 UTC
_DEFAULT_PG_DUMP_MISS_AFTER_SEC = 129_600  # 36 ч — допуск к «раз в сутки»
_MISS_CHECK_INTERVAL_MIN = 15
_PG_BACKUP_SCRIPT = "/app/postgres/backup_postgres.sh"


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


_CRON_FIELDS = re.compile(
    r"^(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*$"
)


def _cron_valid(expr: str) -> bool:
    expr = (expr or "").strip()
    if not expr:
        return False
    return _CRON_FIELDS.match(expr) is not None


class CoreScheduler:
    def __init__(self) -> None:
        self._ap: AsyncIOScheduler | None = None

    async def start(self) -> None:
        if self._ap is not None:
            return
        db = Database.get_database()
        self._maybe_seed_pg_backup(db)
        self._ap = AsyncIOScheduler(timezone=ZoneInfo("UTC"))
        interval_min = _MISS_CHECK_INTERVAL_MIN
        self._ap.add_job(
            self._miss_check_tick,
            "interval",
            minutes=interval_min,
            id=_MISS_JOB_ID,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        self.reload_from_db()
        self._ap.start()
        log.info(
            "CoreScheduler запущен (APScheduler), проверка пропусков каждые %d мин → журнал",
            interval_min,
        )

    async def stop(self) -> None:
        if self._ap is None:
            return
        try:
            self._ap.shutdown(wait=False)
        except Exception as e:
            log.warn("CoreScheduler shutdown: %s", str(e))
        self._ap = None
        log.info("CoreScheduler остановлен")

    def reload_from_db(self) -> None:
        if self._ap is None:
            return
        for job in list(self._ap.get_jobs()):
            if job.id != _MISS_JOB_ID:
                try:
                    self._ap.remove_job(job.id)
                except Exception:
                    pass
        db = Database.get_database()
        rows = db.fetch_all(
            "SELECT job_id, cron_expr, COALESCE(timezone, 'UTC') AS tz "
            "FROM scheduled_jobs WHERE enabled = 1"
        )
        for row in rows or []:
            job_id, cron_expr, tz_name = int(row[0]), str(row[1]), str(row[2] or "UTC")
            aid = f"sj_{job_id}"
            if not _cron_valid(cron_expr):
                log.warn("scheduled_jobs id=%d: некорректный cron_expr %r, задание не зарегистрировано", job_id, cron_expr)
                continue
            try:
                tz = ZoneInfo(tz_name)
            except ZoneInfoNotFoundError:
                log.warn("scheduled_jobs id=%d: неизвестная timezone %r, используем UTC", job_id, tz_name)
                tz = ZoneInfo("UTC")
            try:
                trigger = CronTrigger.from_crontab(cron_expr.strip(), timezone=tz)
            except ValueError as e:
                log.warn("scheduled_jobs id=%d: CronTrigger %r: %s", job_id, cron_expr, str(e))
                continue
            self._ap.add_job(
                self._run_job_safe,
                trigger,
                args=[job_id, "scheduled"],
                id=aid,
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            log.debug("Зарегистрировано задание %s cron=%r tz=%s", aid, cron_expr, tz_name)

    def _maybe_seed_pg_backup(self, db: Database) -> None:
        if not db.is_postgres():
            return
        exists = db.fetch_all("SELECT 1 FROM scheduled_jobs WHERE name = :n LIMIT 1", {"n": "pg_dump_daily"})
        if exists:
            return
        cron = _DEFAULT_PG_DUMP_CRON
        miss_sec = _DEFAULT_PG_DUMP_MISS_AFTER_SEC
        db.execute(
            """
            INSERT INTO scheduled_jobs (name, enabled, cron_expr, timezone, kind, config_json, miss_after_sec)
            VALUES (:name, 1, :cron, 'UTC', 'pg_dump_script', '{}', :miss)
            """,
            {"name": "pg_dump_daily", "cron": cron, "miss": miss_sec},
        )
        log.info(
            "Seeded scheduled_jobs: pg_dump_daily cron=%r UTC miss_after_sec=%d (изменить — через БД/API)",
            cron,
            miss_sec,
        )

    async def _miss_check_tick(self) -> None:
        try:
            await asyncio.to_thread(self._miss_check_sync)
        except Exception as e:
            log.excpt("miss_check_tick", e=e)

    def _miss_check_sync(self) -> None:
        db = Database.get_database()
        q = """
            SELECT j.job_id, j.name, j.miss_after_sec, j.created_at,
                   MAX(CASE WHEN r.status = 'success' THEN r.finished_at END) AS last_ok
            FROM scheduled_jobs j
            LEFT JOIN scheduled_job_runs r ON r.job_id = j.job_id
            WHERE j.enabled = 1
            GROUP BY j.job_id, j.name, j.miss_after_sec, j.created_at
        """
        rows = db.fetch_all(q)
        now = _utcnow()
        for row in rows or []:
            job_id, name, miss_raw, created_raw, last_ok_raw = (
                int(row[0]),
                str(row[1]),
                int(row[2]) if row[2] is not None else 0,
                row[3],
                row[4],
            )
            if miss_raw <= 0:
                continue
            last_ok = _parse_ts(last_ok_raw)
            created = _parse_ts(created_raw)
            ref = last_ok or created
            if ref is None:
                continue
            delta = (now - ref).total_seconds()
            if delta > miss_raw:
                log.warn(
                    "SCHEDULER_MISS: job_id=%d name=%r нет успешного завершения дольше %ds "
                    "(порог miss_after_sec=%d, с последнего успеха/создания прошло %.0fs)",
                    job_id,
                    name,
                    miss_raw,
                    miss_raw,
                    delta,
                )

    async def _run_job_safe(self, job_id: int, trigger_kind: str) -> None:
        try:
            await self._run_job(job_id, trigger_kind)
        except Exception as e:
            log.excpt("scheduled job_id=%d", job_id, e=e)

    async def _run_job(self, job_id: int, trigger_kind: str) -> None:
        db = Database.get_database()
        row = db.fetch_one(
            "SELECT kind, config_json FROM scheduled_jobs WHERE job_id = :id AND enabled = 1",
            {"id": job_id},
        )
        if not row:
            log.warn("_run_job: job_id=%d не найден или выключен", job_id)
            return
        kind, cfg_s = str(row[0]), str(row[1] or "{}")
        try:
            cfg = json.loads(cfg_s) if cfg_s else {}
        except json.JSONDecodeError:
            cfg = {}
        started = _utcnow()
        run_id = self._insert_run_start(db, job_id, started, trigger_kind)
        stdout_b, stderr_b, exit_code, status = await self._dispatch(kind, cfg)
        finished = _utcnow()
        out_t = (stdout_b or b"").decode("utf-8", errors="replace")[-_OUT_TAIL:]
        err_t = (stderr_b or b"").decode("utf-8", errors="replace")[-_OUT_TAIL:]
        self._insert_run_finish(db, run_id, finished, status, exit_code, out_t, err_t)
        if status != "success":
            log.warn(
                "scheduled job_id=%d kind=%s exit=%s status=%s stderr_tail=%s",
                job_id,
                kind,
                exit_code,
                status,
                err_t[:500],
            )

    def _insert_run_start(self, db: Database, job_id: int, started: datetime, trigger_kind: str) -> int | None:
        st_val = started if db.is_postgres() else started.isoformat()
        row = db.fetch_one(
            """
            INSERT INTO scheduled_job_runs (job_id, started_at, status, trigger_kind)
            VALUES (:jid, :st, 'running', :tk)
            RETURNING run_id
            """,
            {"jid": job_id, "st": st_val, "tk": trigger_kind},
        )
        return int(row[0]) if row and row[0] is not None else None

    def _insert_run_finish(
        self,
        db: Database,
        run_id: int | None,
        finished: datetime,
        status: str,
        exit_code: int | None,
        out_t: str,
        err_t: str,
    ) -> None:
        if run_id is None:
            return
        if db.is_postgres():
            db.execute(
                """
                UPDATE scheduled_job_runs
                SET finished_at = :fn, status = :st, exit_code = :ec,
                    stdout_tail = :so, stderr_tail = :se
                WHERE run_id = :rid
                """,
                {
                    "fn": finished,
                    "st": status,
                    "ec": exit_code,
                    "so": out_t,
                    "se": err_t,
                    "rid": run_id,
                },
            )
            return
        db.execute(
            """
            UPDATE scheduled_job_runs
            SET finished_at = :fn, status = :st, exit_code = :ec,
                stdout_tail = :so, stderr_tail = :se
            WHERE run_id = :rid
            """,
            {
                "fn": finished.isoformat(),
                "st": status,
                "ec": exit_code,
                "so": out_t,
                "se": err_t,
                "rid": run_id,
            },
        )

    async def _dispatch(self, kind: str, cfg: dict) -> tuple[bytes, bytes, int | None, str]:
        if kind == "pg_dump_script":
            return await self._run_subprocess(["/bin/sh", _PG_BACKUP_SCRIPT], os.environ.copy())
        if kind == "shell":
            argv = cfg.get("argv")
            if not isinstance(argv, list) or not all(isinstance(x, str) for x in argv):
                return b"", b"config_json.argv must be list[str]", None, "failed"
            env = os.environ.copy()
            extra = cfg.get("env")
            if isinstance(extra, dict):
                for k, v in extra.items():
                    if isinstance(k, str) and isinstance(v, str):
                        env[k] = v
            return await self._run_subprocess(argv, env)
        return b"", b"unknown kind %r" % kind, None, "failed"

    async def _run_subprocess(self, argv: list[str], env: dict) -> tuple[bytes, bytes, int | None, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await proc.communicate()
            code = proc.returncode
            ok = code == 0
            return stdout or b"", stderr or b"", code, "success" if ok else "failed"
        except Exception as e:
            return b"", str(e).encode("utf-8", errors="replace"), None, "failed"


async def start_core_scheduler() -> None:
    global _SCHED
    _SCHED = CoreScheduler()
    await _SCHED.start()


async def stop_core_scheduler() -> None:
    global _SCHED
    if _SCHED is not None:
        await _SCHED.stop()
        _SCHED = None


def get_scheduler() -> CoreScheduler | None:
    return _SCHED
