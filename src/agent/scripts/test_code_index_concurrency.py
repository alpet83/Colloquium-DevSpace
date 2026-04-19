#!/usr/bin/env python3
"""Проверка: пока идёт долгий GET /api/project/code_index, отвечают ли лёгкие запросы к ядру.

Два независимых httpx.AsyncClient (две сессии) + asyncio.gather.
«Асинхронный ребилд» на стороне MCP — это отдельный процесс; здесь измеряется поведение самого ядра под нагрузкой code_index.

Запуск с хоста (пример):
  PYTHONPATH=/path/to/mcp-tools python /path/to/this/script.py --url http://localhost:8008 --project-id 5
или из каталога mcp-tools с установленным httpx.
"""
from __future__ import annotations

import argparse
import asyncio
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


async def _poll_light(
    base_url: str,
    username: str,
    password: str,
    project_id: int,
    stop: asyncio.Event,
    label: str,
) -> list[tuple[float, str, int, float]]:
    """Пока stop не set — GET /docs и /api/project/status, логирует задержки."""
    out: list[tuple[float, str, int, float]] = []
    c = ColloquiumClient(base_url, username, password)
    await c._ensure_login()
    try:
        while not stop.is_set():
            t0 = time.perf_counter()
            r = await c._client.get("/docs", timeout=15.0)
            dt_ms = (time.perf_counter() - t0) * 1000.0
            out.append((time.perf_counter(), f"{label}/docs", r.status_code, dt_ms))
            if dt_ms > 500:
                print(f"[{label}] /docs {r.status_code} {dt_ms:.0f} ms", flush=True)

            t0 = time.perf_counter()
            r2 = await c._client.get(
                "/api/project/status",
                params={"project_id": project_id},
                timeout=30.0,
            )
            dt2 = (time.perf_counter() - t0) * 1000.0
            out.append((time.perf_counter(), f"{label}/status", r2.status_code, dt2))
            if dt2 > 500:
                print(f"[{label}] /api/project/status {r2.status_code} {dt2:.0f} ms", flush=True)

            await asyncio.sleep(0.35)
        return out
    finally:
        await c.aclose()


async def _heavy_code_index(
    base_url: str,
    username: str,
    password: str,
    project_id: int,
    timeout: int,
) -> tuple[int, float, str | None]:
    c = ColloquiumClient(base_url, username, password)
    await c._ensure_login()
    t0 = time.perf_counter()
    err: str | None = None
    n_entities = -1
    try:
        data = await c.get_code_index(project_id, timeout=timeout)
        ent = data.get("entities")
        if isinstance(ent, list):
            n_entities = len(ent)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    elapsed = time.perf_counter() - t0
    await c.aclose()
    return n_entities, elapsed, err


async def main_async() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("CQDS_BASE_URL", "http://localhost:8008"))
    ap.add_argument("--username", default=os.environ.get("COLLOQUIUM_USER", "copilot"))
    ap.add_argument("--password-file", default=os.environ.get("COLLOQUIUM_PASSWORD_FILE", ""))
    ap.add_argument("--project-id", type=int, default=int(os.environ.get("TEST_PROJECT_ID", "5")))
    ap.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args()

    pw_file = args.password_file or str(Path(__file__).resolve().parents[2] / "mcp-tools" / "cqds_mcp_auth.secret")
    password = Path(pw_file).read_text(encoding="utf-8").strip()

    stop = asyncio.Event()
    poll_task = asyncio.create_task(
        _poll_light(args.url, args.username, password, args.project_id, stop, "B"),
    )
    await asyncio.sleep(0.15)
    print(
        f"START heavy code_index project_id={args.project_id} (client A); "
        f"parallel polls client B -> {args.url}",
        flush=True,
    )
    t_wall = time.perf_counter()
    heavy = asyncio.create_task(
        _heavy_code_index(args.url, args.username, password, args.project_id, args.timeout),
    )
    n_ent, elapsed, err = await heavy
    stop.set()
    samples = await poll_task
    wall = time.perf_counter() - t_wall

    print(f"\nDONE code_index wall={wall:.1f}s client_elapsed={elapsed:.1f}s entities={n_ent}", flush=True)
    if err:
        print(f"ERROR: {err}", flush=True)

    slow = [x for x in samples if x[3] > 2000]
    if samples:
        ms_docs = [x[3] for x in samples if "/docs" in x[1]]
        ms_st = [x[3] for x in samples if "/status" in x[1]]
        if ms_docs:
            print(
                f"Poll B: /docs n={len(ms_docs)} max={max(ms_docs):.0f}ms p95~{sorted(ms_docs)[int(len(ms_docs)*0.95)-1]:.0f}ms",
                flush=True,
            )
        if ms_st:
            print(
                f"Poll B: /status n={len(ms_st)} max={max(ms_st):.0f}ms p95~{sorted(ms_st)[int(len(ms_st)*0.95)-1]:.0f}ms",
                flush=True,
            )
    if slow:
        print(f"Poll samples with latency > 2000 ms (count={len(slow)}):", flush=True)
        for _ts, path, code, ms in slow[:12]:
            print(f"  {path} {code} {ms:.0f} ms", flush=True)
    elif samples:
        print("No poll samples > 2000 ms while index ran (light requests stayed responsive).", flush=True)

    return 0 if not err else 1


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
