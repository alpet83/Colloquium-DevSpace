from __future__ import annotations

from typing import Any, Awaitable, Callable

from mcp.types import CallToolResult, Tool  # type: ignore[import]

from cqds_helpers import _json_text
from cqds_run_ctx import RunContext

LegacyHandler = Callable[[str, dict[str, Any], RunContext], Awaitable[CallToolResult | None]]


def _result_to_payload(res: CallToolResult) -> dict[str, Any]:
    text = ""
    if res.content:
        block = res.content[0]
        text = getattr(block, "text", str(block))
    ok = not bool(getattr(res, "isError", False))
    return {"ok": ok, "legacy_result": text}


def make_ctl_tool(*, name: str, headline: str, actions_enum: list[str]) -> Tool:
    description = (
        f"{headline} "
        "BEST: batch — requests=[{{action,args}},…] for 2+ steps (fewer rounds, less rate-limit risk). "
        "Single: action + args."
    )
    return Tool(
        name=name,
        description=description,
        inputSchema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": sorted(actions_enum),
                    "description": "Single-step only. Prefer requests[] if multiple operations.",
                },
                "args": {
                    "type": "object",
                    "description": "Payload for action (see cq_help tool_ref=<this_tool>#<action>).",
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
                    "description": "Preferred: ordered steps; each step {action, args}.",
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


def _norm_action(raw: str) -> str:
    return str(raw or "").strip().lower().replace("-", "_")


async def handle_ctl(
    tool_name: str,
    name: str,
    arguments: dict[str, Any],
    ctx: RunContext,
    *,
    legacy_handle: LegacyHandler,
    action_to_legacy: dict[str, str],
) -> CallToolResult | None:
    if name != tool_name:
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
            act = _norm_action(str(req.get("action", "")))
            legacy = action_to_legacy.get(act)
            raw_args = req.get("args")
            if raw_args is None:
                raw_args = {}
            if not isinstance(raw_args, dict):
                results.append({"index": index, "ok": False, "error": "args must be an object", "action": act})
                if stop_on_error:
                    break
                continue
            if not legacy:
                results.append(
                    {
                        "index": index,
                        "ok": False,
                        "action": act,
                        "error": f"unknown action '{act}'",
                        "allowed": sorted(action_to_legacy),
                    }
                )
                if stop_on_error:
                    break
                continue
            try:
                res = await legacy_handle(legacy, raw_args, ctx)
                if res is None:
                    results.append({"index": index, "ok": False, "action": act, "error": "handler returned None"})
                else:
                    row = _result_to_payload(res)
                    row["index"] = index
                    row["action"] = act
                    results.append(row)
            except Exception as exc:  # noqa: BLE001
                results.append({"index": index, "ok": False, "action": act, "error": str(exc)})
            if stop_on_error and results and not results[-1].get("ok"):
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

    act = _norm_action(str(arguments.get("action", "")))
    if not act:
        raise ValueError("Provide action+args, or non-empty requests[] for batch mode")

    legacy = action_to_legacy.get(act)
    if not legacy:
        raise ValueError(f"Unknown action '{act}'. Allowed: {sorted(action_to_legacy)}")

    raw_args = arguments.get("args")
    if raw_args is None:
        raw_args = {}
    if not isinstance(raw_args, dict):
        raise ValueError("args must be an object")

    res = await legacy_handle(legacy, raw_args, ctx)
    if res is None:
        raise ValueError("handler returned None")
    payload = _result_to_payload(res)
    return _json_text({"ok": payload["ok"], "batch": False, "action": act, **payload})
