#!/usr/bin/env python3
"""Ghost influence acceptance runner.

Проверяет, что модель НЕ использует устаревший контекст после edit_post:
1) post_ghost: OLD_TOKEN -> NEW_TOKEN в том же post_id.
2) attach_switch_ghost: @attached_file#old -> @attached_file#new в том же post_id.
3) vars_state: степень 1 — много постов с присваиваниями, edit одного поста меняет
   переменную/выражение; модель должна вывести полное актуальное состояние.
4) vars_missing: степень 2 — запрос 3-4 переменных, включая отсутствующую; прохождение
   только при явной индикации ошибки/отсутствия для missing переменной.

Сетап шаги делаются с #debug_bypass, финальный вопрос без bypass.
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


def _posts_list(resp: dict) -> list[dict]:
    posts = resp.get("posts")
    if not isinstance(posts, dict):
        return []
    out = [p for p in posts.values() if isinstance(p, dict)]
    out.sort(key=lambda p: int(p.get("id", 0) or 0))
    return out


def _user_posts_sorted(history: dict, username: str) -> list[dict]:
    u = _norm_user(username)
    return [p for p in _posts_list(history) if _norm_user(str(p.get("user_name", ""))) == u]


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


def _extract_json_object(text: str) -> dict[str, Any] | None:
    if not text or not text.strip():
        return None
    t = text.strip()
    try:
        v = json.loads(t)
        return v if isinstance(v, dict) else None
    except json.JSONDecodeError:
        pass
    s = t.find("{")
    if s < 0:
        return None
    depth = 0
    for i in range(s, len(t)):
        if t[i] == "{":
            depth += 1
        elif t[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    v = json.loads(t[s : i + 1])
                    return v if isinstance(v, dict) else None
                except json.JSONDecodeError:
                    return None
    return None


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


async def _pick_two_file_ids(client: ColloquiumClient, project_id: int) -> tuple[int, int]:
    q = (
        f"SELECT id FROM attached_files WHERE project_id = {int(project_id)} "
        "ORDER BY id ASC LIMIT 2"
    )
    raw = await client.query_db(project_id, q, timeout=60)
    rows = _parse_query_db_rows(raw)
    ids = []
    for r in rows:
        if not r:
            continue
        try:
            ids.append(int(r[0]))
        except (TypeError, ValueError):
            continue
    if len(ids) < 2:
        raise RuntimeError("Need at least 2 attached files for attach_switch_ghost")
    return ids[0], ids[1]


async def run_case_post_ghost(client: ColloquiumClient, args: argparse.Namespace) -> dict[str, Any]:
    actor = args.actor_mention
    desc = f"ghost-post:{_iso_now()}:p{args.project_id}"
    chat_id = await _retry(lambda: client.create_chat(desc), args.retries, args.base_sleep, "create_chat_post")
    old_token = "OLD_POST_TOKEN_314159"
    new_token = "NEW_POST_TOKEN_271828"
    setup = (
        "GHOST_SETUP_POST\n"
        f"TOKEN: {old_token}\n"
        f"(setup) {DEBUG_BYPASS_TAG}"
    )
    await _retry(lambda: client.post_message(chat_id, setup), args.retries, args.base_sleep, "post_setup_post")
    await _retry(lambda: _wait_reply(client, chat_id, min(30, args.wait_timeout), actor), args.retries, args.base_sleep, "wait_setup_post")
    await _retry(lambda: client.post_message(chat_id, f"filler post ghost {DEBUG_BYPASS_TAG}"), args.retries, args.base_sleep, "filler_post")
    await _retry(lambda: _wait_reply(client, chat_id, min(30, args.wait_timeout), actor), args.retries, args.base_sleep, "wait_filler_post")

    hist = await _retry(lambda: client.get_history(chat_id), args.retries, args.base_sleep, "get_history_post")
    ups = _user_posts_sorted(hist, args.username)
    target = [p for p in ups if "GHOST_SETUP_POST" in str(p.get("message", ""))]
    if not target:
        return {"case": "post_ghost", "pass": False, "error": "setup_post_not_found", "chat_id": chat_id}
    pid = int(target[0].get("id", 0) or 0)
    edited = (
        "GHOST_SETUP_POST\n"
        f"TOKEN: {new_token}\n"
        f"(edited) {DEBUG_BYPASS_TAG}"
    )
    await _retry(lambda: client.edit_post(pid, edited), args.retries, args.base_sleep, "edit_post_post")

    question = (
        f"{actor} GHOST_INFLUENCE_CHECK_POST.\n"
        f"Read TOKEN from @post#{pid} (line starts with `TOKEN:`). "
        "Use latest revision semantics from context_patch/revision_ts.\n"
        "Reply JSON only:\n"
        '{"post_id": <int>, "token": "<string>"}'
    )
    await _retry(lambda: client.post_message(chat_id, question), args.retries, args.base_sleep, "question_post")
    reply = await _retry(lambda: _wait_reply(client, chat_id, args.wait_timeout, actor), args.retries, args.base_sleep, "wait_answer_post")
    text = _extract_latest_message(reply, actor)
    obj = _extract_json_object(text)
    if not obj:
        return {"case": "post_ghost", "pass": False, "error": "no_json", "chat_id": chat_id, "reply_excerpt": text[:800]}
    got_pid = int(obj.get("post_id", -1) or -1)
    got_token = str(obj.get("token", "") or "")
    if got_token == old_token:
        return {"case": "post_ghost", "pass": False, "error": "ghost_old_token_used", "chat_id": chat_id}
    if got_token != new_token:
        return {"case": "post_ghost", "pass": False, "error": "wrong_token", "expected": new_token, "got": got_token, "chat_id": chat_id}
    out = {
        "case": "post_ghost",
        "pass": True,
        "chat_id": chat_id,
        "target_post_id": pid,
        "post_id_returned": got_pid,
        "token": got_token,
        "reply_excerpt": text[:800],
    }
    if got_pid != pid:
        out["post_id_mismatch"] = {"expected": pid, "got": got_pid}
        if args.strict_post_id:
            out["pass"] = False
            out["error"] = "wrong_post_id"
    return out


async def run_case_attach_switch(client: ColloquiumClient, args: argparse.Namespace) -> dict[str, Any]:
    actor = args.actor_mention
    desc = f"ghost-attach:{_iso_now()}:p{args.project_id}"
    chat_id = await _retry(lambda: client.create_chat(desc), args.retries, args.base_sleep, "create_chat_attach")
    old_fid, new_fid = await _retry(lambda: _pick_two_file_ids(client, args.project_id), args.retries, args.base_sleep, "pick_files")
    old_token = "OLD_ATTACH_TOKEN_9001"
    new_token = "NEW_ATTACH_TOKEN_9002"

    setup = (
        "GHOST_SETUP_ATTACH\n"
        f"USE_FILE: @attached_file#{old_fid}\n"
        f"TOKEN: {old_token}\n"
        f"(setup) {DEBUG_BYPASS_TAG}"
    )
    await _retry(lambda: client.post_message(chat_id, setup), args.retries, args.base_sleep, "post_setup_attach")
    await _retry(lambda: _wait_reply(client, chat_id, min(30, args.wait_timeout), actor), args.retries, args.base_sleep, "wait_setup_attach")
    await _retry(lambda: client.post_message(chat_id, f"filler attach ghost {DEBUG_BYPASS_TAG}"), args.retries, args.base_sleep, "filler_attach")
    await _retry(lambda: _wait_reply(client, chat_id, min(30, args.wait_timeout), actor), args.retries, args.base_sleep, "wait_filler_attach")

    hist = await _retry(lambda: client.get_history(chat_id), args.retries, args.base_sleep, "get_history_attach")
    ups = _user_posts_sorted(hist, args.username)
    target = [p for p in ups if "GHOST_SETUP_ATTACH" in str(p.get("message", ""))]
    if not target:
        return {"case": "attach_switch_ghost", "pass": False, "error": "setup_post_not_found", "chat_id": chat_id}
    pid = int(target[0].get("id", 0) or 0)
    edited = (
        "GHOST_SETUP_ATTACH\n"
        f"USE_FILE: @attached_file#{new_fid}\n"
        f"TOKEN: {new_token}\n"
        f"(edited) {DEBUG_BYPASS_TAG}"
    )
    await _retry(lambda: client.edit_post(pid, edited), args.retries, args.base_sleep, "edit_post_attach")

    question = (
        f"{actor} GHOST_INFLUENCE_CHECK_ATTACH.\n"
        f"From @post#{pid}, read current `USE_FILE:` and `TOKEN:` values using latest revision semantics.\n"
        "Reply JSON only:\n"
        '{"post_id": <int>, "file_id": <int>, "token": "<string>"}'
    )
    await _retry(lambda: client.post_message(chat_id, question), args.retries, args.base_sleep, "question_attach")
    reply = await _retry(lambda: _wait_reply(client, chat_id, args.wait_timeout, actor), args.retries, args.base_sleep, "wait_answer_attach")
    text = _extract_latest_message(reply, actor)
    obj = _extract_json_object(text)
    if not obj:
        return {"case": "attach_switch_ghost", "pass": False, "error": "no_json", "chat_id": chat_id, "reply_excerpt": text[:800]}

    got_pid = int(obj.get("post_id", -1) or -1)
    got_fid = int(obj.get("file_id", -1) or -1)
    got_token = str(obj.get("token", "") or "")

    if got_fid == old_fid or got_token == old_token:
        return {
            "case": "attach_switch_ghost",
            "pass": False,
            "error": "ghost_old_attach_used",
            "chat_id": chat_id,
            "expected_file_id": new_fid,
            "got_file_id": got_fid,
            "expected_token": new_token,
            "got_token": got_token,
        }
    if got_fid != new_fid or got_token != new_token:
        return {
            "case": "attach_switch_ghost",
            "pass": False,
            "error": "wrong_attach_or_token",
            "chat_id": chat_id,
            "expected_file_id": new_fid,
            "got_file_id": got_fid,
            "expected_token": new_token,
            "got_token": got_token,
        }
    out = {
        "case": "attach_switch_ghost",
        "pass": True,
        "chat_id": chat_id,
        "target_post_id": pid,
        "post_id_returned": got_pid,
        "file_id": got_fid,
        "token": got_token,
        "reply_excerpt": text[:800],
    }
    if got_pid != pid:
        out["post_id_mismatch"] = {"expected": pid, "got": got_pid}
        if args.strict_post_id:
            out["pass"] = False
            out["error"] = "wrong_post_id"
    return out


async def run_case_vars_state(client: ColloquiumClient, args: argparse.Namespace) -> dict[str, Any]:
    """Степень 1: состояние переменных после правки старого поста."""
    actor = args.actor_mention
    desc = f"ghost-vars-state:{_iso_now()}:p{args.project_id}"
    chat_id = await _retry(lambda: client.create_chat(desc), args.retries, args.base_sleep, "create_chat_vars_state")

    p1 = f"GHOST_VARS\nSET: alpha = 2\nSET: beta = 3\n{DEBUG_BYPASS_TAG}"
    p2_old = f"GHOST_VARS\nSET: gamma = alpha + beta\nSET: delta = gamma * 2\n{DEBUG_BYPASS_TAG}"
    p3 = f"GHOST_VARS\nSET: eps = delta - 1\n{DEBUG_BYPASS_TAG}"

    await _retry(lambda: client.post_message(chat_id, p1), args.retries, args.base_sleep, "vars_state_p1")
    await _retry(lambda: _wait_reply(client, chat_id, min(30, args.wait_timeout), actor), args.retries, args.base_sleep, "wait_vars_state_p1")
    await _retry(lambda: client.post_message(chat_id, p2_old), args.retries, args.base_sleep, "vars_state_p2_old")
    await _retry(lambda: _wait_reply(client, chat_id, min(30, args.wait_timeout), actor), args.retries, args.base_sleep, "wait_vars_state_p2_old")
    await _retry(lambda: client.post_message(chat_id, p3), args.retries, args.base_sleep, "vars_state_p3")
    await _retry(lambda: _wait_reply(client, chat_id, min(30, args.wait_timeout), actor), args.retries, args.base_sleep, "wait_vars_state_p3")

    hist = await _retry(lambda: client.get_history(chat_id), args.retries, args.base_sleep, "get_history_vars_state")
    ups = _user_posts_sorted(hist, args.username)
    targets = [p for p in ups if "SET: gamma" in str(p.get("message", "")) and "SET: delta" in str(p.get("message", ""))]
    if not targets:
        return {"case": "vars_state", "pass": False, "error": "setup_post_not_found", "chat_id": chat_id}
    pid = int(targets[0].get("id", 0) or 0)

    # Меняем и выражение, и переменную: gamma = alpha - beta; delta = gamma * 5
    p2_new = f"GHOST_VARS\nSET: gamma = alpha - beta\nSET: delta = gamma * 5\n(edited) {DEBUG_BYPASS_TAG}"
    await _retry(lambda: client.edit_post(pid, p2_new), args.retries, args.base_sleep, "edit_vars_state_p2")

    expected = {"alpha": 2, "beta": 3, "gamma": -1, "delta": -5, "eps": -6}
    question = (
        f"{actor} GHOST_INFLUENCE_CHECK_VARS_STATE.\n"
        "Compute final values from all current SET lines (latest revision wins by context_patch/revision_ts).\n"
        "Reply JSON only exactly:\n"
        '{"vars":{"alpha":<number>,"beta":<number>,"gamma":<number>,"delta":<number>,"eps":<number>}}'
    )
    await _retry(lambda: client.post_message(chat_id, question), args.retries, args.base_sleep, "question_vars_state")
    reply = await _retry(lambda: _wait_reply(client, chat_id, args.wait_timeout, actor), args.retries, args.base_sleep, "wait_vars_state")
    text = _extract_latest_message(reply, actor)
    obj = _extract_json_object(text)
    if not obj:
        return {"case": "vars_state", "pass": False, "error": "no_json", "chat_id": chat_id, "reply_excerpt": text[:800]}
    vars_obj = obj.get("vars")
    if not isinstance(vars_obj, dict):
        return {"case": "vars_state", "pass": False, "error": "vars_not_object", "chat_id": chat_id, "obj": obj}

    mismatches = {}
    for k, v in expected.items():
        got = vars_obj.get(k)
        if not isinstance(got, (int, float)) or float(got) != float(v):
            mismatches[k] = {"expected": v, "got": got}
    if mismatches:
        # Особая ghost-диагностика: старый delta=10 или eps=9
        if vars_obj.get("delta") == 10 or vars_obj.get("eps") == 9:
            return {
                "case": "vars_state",
                "pass": False,
                "error": "ghost_old_expression_influence",
                "chat_id": chat_id,
                "mismatches": mismatches,
                "reply_excerpt": text[:800],
            }
        return {
            "case": "vars_state",
            "pass": False,
            "error": "wrong_state",
            "chat_id": chat_id,
            "mismatches": mismatches,
            "reply_excerpt": text[:800],
        }
    return {
        "case": "vars_state",
        "pass": True,
        "chat_id": chat_id,
        "target_post_id": pid,
        "vars": vars_obj,
        "reply_excerpt": text[:800],
    }


async def run_case_vars_missing(client: ColloquiumClient, args: argparse.Namespace) -> dict[str, Any]:
    """Степень 2: запрос набора переменных, включая отсутствующую — нужна явная ошибка."""
    actor = args.actor_mention
    desc = f"ghost-vars-missing:{_iso_now()}:p{args.project_id}"
    chat_id = await _retry(lambda: client.create_chat(desc), args.retries, args.base_sleep, "create_chat_vars_missing")

    p1 = f"GHOST_VARS_M\nSET: a = 7\nSET: b = 2\n{DEBUG_BYPASS_TAG}"
    p2_old = f"GHOST_VARS_M\nSET: c = a + b\nSET: d = c * 3\n{DEBUG_BYPASS_TAG}"
    await _retry(lambda: client.post_message(chat_id, p1), args.retries, args.base_sleep, "vars_missing_p1")
    await _retry(lambda: _wait_reply(client, chat_id, min(30, args.wait_timeout), actor), args.retries, args.base_sleep, "wait_vars_missing_p1")
    await _retry(lambda: client.post_message(chat_id, p2_old), args.retries, args.base_sleep, "vars_missing_p2_old")
    await _retry(lambda: _wait_reply(client, chat_id, min(30, args.wait_timeout), actor), args.retries, args.base_sleep, "wait_vars_missing_p2_old")

    hist = await _retry(lambda: client.get_history(chat_id), args.retries, args.base_sleep, "get_history_vars_missing")
    ups = _user_posts_sorted(hist, args.username)
    targets = [p for p in ups if "SET: c" in str(p.get("message", "")) and "SET: d" in str(p.get("message", ""))]
    if not targets:
        return {"case": "vars_missing", "pass": False, "error": "setup_post_not_found", "chat_id": chat_id}
    pid = int(targets[0].get("id", 0) or 0)
    p2_new = f"GHOST_VARS_M\nSET: c = a - b\nSET: d = c * 4\n(edited) {DEBUG_BYPASS_TAG}"
    await _retry(lambda: client.edit_post(pid, p2_new), args.retries, args.base_sleep, "edit_vars_missing_p2")

    expected_vals = {"a": 7, "c": 5, "d": 20}
    missing_key = "z"
    question = (
        f"{actor} GHOST_INFLUENCE_CHECK_VARS_MISSING.\n"
        "Read latest SET assignments (revision-aware). Return values for a,c,d,z.\n"
        "For missing variable z, you must indicate explicit error.\n"
        "Reply JSON only exactly:\n"
        '{"vars":{"a":<number>,"c":<number>,"d":<number>},"errors":{"z":"<reason>"}}'
    )
    await _retry(lambda: client.post_message(chat_id, question), args.retries, args.base_sleep, "question_vars_missing")
    reply = await _retry(lambda: _wait_reply(client, chat_id, args.wait_timeout, actor), args.retries, args.base_sleep, "wait_vars_missing")
    text = _extract_latest_message(reply, actor)
    obj = _extract_json_object(text)
    if not obj:
        return {"case": "vars_missing", "pass": False, "error": "no_json", "chat_id": chat_id, "reply_excerpt": text[:800]}

    vars_obj = obj.get("vars")
    errs_obj = obj.get("errors")
    if not isinstance(vars_obj, dict):
        return {"case": "vars_missing", "pass": False, "error": "vars_not_object", "chat_id": chat_id, "obj": obj}
    if not isinstance(errs_obj, dict):
        return {"case": "vars_missing", "pass": False, "error": "errors_not_object", "chat_id": chat_id, "obj": obj}

    mismatches = {}
    for k, v in expected_vals.items():
        got = vars_obj.get(k)
        if not isinstance(got, (int, float)) or float(got) != float(v):
            mismatches[k] = {"expected": v, "got": got}
    if mismatches:
        return {"case": "vars_missing", "pass": False, "error": "wrong_values", "chat_id": chat_id, "mismatches": mismatches, "reply_excerpt": text[:800]}

    z_err = errs_obj.get(missing_key)
    if not isinstance(z_err, str) or not z_err.strip():
        return {
            "case": "vars_missing",
            "pass": False,
            "error": "missing_var_not_flagged",
            "chat_id": chat_id,
            "reply_excerpt": text[:800],
        }
    if "not" not in z_err.lower() and "missing" not in z_err.lower() and "undefined" not in z_err.lower() and "нет" not in z_err.lower():
        return {
            "case": "vars_missing",
            "pass": False,
            "error": "missing_var_bad_message",
            "chat_id": chat_id,
            "z_error": z_err,
            "reply_excerpt": text[:800],
        }

    return {
        "case": "vars_missing",
        "pass": True,
        "chat_id": chat_id,
        "target_post_id": pid,
        "vars": vars_obj,
        "errors": errs_obj,
        "reply_excerpt": text[:800],
    }


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
        await _retry(lambda: client.select_project(args.project_id), args.retries, args.base_sleep, "select_project")
        cases = [c.strip().lower() for c in args.cases.split(",") if c.strip()]
        for c in cases:
            if c == "post":
                report["cases"].append(await run_case_post_ghost(client, args))
            elif c == "attach":
                report["cases"].append(await run_case_attach_switch(client, args))
            elif c == "vars_state":
                report["cases"].append(await run_case_vars_state(client, args))
            elif c == "vars_missing":
                report["cases"].append(await run_case_vars_missing(client, args))
            else:
                report["cases"].append({"case": c, "pass": False, "error": "unknown_case"})
        passed = sum(1 for x in report["cases"] if x.get("pass"))
        report["passed"] = passed
        report["total"] = len(report["cases"])
        report["ghost_fail_rate_pct"] = round(100.0 * (report["total"] - passed) / report["total"], 3) if report["total"] else 0.0
        report["status"] = "ok" if passed == report["total"] else "partial"
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
    ap.add_argument("--actor-mention", default="@gpt5n")
    ap.add_argument("--wait-timeout", type=int, default=120)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--base-sleep", type=float, default=1.2)
    ap.add_argument("--cases", default="post,attach", help="Comma list: post,attach,vars_state,vars_missing")
    ap.add_argument("--strict-post-id", action="store_true", help="Fail case when model returns wrong post_id.")
    ap.add_argument("--out", default="", help="JSON report path")
    args = ap.parse_args()

    payload = asyncio.run(_amain(args))
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if (args.out or "").strip():
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
        print(f"Saved: {out}")
    else:
        print(text)
    return 0 if payload.get("status") == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
