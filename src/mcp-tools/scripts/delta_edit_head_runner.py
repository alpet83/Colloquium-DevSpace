#!/usr/bin/env python3
"""Короткий прогон: 2 поста (ожидаем FULL затем DELTA_SAFE), правка самого старого пользовательского поста, 3-й пост.

Ожидание после правки: FULL + reason head_post_content_changed (или no_tail_append если бы не было нового поста).
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import random
import re
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


def _norm_user(name: str) -> str:
    return (name or "").strip().lower()


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


def _posts_list(resp: dict) -> list[dict]:
    posts = resp.get("posts")
    if not isinstance(posts, dict):
        return []
    out = [p for p in posts.values() if isinstance(p, dict)]
    out.sort(key=lambda p: int(p.get("id", 0) or 0))
    return out


def _min_user_post_id(history: dict, username: str) -> int | None:
    u = _norm_user(username)
    best: int | None = None
    for p in _posts_list(history):
        if _norm_user(str(p.get("user_name", ""))) != u:
            continue
        pid = int(p.get("id", 0) or 0)
        if pid <= 0:
            continue
        if best is None or pid < best:
            best = pid
    return best


def _parse_query_db_rows(exec_result: dict[str, Any]) -> list[list[Any]]:
    shell_status = str(exec_result.get("status") or "")
    raw = str(exec_result.get("output") or "")
    if shell_status != "success":
        raise RuntimeError(f"query_db: {shell_status}: {raw[:600]}")
    m = re.search(r"<stdout>(.*)</stdout>", raw, re.DOTALL)
    inner = (m.group(1) if m else raw).strip()
    data = json.loads(inner)
    if data.get("status") != "success":
        raise RuntimeError(f"query_db inner: {data!r}")
    rows = data.get("rows")
    return rows if isinstance(rows, list) else []


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
        "steps": [],
    }

    try:
        await _retry(
            lambda: client.select_project(args.project_id),
            args.retries,
            args.base_sleep,
            "select_project",
        )
        desc = f"cache-delta-edit-head:{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}:p{args.project_id}"
        chat_id = await _retry(
            lambda: client.create_chat(desc),
            args.retries,
            args.base_sleep,
            "create_chat",
        )
        report["chat_id"] = chat_id
        report["description"] = desc
        actor = args.actor_mention

        async def turn(label: str, body: str) -> None:
            await _retry(
                lambda: client.post_message(chat_id, body),
                args.retries,
                args.base_sleep,
                f"post:{label}",
            )
            reply = await _retry(
                lambda: _wait_reply(client, chat_id, args.wait_timeout, actor),
                args.retries,
                args.base_sleep,
                f"wait:{label}",
            )
            excerpt = _extract_latest_message(reply, actor)[:300]
            report["steps"].append({"step": label, "ok": bool(excerpt), "reply_excerpt": excerpt})
            await asyncio.sleep(max(0.0, float(args.per_turn_sleep)))

        await turn(
            "1",
            f"{actor} cache edit-head step1/3. {DEBUG_BYPASS_TAG}",
        )
        await turn(
            "2",
            f"{actor} cache edit-head step2/3 (expect prior DELTA_SAFE). {DEBUG_BYPASS_TAG}",
        )

        hist = await _retry(
            lambda: client.get_history(chat_id),
            args.retries,
            args.base_sleep,
            "get_history",
        )
        first_pid = _min_user_post_id(hist, args.username)
        report["edited_post_id"] = first_pid
        if first_pid is None:
            raise RuntimeError("Could not find user post id for edit")

        new_text = f"{actor} cache edit-head step1/3 EDITED. {DEBUG_BYPASS_TAG}"
        await _retry(
            lambda: client.edit_post(first_pid, new_text),
            args.retries,
            args.base_sleep,
            "edit_post",
        )
        report["steps"].append({"step": "edit_first_user_post", "post_id": first_pid, "ok": True})

        await turn(
            "3_after_edit",
            f"{actor} cache edit-head step3/3 after head edit (expect FULL head_post_content_changed). {DEBUG_BYPASS_TAG}",
        )

        q = (
            "SELECT mode, reason, last_post_id, schema_ver, metric_id "
            "FROM context_cache_metrics WHERE chat_id = :cid ORDER BY metric_id DESC LIMIT 12"
        )
        q_exec = q.replace(":cid", str(int(chat_id)))
        raw_exec = await _retry(
            lambda: client.query_db(args.project_id, q_exec, timeout=60),
            args.retries,
            args.base_sleep,
            "query_metrics",
        )
        rows = _parse_query_db_rows(raw_exec)
        report["metrics_tail"] = [
            {"mode": r[0], "reason": r[1], "last_post_id": r[2], "schema_ver": r[3], "metric_id": r[4]}
            for r in rows
            if len(r) >= 5
        ]

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

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default=os.environ.get("COLLOQUIUM_URL", "http://localhost:8008"))
    ap.add_argument("--username", default=os.environ.get("COLLOQUIUM_USERNAME", "copilot"))
    ap.add_argument("--password", default=os.environ.get("COLLOQUIUM_PASSWORD", ""))
    ap.add_argument("--password-file", default=cq_cred.default_password_file_for_cli())
    ap.add_argument("--project-id", type=int, default=2)
    ap.add_argument("--actor-mention", default="@grok4f")
    ap.add_argument("--wait-timeout", type=int, default=35)
    ap.add_argument("--per-turn-sleep", type=float, default=0.08)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--base-sleep", type=float, default=1.2)
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
