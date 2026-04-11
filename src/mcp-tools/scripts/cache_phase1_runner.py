#!/usr/bin/env python3
"""Read-only interactive scenario runner for Phase-1 cache telemetry."""
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


READONLY_GUARD = (
    "Analysis only. Review real code and explain findings. "
    "Do not propose or execute file/code modifications."
)


SCENARIOS: list[dict[str, Any]] = [
    {
        "id": "module_risk_review",
        "turns": [
            "Review one core module of this project and list top 3 implementation risks with concrete evidence.",
            "For the highest-impact risk, explain likely failure mode and the earliest signal in logs/behavior.",
        ],
    },
    {
        "id": "dependency_flow_review",
        "turns": [
            "Explain request/data flow across key components for one representative feature in this project.",
            "Identify 2 bottlenecks or brittle boundaries and why they are brittle.",
        ],
    },
    {
        "id": "test_gap_review",
        "turns": [
            "Find likely test coverage gaps in a critical part of this project.",
            "Provide a minimal read-only validation checklist (no code changes).",
        ],
    },
]


def _iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _extract_latest_message(resp: dict) -> str:
    if not isinstance(resp, dict):
        return ""
    hist = resp.get("chat_history")
    if isinstance(hist, list) and hist:
        last = hist[-1]
        if isinstance(last, dict):
            for key in ("message", "text", "content"):
                val = last.get(key)
                if isinstance(val, str):
                    return val
    if isinstance(hist, str):
        return hist
    return ""


async def _wait_until_message(client: ColloquiumClient, chat_id: int, timeout_sec: int) -> dict:
    deadline = time.monotonic() + max(1, timeout_sec)
    last_resp: dict = {"chat_history": "no changes"}
    while time.monotonic() < deadline:
        rem = max(1.0, deadline - time.monotonic())
        resp = await client.get_reply(chat_id, wait=True, timeout=min(rem, 15.0))
        last_resp = resp if isinstance(resp, dict) else {"chat_history": str(resp)}
        hist = str(last_resp.get("chat_history", ""))
        if hist not in ("no changes", "chat switch", ""):
            return last_resp
    return last_resp


def _has_real_reply(resp: dict, expected_user: str = "") -> bool:
    if not isinstance(resp, dict):
        return False
    posts = resp.get("posts")
    if not isinstance(posts, dict):
        return False
    for _, post in posts.items():
        if not isinstance(post, dict):
            continue
        uname = str(post.get("user_name", "")).strip().lower()
        msg = str(post.get("message", "") or "")
        if expected_user and uname != expected_user.strip().lstrip("@").lower():
            continue
        if not msg:
            continue
        if "response in progress" in msg.lower():
            continue
        return True
    return False


async def run_one_scenario(
    client: ColloquiumClient,
    project_id: int,
    scenario: dict[str, Any],
    wait_timeout: int,
    actor_mention: str,
) -> dict[str, Any]:
    started = int(time.time())
    sid = str(scenario["id"])
    chat_desc = f"cache-phase1-test:{sid}:{_iso_now()}"
    await client.select_project(project_id)
    chat_id = await client.create_chat(chat_desc)

    turns_out: list[dict[str, Any]] = []
    status = "ok"
    error = ""
    try:
        for idx, turn in enumerate(scenario.get("turns", []), start=1):
            prompt = f"{actor_mention} {READONLY_GUARD}\n\nTask:\n{turn}"
            await client.post_message(chat_id, prompt)
            reply = await _wait_until_message(client, chat_id, wait_timeout)
            latest = _extract_latest_message(reply)
            ok_reply = _has_real_reply(reply, expected_user=actor_mention)
            if not ok_reply:
                status = "failed"
                error = f"no_real_reply:{actor_mention}"
            turns_out.append(
                {
                    "turn": idx,
                    "prompt": turn,
                    "reply_excerpt": latest[:400],
                    "reply_kind": str(reply.get("chat_history", ""))[:40] if isinstance(reply, dict) else "",
                    "has_real_reply": ok_reply,
                }
            )
    except Exception as exc:  # noqa: BLE001
        status = "failed"
        error = f"{type(exc).__name__}: {exc}"

    finished = int(time.time())
    return {
        "run_id": f"{_iso_now()}-{sid}",
        "scenario_id": sid,
        "project_id": project_id,
        "chat_id": chat_id,
        "turn_count": len(turns_out),
        "status": status,
        "error": error,
        "started_at": started,
        "finished_at": finished,
        "turns": turns_out,
    }


async def _amain(args: argparse.Namespace) -> int:
    password, _ = cq_cred.resolve_password(args.password, args.password_file)
    if not password:
        print("ERROR: password is required (same sources as mcp-tools).", file=sys.stderr)
        return 2

    client = ColloquiumClient(base_url=args.url, username=args.username, password=password)
    try:
        projects = await client.list_projects()
        if not projects:
            print("ERROR: no projects available", file=sys.stderr)
            return 3
        project_id = int(args.project_id) if args.project_id > 0 else int(projects[0].get("id", 0))
        if project_id <= 0:
            print("ERROR: failed to resolve project_id", file=sys.stderr)
            return 4

        scenarios = SCENARIOS[: max(1, args.max_scenarios)]
        results = []
        for scenario in scenarios:
            res = await run_one_scenario(client, project_id, scenario, args.wait_timeout, args.actor_mention)
            results.append(res)
            print(
                json.dumps(
                    {
                        "scenario_id": res["scenario_id"],
                        "chat_id": res["chat_id"],
                        "status": res["status"],
                        "turn_count": res["turn_count"],
                        "error": res["error"],
                    },
                    ensure_ascii=False,
                )
            )

        payload = {
            "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "base_url": args.url,
            "project_id": project_id,
            "scenario_count": len(results),
            "results": results,
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        if args.out:
            out_path = Path(args.out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(text + "\n", encoding="utf-8")
            print(f"Saved report: {out_path}")
        else:
            print(text)
        return 0
    finally:
        await client.aclose()


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase-1 read-only scenario runner")
    ap.add_argument("--url", default=os.environ.get("COLLOQUIUM_URL", "http://localhost:8008"))
    ap.add_argument("--username", default=os.environ.get("COLLOQUIUM_USERNAME", "copilot"))
    ap.add_argument("--password", default=os.environ.get("COLLOQUIUM_PASSWORD", ""))
    ap.add_argument("--password-file", default=cq_cred.default_password_file_for_cli())
    ap.add_argument("--project-id", type=int, default=0, help="0 means first available project")
    ap.add_argument("--max-scenarios", type=int, default=3, help="1..len(SCENARIOS)")
    ap.add_argument("--wait-timeout", type=int, default=50, help="Max seconds per turn wait")
    ap.add_argument("--actor-mention", default="@grok4f", help="LLM mention trigger, e.g. @grok4f")
    ap.add_argument("--out", default="", help="Optional JSON report path")
    args = ap.parse_args()
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
