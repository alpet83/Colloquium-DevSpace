# cqds_host_grep_jobs.py — фоновый host_fs (ripgrep): polling stdout + снимки для cq_fetch_result
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import shutil

from cqds_smart_grep_host import (
    build_ripgrep_argv,
    hit_dict_from_rg_json_line,
    _smart_grep_python,
)

LOGGER = logging.getLogger("cqds_host_grep_jobs")


def _poll_sec() -> float:
    try:
        v = float(os.environ.get("CQDS_HOST_GREP_POLL_SEC", "").strip() or "5")
        return max(0.5, min(v, 120.0))
    except ValueError:
        return 5.0


def _max_jobs() -> int:
    try:
        v = int(os.environ.get("CQDS_HOST_GREP_MAX_JOBS", "").strip() or "8")
        return max(1, min(v, 64))
    except ValueError:
        return 8


def _retain_after_complete_sec() -> float:
    try:
        v = float(os.environ.get("CQDS_HOST_GREP_JOB_RETAIN_SEC", "").strip() or "1800")
        return max(60.0, min(v, 86400.0))
    except ValueError:
        return 1800.0


@dataclass
class HostGrepJob:
    job_id: str
    host_path: str
    query: str
    mode: str
    profile: str
    include_glob: list[str] | None
    is_regex: bool
    case_sensitive: bool
    max_results: int
    context_lines: int
    timeout_sec: int
    workers: int
    page_size: int
    hits: list[dict[str, Any]] = field(default_factory=list)
    complete: bool = False
    truncated: bool = False
    error: str | None = None
    engine: str = "ripgrep"
    snapshot_seq: int = 0
    pages_completed: int = 0
    created_monotonic: float = field(default_factory=time.monotonic)
    completed_monotonic: float | None = None
    proc: asyncio.subprocess.Process | None = None
    task: asyncio.Task[None] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_jobs: dict[str, HostGrepJob] = {}
_registry_lock = asyncio.Lock()


async def start_host_grep_job(
    host_path: str,
    query: str,
    *,
    mode: str,
    profile: str,
    include_glob: list[str] | None,
    is_regex: bool,
    case_sensitive: bool,
    max_results: int,
    context_lines: int,
    timeout_sec: int,
    workers: int,
    page_size: int,
) -> str:
    async with _registry_lock:
        if len(_jobs) >= _max_jobs():
            raise ValueError(
                f"Too many concurrent host_grep jobs (max {_max_jobs()}); "
                "wait for completion or raise CQDS_HOST_GREP_MAX_JOBS"
            )
        jid = uuid.uuid4().hex
        job = HostGrepJob(
            job_id=jid,
            host_path=host_path,
            query=query,
            mode=mode,
            profile=profile,
            include_glob=include_glob,
            is_regex=is_regex,
            case_sensitive=case_sensitive,
            max_results=max(1, min(int(max_results), 10000)),
            context_lines=context_lines,
            timeout_sec=timeout_sec,
            workers=workers,
            page_size=max(1, min(int(page_size), 500)),
        )
        _jobs[jid] = job

    task = asyncio.create_task(_run_job(job), name=f"hostgrep_{jid[:8]}")
    job.task = task

    def _done(t: asyncio.Task[None]) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            LOGGER.exception("host_grep job %s failed", jid)
            asyncio.create_task(_mark_job_failed(job, exc))

    task.add_done_callback(_done)
    return jid


async def _mark_job_failed(job: HostGrepJob, exc: BaseException) -> None:
    async with job.lock:
        if not job.complete:
            job.error = str(exc)
            job.complete = True
            job.completed_monotonic = time.monotonic()
            job.snapshot_seq += 1


async def _run_job(job: HostGrepJob) -> None:
    root = Path(job.host_path).expanduser().resolve()
    if not root.is_dir():
        async with job.lock:
            job.error = f"host_path is not a directory: {root}"
            job.complete = True
            job.completed_monotonic = time.monotonic()
            job.snapshot_seq += 1
        return

    rg = shutil.which("rg")
    if rg:
        job.engine = "ripgrep"
        await _run_ripgrep_streaming(job, root, rg)
    else:
        job.engine = "python_threads"
        await _run_python_job(job, root)


async def _run_python_job(job: HostGrepJob, root: Path) -> None:
    loop = asyncio.get_event_loop()

    def _sync() -> dict[str, Any]:
        return _smart_grep_python(
            root,
            job.query,
            mode=job.mode,
            profile=job.profile,
            include_glob=job.include_glob,
            is_regex=job.is_regex,
            case_sensitive=job.case_sensitive,
            max_results=job.max_results,
            context_lines=job.context_lines,
            workers=job.workers,
        )

    try:
        res = await asyncio.wait_for(
            loop.run_in_executor(None, _sync),
            timeout=float(job.timeout_sec),
        )
    except asyncio.TimeoutError:
        async with job.lock:
            job.error = f"python host_grep timed out after {job.timeout_sec}s"
            job.complete = True
            job.completed_monotonic = time.monotonic()
            job.snapshot_seq += 1
        return
    except Exception as e:
        async with job.lock:
            job.error = str(e)
            job.complete = True
            job.completed_monotonic = time.monotonic()
            job.snapshot_seq += 1
        return

    async with job.lock:
        job.hits = list(res.get("hits") or [])
        job.truncated = bool(res.get("truncated"))
        job.complete = True
        job.completed_monotonic = time.monotonic()
        job.snapshot_seq += 1


