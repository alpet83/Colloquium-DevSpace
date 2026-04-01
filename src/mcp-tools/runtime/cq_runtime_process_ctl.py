from __future__ import annotations

from typing import Any

from mcp.types import CallToolResult, Tool  # type: ignore[import]

import cqds_host
import cqds_process
from cqds_helpers import _json_text
from cqds_run_ctx import RunContext


_ACTION_TO_LEGACY = {
    False: {
        "spawn": "cq_process_spawn",
        "io": "cq_process_io",
        "wait": "cq_process_wait",
        "status": "cq_process_status",
        "kill": "cq_process_kill",
        "list": "cq_process_list",
    },
    True: {
        "spawn": "cq_host_process_spawn",
        "io": "cq_host_process_io",
        "wait": "cq_host_process_wait",
        "status": "cq_host_process_status",
        "kill": "cq_host_process_kill",
        "list": "cq_host_process_list",
    },
}


TOOLS: list[Tool] = [
    Tool(
        name="cq_process_ctl",
        description=(
            "Process control: host=false → mcp-sandbox; host=true → MCP host. "
            "BEST: batch — requests=[{host?,action,args},…] for 2+ steps (fewer rounds, less rate-limit risk). "
            "Single: host + action + args. Details: cq_help tool_ref=cq_process_ctl."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "host": {
                    "type": "boolean",
                    "description": "Single-step only. Per-item host in requests[] when batching.",
                    "default": False,
                },
                "action": {
                    "type": "string",
                    "enum": ["spawn", "io", "wait", "status", "kill", "list"],
                    "description": "Single-step only. Prefer requests[] if multiple operations.",
                },
                "args": {
                    "type": "object",
                    "description": "Action payload (cq_help cq_process_ctl#<action>).",
                    "default": {},
                },
                "requests": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "host": {"type": "boolean", "description": "default false"},
                            "action": {"type": "string"},
                            "args": {"type": "object"},
                        },
                        "required": ["action"],
                    },
                    "description": "Preferred for pipelines: multiple {host, action, args} in one call.",
                },
                "stop_on_error": {
                    "type": "boolean",
                    "description": "Batch: stop after first failure.",
                    "default": False,
                },
            },
            "required": [],
        },
    )
]


async def _delegate_legacy(host: bool, action: str, args: dict[str, Any], ctx: RunContext) -> CallToolResult:
    legacy_name = _ACTION_TO_LEGACY[host][action]
    if host:
        delegated = await cqds_host.handle(legacy_name, args, ctx)
    else:
        delegated = await cqds_process.handle(legacy_name, args, ctx)
    if delegated is None:
        raise RuntimeError(f"legacy delegation failed: {legacy_name}")
    return delegated


def _hint_for_action(action: str) -> str:
    return {
        "spawn": "next: status/wait/io",
        "io": "next: io/wait/status",
        "wait": "next: io/status/kill",
        "status": "next: io/wait/kill",
        "kill": "next: status",
        "list": "next: status/kill",
    }[action]


async def _run_one(host: bool, action: str, raw_args: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
    action = action.strip().lower()
    if action not in _ACTION_TO_LEGACY[host]:
        return {
            "ok": False,
            "host": host,
            "action": action,
            "error": "action must be one of: spawn, io, wait, status, kill, list",
        }
    try:
        result = await _delegate_legacy(host, action, raw_args, ctx)
    except Exception as e:  # noqa: BLE001 — batch must record per-step failures
        return {"ok": False, "host": host, "action": action, "error": str(e)}
    return {
        "ok": True,
        "host": host,
        "action": action,
        "hint": _hint_for_action(action),
        "legacy_result": result.content[0].text if result.content else "",
    }


async def handle(name: str, arguments: dict[str, Any], ctx: RunContext) -> CallToolResult | None:
    if name != "cq_process_ctl":
        return None

    raw_reqs = arguments.get("requests")
    if isinstance(raw_reqs, list) and len(raw_reqs) > 0:
        stop_on_error = bool(arguments.get("stop_on_error", False))
        results: list[dict[str, Any]] = []
        for index, req in enumerate(raw_reqs):
            if not isinstance(req, dict):
                results.append({"index": index, "ok": False, "error": "request must be an object"})
                if stop_on_error:
                    break
                continue
            host = bool(req.get("host", False))
            action = str(req.get("action", "")).strip().lower()
            raw_args = req.get("args")
            if raw_args is None:
                raw_args = {}
            if not isinstance(raw_args, dict):
                results.append({"index": index, "ok": False, "error": "args must be an object", "action": action})
                if stop_on_error:
                    break
                continue
            row = await _run_one(host, action, raw_args, ctx)
            row["index"] = index
            if not row.get("ok", True):
                pass
            results.append(row)
            if stop_on_error and not row.get("ok"):
                break
        all_ok = all(r.get("ok") for r in results)
        return _json_text(
            {
                "ok": True,
                "batch": True,
                "all_ok": all_ok,
                "count": len(results),
                "results": results,
            }
        )

    host = bool(arguments.get("host", False))
    action = str(arguments.get("action", "")).strip().lower()
    if not action:
        raise ValueError("Provide action+args, or non-empty requests[] for batch")

    raw_args = arguments.get("args", {})
    if raw_args is None:
        raw_args = {}
    if not isinstance(raw_args, dict):
        raise ValueError("args must be an object")

    row = await _run_one(host, action, raw_args, ctx)
    if not row.get("ok"):
        raise ValueError(str(row.get("error", "invalid request")))
    return _json_text({"ok": True, "batch": False, **row})
