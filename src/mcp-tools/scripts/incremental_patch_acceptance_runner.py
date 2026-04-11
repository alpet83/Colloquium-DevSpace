#!/usr/bin/env python3
"""Приёмочные сценарии для приоритета context_patch над старым телом поста.

Сценарии вызывают реальный LLM (без #debug_bypass): модель должна взять актуальные
фрагменты после правок и вернуть строго JSON для автооценки.

Зависимости: httpx, mcp (cqds_client), см. requirements-cli.txt.

Пример:
  python incremental_patch_acceptance_runner.py --cases a,b --out ../../.tmp/patch_accept.json
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
from patch_acceptance_math import grade_case_a, grade_case_b, normalize_expr, safe_eval_arith

DEBUG_BYPASS_TAG = "#debug_bypass"


def _norm_actor(name: str) -> str:
    return name.strip().lstrip("@").lower()


def _norm_user(name: str) -> str:
    return (name or "").strip().lower()


def _posts_list(resp: dict) -> list[dict]:
    posts = resp.get("posts")
    if not isinstance(posts, dict):
        return []
    out = [p for p in posts.values() if isinstance(p, dict)]
    out.sort(key=lambda p: int(p.get("id", 0) or 0))
    return out


def _latest_actor_message(resp: dict, expected_user: str) -> str | None:
    vals = _posts_list(resp)
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


def _user_posts_sorted(history: dict, username: str) -> list[dict]:
    u = _norm_user(username)
    return [
        p
        for p in _posts_list(history)
        if _norm_user(str(p.get("user_name", ""))) == u
    ]


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
            return last

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


async def run_case_a(
    client: ColloquiumClient,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Один пост: старое выражение (1+2)*3, после edit (1+2)*4."""
    actor = args.actor_mention
    desc = f"patch-accept-a:{_iso_now()}:p{args.project_id}"
    await _retry(
        lambda: client.select_project(args.project_id),
        args.retries,
        args.base_sleep,
        "select_project",
    )
    chat_id = await _retry(
        lambda: client.create_chat(desc),
        args.retries,
        args.base_sleep,
        "create_chat",
    )

    stale_expr = "(1+2)*3"
    fresh_expr = "(1+2)*4"
    expected_result = safe_eval_arith(fresh_expr)

    await _retry(
        lambda: client.post_message(
            chat_id,
            f"PATCH_ACCEPT_SETUP_A\nExpression for test: `{stale_expr}`\n"
            f"(setup only, ignore for final answer) {DEBUG_BYPASS_TAG}",
        ),
        args.retries,
        args.base_sleep,
        "post_setup_a",
    )
    await _retry(
        lambda: _wait_reply(client, chat_id, min(30, args.wait_timeout), actor),
        args.retries,
        args.base_sleep,
        "wait_setup_a",
    )

    await _retry(
        lambda: client.post_message(
            chat_id,
            f"filler post A1 {DEBUG_BYPASS_TAG}",
        ),
        args.retries,
        args.base_sleep,
        "filler_a1",
    )
    await _retry(
        lambda: _wait_reply(client, chat_id, min(30, args.wait_timeout), actor),
        args.retries,
        args.base_sleep,
        "wait_filler_a1",
    )

    hist = await _retry(
        lambda: client.get_history(chat_id),
        args.retries,
        args.base_sleep,
        "get_history_a",
    )
    ups = _user_posts_sorted(hist, args.username)
    if not ups:
        return {"case": "a", "pass": False, "error": "no_user_posts", "chat_id": chat_id}
    target_pid = int(ups[0].get("id", 0) or 0)
    if target_pid <= 0:
        return {"case": "a", "pass": False, "error": "bad_target_pid", "chat_id": chat_id}

    new_body = (
        f"PATCH_ACCEPT_SETUP_A\nExpression for test: `{fresh_expr}`\n"
        f"(edited body; authoritative for the next question) {DEBUG_BYPASS_TAG}"
    )
    await _retry(
        lambda: client.edit_post(target_pid, new_body),
        args.retries,
        args.base_sleep,
        "edit_post_a",
    )

    question = (
        f"{actor} PATCH_ACCEPTANCE_A (math + context priority).\n"
        "Rules from system pre-prompt apply: if the same post_id appears with a context_patch revision, "
        "the patch wins over an older inline copy.\n\n"
        f"In @post#{target_pid}, read the expression inside the backticks after the exact label "
        "`Expression for test:`.\n"
        "Evaluate it numerically.\n\n"
        "Reply with JSON only (no markdown fence), exactly one object:\n"
        '{"post_id": <int>, "expr": "<string>", "result": <number>}\n'
        f"The post_id must be {target_pid}. The expr string must be the expression from the backticks only."
    )
    await _retry(
        lambda: client.post_message(chat_id, question),
        args.retries,
        args.base_sleep,
        "question_a",
    )
    reply = await _retry(
        lambda: _wait_reply(client, chat_id, args.wait_timeout, actor),
        args.retries,
        args.base_sleep,
        "wait_answer_a",
    )
    text = _extract_latest_message(reply, actor)
    grade = grade_case_a(
        reply_text=text,
        expected_post_id=target_pid,
        expected_expr=fresh_expr,
        stale_expr=stale_expr,
    )
    grade["case"] = "a"
    grade["chat_id"] = chat_id
    grade["target_post_id"] = target_pid
    grade["expected_expr"] = fresh_expr
    grade["expected_result"] = expected_result
    grade["reply_excerpt"] = text[:1200]
    return grade


