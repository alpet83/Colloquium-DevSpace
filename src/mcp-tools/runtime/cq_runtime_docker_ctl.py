from __future__ import annotations

from typing import Any

from mcp.types import CallToolResult, Tool  # type: ignore[import]

import cqds_docker
from cqds_helpers import _json_text
from cqds_run_ctx import RunContext

_DOCKER_ACTIONS = frozenset({"compose", "cli", "cqds_ctl", "exec", "inspect", "logs"})


async def _dispatch_one(action: str, args: dict[str, Any]) -> dict[str, Any]:
    action = action.strip().lower()
    if action not in _DOCKER_ACTIONS:
        return {"ok": False, "error": f"unknown action '{action}'", "allowed": sorted(_DOCKER_ACTIONS)}

    if action == "compose":
        return await cqds_docker.docker_compose_run(args)

    if action == "cli":
        return await cqds_docker.docker_cli_run(args)

    if action == "cqds_ctl":
        cmd = str(args.get("command", "status"))
        services = [str(s) for s in (args.get("services") or [])]
        timeout = max(10, min(int(args.get("timeout", 90)), 600))
        wait = bool(args.get("wait", False))
        out = await cqds_docker.invoke_cqds_ctl(cmd, services, timeout, wait)
        if out["ok"]:
            return {"ok": True, "response": out["data"], "request": args}
        return {
            "ok": False,
            "error": out.get("error"),
            "stdout": out.get("stdout"),
            "stderr": out.get("stderr"),
            "request": args,
        }

    if action == "exec":
        return await cqds_docker.docker_exec_one(args)

    if action == "inspect":
        return await cqds_docker.docker_inspect_run(args)

    if action == "logs":
        return await cqds_docker.docker_logs_run(args)

    return {"ok": False, "error": "unreachable"}


TOOLS: list[Tool] = [
    Tool(
        name="cq_docker_ctl",
        description=(
            "Docker on MCP host: compose (stack in a directory), cli (raw docker argv, e.g. ps), "
            "cqds_ctl, exec, inspect, logs. "
            "BEST: batch — pass requests=[{action,args},…] for 2+ steps (fewer tool rounds, less rate-limit risk). "
            "Single step: action + args only."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["compose", "cli", "cqds_ctl", "exec", "inspect", "logs"],
                    "description": "Single-step only. Prefer requests[] if more than one operation.",
                },
                "args": {
                    "type": "object",
                    "description": "Payload for action (cq_help tool_ref=cq_docker_ctl).",
                    "default": {},
                },
                "requests": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string"},
                            "args": {"type": "object"},
                        },
                        "required": ["action"],
                    },
                    "description": "Preferred for multiple steps: one MCP call, ordered {action, args}.",
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


async def handle(name: str, arguments: dict[str, Any], ctx: RunContext) -> CallToolResult | None:
    del ctx
    if name != "cq_docker_ctl":
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
            act = str(req.get("action", "")).strip().lower()
            args = req.get("args")
            if args is None:
                args = {}
            if not isinstance(args, dict):
                results.append({"index": index, "ok": False, "error": "args must be an object", "action": act})
                if stop_on_error:
                    break
                continue
            row = await _dispatch_one(act, args)
            row["index"] = index
            row["action"] = act
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

    action = str(arguments.get("action", "")).strip().lower()
    if not action:
        raise ValueError("Provide action+args, or non-empty requests[] for batch mode")

    args = arguments.get("args")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ValueError("args must be an object")

    out = await _dispatch_one(action, args)
    return _json_text({"ok": True, "batch": False, "action": action, "result": out})
