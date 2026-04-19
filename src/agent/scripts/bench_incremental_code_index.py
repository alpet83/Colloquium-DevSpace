#!/usr/bin/env python3
"""Замер wall-clock maint code_index после правки одного файла (project exec + maint_enqueue).

Готовность: только GET /api/core/status → maint_pool.active_jobs без queued|running
code_index для project_id (надёжнее, чем опрос cache_only с rebuilt_now/entities).

Запуск вручную:

  PYTHONPATH=mcp-tools python agent/scripts/bench_incremental_code_index.py --project-id 5 --rounds 5

Рекомендуется не блокировать сессию MCP долгим HTTP: вынести процесс на хост и задать жёсткий TTL.

Через cq_process_ctl (host=true — машина, где крутится MCP; timeout — убийство subprocess снаружи):

  requests=[
    {"host": true, "action": "spawn", "args": {
      "command": "cmd /c cd /d P:\\\\opt\\\\docker\\\\cqds && set PYTHONPATH=mcp-tools&& python agent/scripts/bench_incremental_code_index.py --project-id 5 --rounds 3 --max-wait 180",
      "cwd": "P:\\\\opt\\\\docker\\\\cqds",
      "timeout": 240
    }},
    {"host": true, "action": "wait", "args": {
      "process_guid": "<из spawn>",
      "wait_timeout_ms": 250000,
      "wait_condition": "finished"
    }}
  ]

Дальше cq_process_ctl#io по process_guid — stdout. При зависании ядра сработает spawn.timeout (SIGKILL по истечении).

Переменные: CQDS_BASE_URL (по умолчанию http://127.0.0.1:8008), COLLOQUIUM_USER,
COLLOQUIUM_PASSWORD_FILE → mcp-tools/cqds_mcp_auth.secret
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path


def _bootstrap_mcp_tools_path() -> None:
    agent = Path(__file__).resolve().parents[1]
    mcp = agent.parent / "mcp-tools"
    for p in (mcp, agent):
        s = str(p)
        if s not in sys.path:
            sys.path.insert(0, s)


_bootstrap_mcp_tools_path()

from cqds_client import ColloquiumClient  # noqa: E402

_HTTP_WRAP = 18.0


def _mine_code_index(jobs: list, pid: int) -> list[dict]:
    out: list[dict] = []
    for j in jobs or []:
        if not isinstance(j, dict):
            continue
        if int(j.get("project_id") or 0) != pid:
            continue
        if str(j.get("kind") or "").lower() != "code_index":
            continue
        out.append(j)
    return out


def _any_active_job_for_project(jobs: list, pid: int) -> list[dict]:
    """Любая queued|running задача maint для project_id (уникальный слот на проект, не только code_index)."""
    out: list[dict] = []
    for j in jobs or []:
        if not isinstance(j, dict):
            continue
        if int(j.get("project_id") or 0) != pid:
            continue
        out.append(j)
    return out


async def _wait_no_active_maint_for_project(c: ColloquiumClient, pid: int, poll: float, max_wait: float) -> None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < max_wait:
        st = await asyncio.wait_for(c.get_core_status(), _HTTP_WRAP)
        jobs = (st.get("maint_pool") or {}).get("active_jobs") or []
        if not _any_active_job_for_project(jobs, pid):
            return
        await asyncio.sleep(poll)
    raise TimeoutError("maint still has active job for project (any kind)")


async def _wait_maint_code_index_round(
    c: ColloquiumClient, pid: int, poll: float, max_wait: float
) -> tuple[float, bool]:
    """Ждём, пока в active_jobs исчезнет наша code_index после того, как она там появилась (или быстрый no-op)."""
    t0 = time.monotonic()
    saw_mine = False
    while time.monotonic() - t0 < max_wait:
        st = await asyncio.wait_for(c.get_core_status(), _HTTP_WRAP)
        jobs = (st.get("maint_pool") or {}).get("active_jobs") or []
        mine = _mine_code_index(jobs, pid)
        if mine:
            saw_mine = True
        else:
            elapsed = time.monotonic() - t0
            if saw_mine or elapsed >= 0.25:
                return elapsed, saw_mine
        await asyncio.sleep(poll)
    raise TimeoutError(f"maint code_index not finished within {max_wait}s")


async def main_async() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("CQDS_BASE_URL", "http://127.0.0.1:8008"))
    ap.add_argument("--username", default=os.environ.get("COLLOQUIUM_USER", "copilot"))
    ap.add_argument("--password-file", default=os.environ.get("COLLOQUIUM_PASSWORD_FILE", ""))
    ap.add_argument("--project-id", type=int, default=int(os.environ.get("TEST_PROJECT_ID", "5")))
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--poll-interval", type=float, default=0.35)
    ap.add_argument(
        "--max-wait",
        type=float,
        default=120.0,
        help="Макс. секунд ожидания завершения одной задачи maint code_index (по core_status).",
    )
    ap.add_argument(
        "--total-budget-sec",
        type=float,
        default=0.0,
        help="Если >0 — общий бюджет секунд на весь скрипт; выход с кодом 11 при превышении.",
    )
    args = ap.parse_args()

    script_started = time.monotonic()
    budget = float(args.total_budget_sec or 0.0)

    pw_file = args.password_file or str(Path(__file__).resolve().parents[2] / "mcp-tools" / "cqds_mcp_auth.secret")
    password = Path(pw_file).read_text(encoding="utf-8").strip()

    c = ColloquiumClient(args.url, args.username, password)
    await asyncio.wait_for(c._ensure_login(), _HTTP_WRAP)
    pid = args.project_id

    def _over_budget() -> bool:
        return budget > 0 and (time.monotonic() - script_started) >= budget

    try:
        await _wait_no_active_maint_for_project(c, pid, args.poll_interval, min(120.0, args.max_wait))
    except TimeoutError as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        await c.aclose()
        return 4

    results: list[dict[str, object]] = []

    for r in range(1, args.rounds + 1):
        if _over_budget():
            print(json.dumps({"error": "total_budget_sec exceeded", "round": r}, ensure_ascii=False))
            await c.aclose()
            return 11

        cmd = f'echo "bench r{r} $(date +%s)" >> .cqds_index_bench.txt'
        ex = await asyncio.wait_for(c.exec_command(pid, cmd, timeout=30), _HTTP_WRAP + 5.0)
        if ex.get("status") != "success":
            print(json.dumps({"round": r, "exec_error": ex}, ensure_ascii=False))
            await c.aclose()
            return 2

        await asyncio.sleep(0.15)

        enq = await asyncio.wait_for(c.maint_enqueue(pid, "code_index"), _HTTP_WRAP)
        if enq.get("enqueue") == "duplicate":
            await _wait_no_active_maint_for_project(c, pid, args.poll_interval, args.max_wait)
            if _over_budget():
                print(json.dumps({"error": "total_budget_sec exceeded after duplicate wait", "round": r}, ensure_ascii=False))
                await c.aclose()
                return 11
            enq = await asyncio.wait_for(c.maint_enqueue(pid, "code_index"), _HTTP_WRAP)
            if enq.get("enqueue") == "duplicate":
                print(json.dumps({"round": r, "error": "enqueue_still_duplicate"}, ensure_ascii=False))
                await c.aclose()
                return 3

        try:
            wall, saw_mine = await _wait_maint_code_index_round(c, pid, args.poll_interval, args.max_wait)
        except TimeoutError as e:
            print(json.dumps({"round": r, "error": str(e)}, ensure_ascii=False))
            await c.aclose()
            return 5

        meta: dict[str, object] = {
            "round": r,
            "enqueue": enq,
            "wall_sec": round(wall, 3),
            "saw_code_index_in_active_jobs": saw_mine,
        }

        try:
            idx = await asyncio.wait_for(
                c.get_code_index(pid, timeout=30, client_http_max_sec=22.0, cache_only=True),
                30.0,
            )
            for k in ("rebuild_duration", "last_build_kind", "rebuild_revision", "packer_version"):
                if k in idx:
                    meta[k] = idx[k]
        except Exception as e:
            meta["cache_only_meta_error"] = repr(e)

        results.append(meta)
        print(json.dumps(meta, ensure_ascii=False, indent=2), flush=True)
        await asyncio.sleep(0.2)

    await c.aclose()

    walls = [float(x["wall_sec"]) for x in results]
    if walls:
        avg = sum(walls) / len(walls)
        print(
            json.dumps(
                {
                    "summary": {
                        "rounds": len(walls),
                        "wall_sec_mean": round(avg, 3),
                        "wall_sec_min": round(min(walls), 3),
                        "wall_sec_max": round(max(walls), 3),
                    }
                },
                ensure_ascii=False,
                indent=2,
            ),
            flush=True,
        )
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
