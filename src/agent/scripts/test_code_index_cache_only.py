#!/usr/bin/env python3
"""Проверка GET /api/project/code_index?cache_only=true (try-retrieve, rebuilt_now).

Сценарий: снимок cache_only → maint_enqueue(code_index) → опрос cache_only, пока не исчезнет
rebuilt_now или не истечёт таймаут (ожидаем флаг, пока воркер держит задачу в queued/running).

Запуск (из корня репозитория или с PYTHONPATH к mcp-tools):

  PYTHONPATH=mcp-tools python agent/scripts/test_code_index_cache_only.py --project-id 5

Переменные: CQDS_BASE_URL, COLLOQUIUM_USER, COLLOQUIUM_PASSWORD_FILE, TEST_PROJECT_ID.
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


def _summarize_cache_payload(d: dict) -> str:
    keys = sorted(d.keys())
    n_ent = len(d.get("entities") or []) if isinstance(d.get("entities"), list) else "?"
    rebuilt = d.get("rebuilt_now")
    return f"keys={keys[:12]}{'...' if len(keys) > 12 else ''} entities_len={n_ent} rebuilt_now={rebuilt!r}"


async def main_async() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("CQDS_BASE_URL", "http://localhost:8008"))
    ap.add_argument("--username", default=os.environ.get("COLLOQUIUM_USER", "copilot"))
    ap.add_argument("--password-file", default=os.environ.get("COLLOQUIUM_PASSWORD_FILE", ""))
    ap.add_argument("--project-id", type=int, default=int(os.environ.get("TEST_PROJECT_ID", "5")))
    ap.add_argument("--poll-sec", type=float, default=90.0, help="Макс. секунд опроса после enqueue")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument(
        "--check-registry-pending",
        action="store_true",
        help="POST /api/core/background_tasks (pending code_index) и один GET cache_only — ожидаем rebuilt_now=1 при наличии файлового кеша.",
    )
    args = ap.parse_args()

    pw_file = args.password_file or str(Path(__file__).resolve().parents[2] / "mcp-tools" / "cqds_mcp_auth.secret")
    password = Path(pw_file).read_text(encoding="utf-8").strip()

    c = ColloquiumClient(args.url, args.username, password)
    await c._ensure_login()
    pid = args.project_id

    if args.check_registry_pending:
        r = await c._client.post(
            "/api/core/background_tasks",
            json={"kind": "code_index", "meta": {"project_id": pid}},
            timeout=30.0,
        )
        r.raise_for_status()
        task = r.json()
        print(f"[registry] создана pending-задача: {json.dumps(task, ensure_ascii=False)}", flush=True)
        after = await c.get_code_index(pid, timeout=30, client_http_max_sec=25.0, cache_only=True)
        rb = after.get("rebuilt_now")
        print(f"[cache_only] после pending: {_summarize_cache_payload(after)}", flush=True)
        if rb != 1:
            print(
                f"WARNING: ожидали rebuilt_now=1 при незавершённой задаче реестра и существующем кеше; получили {rb!r}",
                flush=True,
            )
        await c.aclose()
        return 0 if rb == 1 else 2

    # 1) Снимок до постановки в очередь
    try:
        before = await c.get_code_index(pid, timeout=30, client_http_max_sec=25.0, cache_only=True)
        print(f"[cache_only] до enqueue: {_summarize_cache_payload(before)}", flush=True)
    except Exception as e:
        print(f"[cache_only] до enqueue: ошибка {e!r} (допустимо, если кеша ещё не было)", flush=True)

    # 2) Очередь maint code_index
    enq = await c.maint_enqueue(pid, "code_index")
    print(f"[maint_enqueue] {json.dumps(enq, ensure_ascii=False)}", flush=True)

    # 3) Опрос cache_only
    t0 = time.monotonic()
    last_rebuilt: object = None
    while time.monotonic() - t0 < args.poll_sec:
        try:
            payload = await c.get_code_index(pid, timeout=30, client_http_max_sec=25.0, cache_only=True)
            rebuilt = payload.get("rebuilt_now")
            if rebuilt != last_rebuilt:
                print(
                    f"[cache_only] t={time.monotonic() - t0:.1f}s {_summarize_cache_payload(payload)}",
                    flush=True,
                )
                last_rebuilt = rebuilt
            if rebuilt is None and isinstance(payload.get("entities"), list) and len(payload["entities"]) > 0:
                print("[cache_only] индекс из файла без rebuilt_now — задача maint, вероятно, завершена.", flush=True)
                break
            if rebuilt is None and "entities" in payload:
                # кеш есть, ребилда нет
                break
        except Exception as e:
            print(f"[cache_only] poll error: {e!r}", flush=True)
        await asyncio.sleep(args.interval)

    await c.aclose()
    print("Готово.", flush=True)
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
