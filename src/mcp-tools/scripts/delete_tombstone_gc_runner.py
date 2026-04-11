#!/usr/bin/env python3
"""E2E runner: delete -> forced FULL reason -> tombstone/DB checks.

Проверяет минимальный сценарий:
1) обычный ход с LLM,
2) удаление пользовательского поста через API,
3) следующий ход должен дать FULL с reason=post_deleted_tombstone.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

_THIS = Path(__file__).resolve()
_MCP_TOOLS = _THIS.parents[1]
if str(_MCP_TOOLS) not in sys.path:
    sys.path.insert(0, str(_MCP_TOOLS))

from cqds_client import ColloquiumClient
import cqds_credentials as cq_cred
from cqds_helpers import _is_progress_stub


def _norm_actor(name: str) -> str:
    return name.strip().lstrip("@").lower()


def _latest_actor_post(resp: dict, expected_user: str) -> dict[str, Any] | None:
    posts = resp.get("posts")
    if not isinstance(posts, dict):
        return None
    vals = [p for p in posts.values() if isinstance(p, dict)]
    if not vals:
        return None
    vals.sort(key=lambda p: int(p.get("id", 0) or 0))
    target = _norm_actor(expected_user)
    for p in reversed(vals):
        if str(p.get("user_name", "")).strip().lower() != target:
            continue
        msg = str(p.get("message", "") or "")
        if not msg or _is_progress_stub(msg):
            continue
        return p
    return None


async def _wait_actor_reply(
    client: ColloquiumClient,
    chat_id: int,
    expected_actor: str,
    timeout_sec: int,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(1, timeout_sec)
    last: dict[str, Any] = {"chat_history": "no changes"}
    while time.monotonic() < deadline:
        rem = max(1.0, deadline - time.monotonic())
        resp = await client.get_reply(chat_id, wait=True, timeout=min(rem, 15.0))
        if isinstance(resp, dict):
            last = resp
            if _latest_actor_post(resp, expected_actor) is not None:
                return resp
    return last


def _find_user_post_id(resp: dict, user_name: str, needle: str) -> int:
    posts = resp.get("posts")
    if not isinstance(posts, dict):
        return 0
    target = user_name.strip().lower()
    cand = [p for p in posts.values() if isinstance(p, dict)]
    cand.sort(key=lambda p: int(p.get("id", 0) or 0))
    for p in cand:
        if str(p.get("user_name", "")).strip().lower() != target:
            continue
        msg = str(p.get("message", "") or "")
        if needle in msg:
            return int(p.get("id", 0) or 0)
    return 0


async def _delete_post_via_api(client: ColloquiumClient, post_id: int) -> dict[str, Any]:
    await client._ensure_login()  # noqa: SLF001
    resp = await client._client.post(  # noqa: SLF001
        "/api/chat/delete_post",
        json={"post_id": int(post_id)},
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()


async def run(args: argparse.Namespace) -> dict[str, Any]:
    password, _ = cq_cred.resolve_password(args.password, args.password_file)
    if not password:
        raise RuntimeError("Password required")

    client = ColloquiumClient(base_url=args.url, username=args.username, password=password)
    report: dict[str, Any] = {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "project_id": int(args.project_id),
        "actor_mention": args.actor_mention,
        "status": "error",
    }
    try:
        await client.select_project(int(args.project_id))
        chat_id = await client.create_chat(f"delete-tombstone-e2e:{int(time.time())}")
        report["chat_id"] = chat_id

        marker = f"seed-delete-marker:{int(time.time())}"
        await client.post_message(chat_id, marker)
        await client.post_message(chat_id, f"{args.actor_mention} warmup")
        await _wait_actor_reply(client, chat_id, args.actor_mention, args.wait_timeout)

        snap = await client.get_history(chat_id)
        seed_post_id = _find_user_post_id(snap, args.username, marker)
        if seed_post_id <= 0:
            raise RuntimeError("Failed to locate seed post_id for delete")
        report["seed_post_id"] = seed_post_id

        del_result = await _delete_post_via_api(client, seed_post_id)
        report["delete_api_result"] = del_result

        await client.post_message(chat_id, f"{args.actor_mention} after-delete")
        await _wait_actor_reply(client, chat_id, args.actor_mention, args.wait_timeout)

        db_row_deleted = await client.query_db(
            int(args.project_id),
            "SELECT id, deleted_at, tombstone_text FROM posts "
            f"WHERE id={int(seed_post_id)};",
        )
        report["deleted_post_db"] = db_row_deleted

        db_metrics = await client.query_db(
            int(args.project_id),
            "SELECT metric_id, mode, reason "
            "FROM context_cache_metrics "
            f"WHERE chat_id={int(chat_id)} "
            "ORDER BY metric_id DESC LIMIT 6;",
        )
        report["metrics_tail"] = db_metrics

        gc_exec = await client.exec_command(
            int(args.project_id),
            (
                "python - <<'PY'\n"
                "import globals as g\n"
                "rm = getattr(g, 'replication_manager', None)\n"
                "if rm is None or not hasattr(rm, 'gc_deleted_posts'):\n"
                "    print('gc_removed', 0)\n"
                "else:\n"
                f"    print('gc_removed', int(rm.gc_deleted_posts(chat_id={int(chat_id)})))\n"
                "PY"
            ),
            timeout=60,
        )
        report["gc_exec"] = gc_exec
        expire_refs = await client.query_db(
            int(args.project_id),
            "UPDATE post_retention_refs "
            "SET expires_at = EXTRACT(EPOCH FROM NOW())::BIGINT - 1 "
            f"WHERE post_id={int(seed_post_id)};",
            allow_write=True,
        )
        report["expire_refs"] = expire_refs
        gc_exec_after_expire = await client.exec_command(
            int(args.project_id),
            (
                "python - <<'PY'\n"
                "import globals as g\n"
                "rm = getattr(g, 'replication_manager', None)\n"
                "if rm is None or not hasattr(rm, 'gc_deleted_posts'):\n"
                "    print('gc_removed', 0)\n"
                "else:\n"
                f"    print('gc_removed', int(rm.gc_deleted_posts(chat_id={int(chat_id)})))\n"
                "PY"
            ),
            timeout=60,
        )
        report["gc_exec_after_expire"] = gc_exec_after_expire
        db_after_gc = await client.query_db(
            int(args.project_id),
            "SELECT id FROM posts "
            f"WHERE id={int(seed_post_id)};",
        )
        report["deleted_post_after_gc"] = db_after_gc
        refs_after = await client.query_db(
            int(args.project_id),
            "SELECT post_id, actor_id, session_id, expires_at "
            "FROM post_retention_refs "
            f"WHERE post_id={int(seed_post_id)};",
        )
        report["refs_after"] = refs_after

        # Lightweight assertions
        rows_deleted = (db_row_deleted.get("output") or "")
        rows_metrics = (db_metrics.get("output") or "")
        rows_after_gc = (db_after_gc.get("output") or "")
        ok_deleted = ("#deleted_by:user" in rows_deleted) and ("null" not in rows_deleted.lower())
        ok_reason = ("post_deleted_tombstone" in rows_metrics) and ("\"FULL\"" in rows_metrics or " FULL " in rows_metrics)
        ok_gc_removed = ("\"rows\": []" in rows_after_gc)
        report["checks"] = {
            "deleted_row_has_tombstone": ok_deleted,
            "metrics_have_forced_full_reason": ok_reason,
            "gc_removes_tombstoned_post": ok_gc_removed,
        }
        # GC зависит от жизненного цикла lease/ref и может быть отложен;
        # критичные инварианты этого раннера — tombstone + forced FULL reason.
        report["status"] = "ok" if (ok_deleted and ok_reason) else "failed"
        return report
    finally:
        await client.aclose()


def main() -> int:
    ap = argparse.ArgumentParser(description="Delete/tombstone/forced-FULL E2E runner")
    ap.add_argument("--url", default=os.environ.get("COLLOQUIUM_URL", "http://localhost:8008"))
    ap.add_argument("--username", default=os.environ.get("COLLOQUIUM_USERNAME", "copilot"))
    ap.add_argument("--password", default=os.environ.get("COLLOQUIUM_PASSWORD", ""))
    ap.add_argument("--password-file", default=cq_cred.default_password_file_for_cli())
    ap.add_argument("--project-id", type=int, default=2)
    ap.add_argument("--actor-mention", default="@gpt5n")
    ap.add_argument("--wait-timeout", type=int, default=45)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    payload = asyncio.run(run(args))
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).resolve().write_text(text + "\n", encoding="utf-8")
        print(f"Saved: {args.out}")
    else:
        print(text)
    return 0 if payload.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