async def _run_ripgrep_streaming(job: HostGrepJob, root: Path, rg_exe: str) -> None:
    cmd = build_ripgrep_argv(
        rg_exe,
        root,
        job.query,
        mode=job.mode,
        profile=job.profile,
        include_glob=job.include_glob,
        is_regex=job.is_regex,
        case_sensitive=job.case_sensitive,
        context_lines=job.context_lines,
    )
    poll = _poll_sec()
    proc: asyncio.subprocess.Process | None = None
    stderr_task: asyncio.Task[bytes] | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(root),
        )
        job.proc = proc
        assert proc.stdout is not None
        if proc.stderr is not None:
            stderr_task = asyncio.create_task(proc.stderr.read())

        async def _pump() -> None:
            assert proc is not None
            stdout = proc.stdout
            assert stdout is not None
            while True:
                try:
                    line_b = await asyncio.wait_for(stdout.readline(), timeout=poll)
                except asyncio.TimeoutError:
                    async with job.lock:
                        if job.complete:
                            return
                        job.snapshot_seq += 1
                    continue
                if not line_b:
                    break
                line = line_b.decode("utf-8", errors="replace").rstrip("\n\r")
                h = hit_dict_from_rg_json_line(line, root, job.profile, job.query)
                if h is None:
                    continue
                async with job.lock:
                    if job.complete:
                        return
                    job.hits.append(h)
                    ps = job.page_size
                    if ps > 0 and len(job.hits) > 0 and len(job.hits) % ps == 0:
                        job.pages_completed = len(job.hits) // ps
                        job.snapshot_seq += 1
                    if len(job.hits) >= job.max_results:
                        job.truncated = True
                        job.complete = True
                        job.completed_monotonic = time.monotonic()
                        job.snapshot_seq += 1
                        try:
                            proc.kill()
                        except ProcessLookupError:
                            pass
                        return

        await asyncio.wait_for(_pump(), timeout=float(job.timeout_sec))
        if proc.returncode is None:
            await asyncio.wait_for(proc.wait(), timeout=60.0)
        stderr_b = b""
        if stderr_task is not None:
            stderr_b = await stderr_task
        rc = proc.returncode
        if rc is not None and rc not in (0, 1, 2):
            async with job.lock:
                if not job.error:
                    err = stderr_b.decode("utf-8", errors="replace")[:500]
                    job.error = f"ripgrep exited {rc}: {err or 'no stderr'}"
    except asyncio.TimeoutError:
        async with job.lock:
            if not job.error:
                job.error = f"ripgrep timed out after {job.timeout_sec}s"
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
    finally:
        if stderr_task is not None and not stderr_task.done():
            try:
                await asyncio.wait_for(stderr_task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                stderr_task.cancel()
                try:
                    await stderr_task
                except asyncio.CancelledError:
                    pass
        async with job.lock:
            if not job.complete:
                job.complete = True
                job.completed_monotonic = time.monotonic()
            job.snapshot_seq += 1
        job.proc = None


async def take_host_grep_snapshot(job_id: str) -> dict[str, Any] | None:
    """Копия состояния задачи для ответа MCP; после истечения retain удаляет job и возвращает None."""
    jid = job_id.strip()
    async with _registry_lock:
        job = _jobs.get(jid)
    if job is None:
        return None

    async with job.lock:
        hits = list(job.hits)
        complete = job.complete
        truncated = job.truncated
        err = job.error
        snap = job.snapshot_seq
        eng = job.engine
        hp = job.host_path
        mode = job.mode
        profile = job.profile
        query = job.query
        is_regex = job.is_regex
        case_sensitive = job.case_sensitive
        completed_at = job.completed_monotonic

    now = time.monotonic()
    if complete and completed_at is not None and now - completed_at > _retain_after_complete_sec():
        async with _registry_lock:
            _jobs.pop(jid, None)
        return None

    return {
        "job_id": jid,
        "hits": hits,
        "scan_complete": complete,
        "truncated": truncated,
        "error": err,
        "snapshot_seq": snap,
        "engine": eng,
        "host_path": str(Path(hp).expanduser().resolve()),
        "mode": mode,
        "profile": profile,
        "query": query,
        "is_regex": is_regex,
        "case_sensitive": case_sensitive,
        "search_mode": "host_fs",
    }


def host_grep_poll_hint_sec() -> float:
    return _poll_sec()
