#!/usr/bin/env python3
"""Один чат, только текстовые посты + #debug_bypass — чтобы ловить DELTA_SAFE в context_cache_metrics.

Без вложений и файлов: non_post_sig стабилен, растёт только хвост постов.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import random
import sys
import time
import traceback
from pathlib import Path
from typing import Any

_THIS = Path(__file__).resolve()
_MCP_TOOLS = _THIS.parents[1]
if str(_MCP_TOOLS) not in sys.path:
    sys.path.insert(0, str(_MCP_TOOLS))

from cqds_client import ColloquiumClient
import cqds_credentials as cq_cred
from cqds_helpers import _is_progress_stub

DEBUG_BYPASS_TAG = "#debug_bypass"


def _norm_actor(name: str) -> str:
    return name.strip().lstrip("@").lower()


def _latest_actor_message(resp: dict, expected_user: str) -> str | None:
    posts = resp.get("posts")
    if not isinstance(posts, dict):
        return None
    vals = [p for p in posts.values() if isinstance(p, dict)]
    if not vals:
        return None
    vals.sort(key=lambda p: int(p.get("id", 0) or 0))
    target = _norm_actor(expected_user)
    for p in reversed(vals):
        if str(p.get("user_name", "")).lower() == target:
            return str(p.get("message", "") or "")
    return None


def _extract_latest_message(resp: dict, expected_user: str) -> str:
    msg = _latest_actor_message(resp, expected_user)
    if msg and not _is_progress_stub(msg):
        return msg
    return ""


async def _wait_reply(
    client: ColloquiumClient,
    chat_id: int,
    timeout_sec: int,
    expected_actor: str,
) -> dict:
    deadline = time.monotonic() + max(1, timeout_sec)
    last: dict[str, Any] = {"chat_history": "no changes"}
    actor = _norm_actor(expected_actor)

    while time.monotonic() < deadline:
        rem = max(1.0, deadline - time.monotonic())
        poll_cap = 60.0 if timeout_sec >= 120 else 15.0
        resp = await client.get_reply(chat_id, wait=True, timeout=min(rem, poll_cap))
        if not isinstance(resp, dict):
            continue
        last = resp
        latest = _latest_actor_message(resp, actor)
        if latest is not None and latest.strip() and not _is_progress_stub(latest):
            return resp

    return last


async def _retry(coro_factory, retries: int, base_sleep: float, tag: str):
    for attempt in range(retries + 1):
        try:
            return await coro_factory()
        except Exception as exc:  # noqa: BLE001
            text = str(exc).lower()
            if attempt >= retries:
                raise
            if "429" in text or "rate" in text or "timeout" in text:
                delay = base_sleep * (2**attempt) + random.uniform(0.0, 0.25)
            else:
                delay = base_sleep + random.uniform(0.0, 0.35)
            print(f"[retry] {tag} attempt={attempt + 1} delay={delay:.2f}s err={exc!s}", flush=True)
            await asyncio.sleep(delay)
    raise RuntimeError("unreachable")


def _iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


async def run(args: argparse.Namespace) -> dict[str, Any]:
    password, _ = cq_cred.resolve_password(args.password, args.password_file)
    if not password:
        raise RuntimeError("Password required.")

    client = ColloquiumClient(base_url=args.url, username=args.username, password=password)
    started = int(time.time())
    report: dict[str, Any] = {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "base_url": args.url,
        "project_id": args.project_id,
        "actor_mention": args.actor_mention,
        "turns": args.turns,
        "debug_bypass": True,
        "results": [],
    }

    try:
        await _retry(
            lambda: client.select_project(args.project_id),
            args.retries,
            args.base_sleep,
            "select_project",
        )
        desc = f"cache-delta-safe:{_iso_now()}:project{args.project_id}"
        if (args.chat_name_prefix or "").strip():
            desc = f"{args.chat_name_prefix.strip()}-{desc}"
        chat_id = await _retry(
            lambda: client.create_chat(desc),
            args.retries,
            args.base_sleep,
            "create_chat",
        )
        report["chat_id"] = chat_id
        report["description"] = desc

        actor = _norm_actor(args.actor_mention)
        for i in range(1, int(args.turns) + 1):
            body = (
                f"{args.actor_mention} turn {i}/{args.turns}: cache delta-safe probe; "
                f"reply minimal. {DEBUG_BYPASS_TAG}"
            )
            await _retry(
                lambda b=body: client.post_message(chat_id, b),
                args.retries,
                args.base_sleep,
                f"post_message:{chat_id}:turn{i}",
            )
            reply = await _retry(
                lambda: _wait_reply(client, chat_id, args.wait_timeout, args.actor_mention),
                args.retries,
                args.base_sleep,
                f"wait_reply:{chat_id}:turn{i}",
            )
            excerpt = _extract_latest_message(reply, args.actor_mention)[:400]
            report["results"].append({"turn": i, "ok": bool(excerpt), "reply_excerpt": excerpt})
            await asyncio.sleep(max(0.0, float(args.per_turn_sleep)))

        report["status"] = "ok"
        report["started_at"] = started
        report["finished_at"] = int(time.time())
        report["elapsed_seconds"] = report["finished_at"] - started
    except BaseException as exc:
        report["status"] = "error"
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["error_traceback"] = traceback.format_exc()
        raise
    finally:
        await client.aclose()

    return report


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser(
        description="Один чат, N текстовых постов с #debug_bypass — метрики DELTA_SAFE vs FULL."
    )
    ap.add_argument("--url", default=os.environ.get("COLLOQUIUM_URL", "http://localhost:8008"))
    ap.add_argument("--username", default=os.environ.get("COLLOQUIUM_USERNAME", "copilot"))
    ap.add_argument("--password", default=os.environ.get("COLLOQUIUM_PASSWORD", ""))
    ap.add_argument("--password-file", default=cq_cred.default_password_file_for_cli())
    ap.add_argument("--project-id", type=int, default=2)
    ap.add_argument("--actor-mention", default="@grok4f")
    ap.add_argument("--turns", type=int, default=12, help="Число пользовательских постов подряд")
    ap.add_argument("--wait-timeout", type=int, default=35)
    ap.add_argument("--per-turn-sleep", type=float, default=0.08)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--base-sleep", type=float, default=1.2)
    ap.add_argument("--chat-name-prefix", default="")
    ap.add_argument("--out", default="", help="JSON отчёт (пусто = stdout)")
    args = ap.parse_args()

    payload = asyncio.run(run(args))
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if (args.out or "").strip():
        Path(args.out).resolve().write_text(text + "\n", encoding="utf-8")
        print(f"Saved: {args.out}")
    else:
        print(text)
    return 0 if payload.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