async def run_case_b(
    client: ColloquiumClient,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Три поста SEG:; после двух edit комбинируется 3*(2+3)=15."""
    actor = args.actor_mention
    desc = f"patch-accept-b:{_iso_now()}:p{args.project_id}"
    await _retry(
        lambda: client.select_project(args.project_id),
        args.retries,
        args.base_sleep,
        "select_project",
    )
    chat_id = await _retry(
        lambda: client.create_chat(desc),
        args.retries,
        args.base_sleep,
        "create_chat_b",
    )

    await _retry(
        lambda: client.post_message(
            chat_id,
            f"PATCH_ACCEPT_SETUP_B\nSEG: 2\n{DEBUG_BYPASS_TAG}",
        ),
        args.retries,
        args.base_sleep,
        "seg1",
    )
    await _retry(
        lambda: _wait_reply(client, chat_id, min(30, args.wait_timeout), actor),
        args.retries,
        args.base_sleep,
        "wait_seg1",
    )
    await _retry(
        lambda: client.post_message(chat_id, f"PATCH_ACCEPT_SETUP_B\nSEG: *\n{DEBUG_BYPASS_TAG}"),
        args.retries,
        args.base_sleep,
        "seg2",
    )
    await _retry(
        lambda: _wait_reply(client, chat_id, min(30, args.wait_timeout), actor),
        args.retries,
        args.base_sleep,
        "wait_seg2",
    )
    await _retry(
        lambda: client.post_message(
            chat_id,
            f"PATCH_ACCEPT_SETUP_B\nSEG: (3+4)\n{DEBUG_BYPASS_TAG}",
        ),
        args.retries,
        args.base_sleep,
        "seg3",
    )
    await _retry(
        lambda: _wait_reply(client, chat_id, min(30, args.wait_timeout), actor),
        args.retries,
        args.base_sleep,
        "wait_seg3",
    )

    await _retry(
        lambda: client.post_message(chat_id, f"filler B {DEBUG_BYPASS_TAG}"),
        args.retries,
        args.base_sleep,
        "filler_b",
    )
    await _retry(
        lambda: _wait_reply(client, chat_id, min(30, args.wait_timeout), actor),
        args.retries,
        args.base_sleep,
        "wait_filler_b",
    )

    hist = await _retry(
        lambda: client.get_history(chat_id),
        args.retries,
        args.base_sleep,
        "get_history_b",
    )
    ups = _user_posts_sorted(hist, args.username)
    seg_posts = [p for p in ups if "PATCH_ACCEPT_SETUP_B" in str(p.get("message", ""))]
    seg_posts.sort(key=lambda p: int(p.get("id", 0) or 0))
    if len(seg_posts) < 3:
        return {
            "case": "b",
            "pass": False,
            "error": "not_enough_seg_posts",
            "chat_id": chat_id,
            "found": len(seg_posts),
        }
    p1 = int(seg_posts[0].get("id", 0) or 0)
    p2 = int(seg_posts[1].get("id", 0) or 0)
    p3 = int(seg_posts[2].get("id", 0) or 0)
    ids_sorted = sorted([p1, p2, p3])

    await _retry(
        lambda: client.edit_post(
            p1,
            f"PATCH_ACCEPT_SETUP_B\nSEG: 3\n(edited) {DEBUG_BYPASS_TAG}",
        ),
        args.retries,
        args.base_sleep,
        "edit_p1",
    )
    await _retry(
        lambda: client.edit_post(
            p3,
            f"PATCH_ACCEPT_SETUP_B\nSEG: (2+3)\n(edited) {DEBUG_BYPASS_TAG}",
        ),
        args.retries,
        args.base_sleep,
        "edit_p3",
    )

    expected_combined = "3*(2+3)"
    expected_result = safe_eval_arith(expected_combined)

    question = (
        f"{actor} PATCH_ACCEPTANCE_B (multi-post + patches).\n"
        "From each of the three setup posts that contain the line starting with `SEG:`, take the substring "
        "after `SEG:` up to the end of that line (trim whitespace). Ignore parenthetical notes like (edited).\n"
        "Let three snippets be ordered by ascending post_id. Concatenate them **with no extra characters** "
        "between snippets to form one arithmetic expression.\n"
        "Use Python-style arithmetic notation (including standard unary + / - when needed), "
        "and keep the expression exactly as a valid Python arithmetic expression string.\n"
        f"Relevant post ids (ascending): {ids_sorted[0]}, {ids_sorted[1]}, {ids_sorted[2]}.\n"
        "Evaluate the expression.\n\n"
        "Reply with JSON only (no markdown fence), exactly one object:\n"
        '{"post_ids": [<int>, <int>, <int>], "combined_expr": "<string>", "result": <number>}\n'
        "post_ids must be sorted ascending. combined_expr must be exactly the concatenation as defined."
    )
    await _retry(
        lambda: client.post_message(chat_id, question),
        args.retries,
        args.base_sleep,
        "question_b",
    )
    reply = await _retry(
        lambda: _wait_reply(client, chat_id, args.wait_timeout, actor),
        args.retries,
        args.base_sleep,
        "wait_answer_b",
    )
    text = _extract_latest_message(reply, actor)
    grade = grade_case_b(
        reply_text=text,
        expected_post_ids=ids_sorted,
        expected_combined_expr=expected_combined,
    )
    grade["case"] = "b"
    grade["chat_id"] = chat_id
    grade["seg_post_ids"] = ids_sorted
    grade["expected_combined_expr"] = expected_combined
    grade["expected_combined_norm"] = normalize_expr(expected_combined)
    grade["expected_result"] = expected_result
    grade["reply_excerpt"] = text[:1200]
    return grade


async def _amain(args: argparse.Namespace) -> dict[str, Any]:
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
        "cases": [],
        "started_at": started,
    }
    try:
        case_list = [c.strip().lower() for c in args.cases.split(",") if c.strip()]
        for c in case_list:
            if c == "a":
                report["cases"].append(await run_case_a(client, args))
            elif c == "b":
                report["cases"].append(await run_case_b(client, args))
            else:
                report["cases"].append({"case": c, "pass": False, "error": "unknown_case"})
        passed = sum(1 for x in report["cases"] if x.get("pass"))
        report["passed"] = passed
        report["total"] = len(report["cases"])
        report["status"] = "ok" if passed == len(report["cases"]) else "partial"
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
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default=os.environ.get("COLLOQUIUM_URL", "http://localhost:8008"))
    ap.add_argument("--username", default=os.environ.get("COLLOQUIUM_USERNAME", "copilot"))
    ap.add_argument("--password", default=os.environ.get("COLLOQUIUM_PASSWORD", ""))
    ap.add_argument("--password-file", default=cq_cred.default_password_file_for_cli())
    ap.add_argument("--project-id", type=int, default=2)
    ap.add_argument("--actor-mention", default="@grok4f")
    ap.add_argument("--wait-timeout", type=int, default=120)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--base-sleep", type=float, default=1.2)
    ap.add_argument(
        "--cases",
        default="a,b",
        help="Comma-separated: a (single post), b (multi SEG posts)",
    )
    ap.add_argument("--out", default="", help="JSON report path")
    args = ap.parse_args()

    try:
        payload = asyncio.run(_amain(args))
    except BaseException:
        return 1

    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if (args.out or "").strip():
        outp = Path(args.out)
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text(text + "\n", encoding="utf-8")
        print(f"Saved: {outp}")
    else:
        print(text)
    return 0 if payload.get("status") == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
