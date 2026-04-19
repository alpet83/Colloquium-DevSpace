from __future__ import annotations

from typing import Any

from mcp.types import CallToolResult, Tool  # type: ignore[import]

from cqds_helpers import _json_text
from cqds_run_ctx import RunContext

from runtime_help_data import (
    COLLOQUIUM_CTLS,
    CQ_HELP_SELF,
    DOCKER_CTL_ACTIONS,
    DOCKER_CTL_OVERVIEW,
    HELP_CATALOG,
    PROCESS_ACTIONS,
    PROCESS_CTL_OVERVIEW,
    _ctl_action_index,
    action_index,
    docker_action_index,
)


TOOLS: list[Tool] = [
    Tool(
        name="cq_help",
        description=(
            "Manuals (tool_ref). Catalog: omit tool_ref. "
            "Live core status: tool_ref=cq_help#core_status (GET /api/core/status). "
            "Unified Colloquium: cq_chat_ctl, cq_project_ctl, cq_files_ctl, cq_exec_ctl (+ #action). "
            "Host: cq_process_ctl, cq_docker_ctl. Prefer requests[] batch on *_ctl when 2+ steps."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "tool_ref": {
                    "type": "string",
                    "description": (
                        "e.g. cq_process_ctl, cq_docker_ctl#compose, cq_help, cq_help#core_status. "
                        "Omit or empty string → catalog."
                    ),
                    "default": "",
                },
                "include_examples": {
                    "type": "boolean",
                    "description": "Include example payloads for actions when available.",
                    "default": True,
                },
            },
            "required": [],
        },
    )
]


def _parse_tool_ref(raw: str | None) -> tuple[str | None, str | None]:
    if raw is None:
        return None, None
    s = str(raw).strip()
    if not s:
        return None, None
    if "#" in s:
        base, _, frag = s.partition("#")
        base = base.strip() or None
        frag = frag.strip().lower() or None
        return base, frag
    return s.strip(), None


def _action_block(action: str, include_examples: bool) -> dict[str, Any]:
    act = PROCESS_ACTIONS.get(action)
    if act is None:
        return {
            "error": "unknown_action",
            "action": action,
            "known_actions": sorted(PROCESS_ACTIONS.keys()),
        }
    out: dict[str, Any] = {"action": action, "see_tool_ref": f"cq_process_ctl#{action}"}
    if include_examples:
        out["detail"] = act
    else:
        slim = dict(act)
        slim.pop("examples", None)
        out["detail"] = slim
    out["back_to_overview"] = "cq_process_ctl"
    return out


def _colloquium_ctl_action_block(
    base: str, actions: dict[str, dict[str, str]], action: str
) -> dict[str, Any]:
    act = actions.get(action)
    if act is None:
        return {
            "error": "unknown_action",
            "action": action,
            "known_actions": sorted(actions),
        }
    return {
        "action": action,
        "see_tool_ref": f"{base}#{action}",
        "detail": dict(act),
        "back_to_overview": base,
    }


def _docker_action_block(action: str, include_examples: bool) -> dict[str, Any]:
    act = DOCKER_CTL_ACTIONS.get(action)
    if act is None:
        return {
            "error": "unknown_action",
            "action": action,
            "known_actions": sorted(DOCKER_CTL_ACTIONS.keys()),
        }
    out: dict[str, Any] = {"action": action, "see_tool_ref": f"cq_docker_ctl#{action}"}
    out["detail"] = dict(act)
    if not include_examples:
        out["detail"].pop("examples", None)
    out["back_to_overview"] = "cq_docker_ctl"
    return out


def _build_payload(tool_ref_raw: str | None, include_examples: bool) -> dict[str, Any]:
    base, frag = _parse_tool_ref(tool_ref_raw)

    if base is None and frag is None:
        return {
            "kind": "catalog",
            "hint": (
                "tool_ref examples: cq_files_ctl, cq_project_ctl#list_projects, cq_docker_ctl#compose, cq_process_ctl#spawn."
            ),
            "items": HELP_CATALOG,
        }

    if frag and not base:
        return {
            "error": "invalid_tool_ref",
            "message": "Fragment #action requires a tool name before #, e.g. cq_process_ctl#spawn.",
            "catalog": HELP_CATALOG,
        }

    assert base is not None

    if base == "cq_help":
        if frag:
            return {
                "error": "unknown_fragment",
                "tool_ref": base,
                "fragment": frag,
                "hint": (
                    "Use cq_help#core_status for live GET /api/core/status (handled before static manuals). "
                    "Otherwise omit #fragment (tool_ref=cq_help only)."
                ),
            }
        return {"kind": "manual", "tool_ref": "cq_help", "content": CQ_HELP_SELF}

    if base == "cq_process_ctl":
        if frag:
            block = _action_block(frag, include_examples)
            if "error" in block:
                return {
                    "kind": "manual",
                    "tool_ref": f"cq_process_ctl#{frag}",
                    **block,
                    "overview_ref": "cq_process_ctl",
                }
            return {
                "kind": "manual",
                "tool_ref": f"cq_process_ctl#{frag}",
                "overview": PROCESS_CTL_OVERVIEW,
                **block,
            }
        return {
            "kind": "manual",
            "tool_ref": "cq_process_ctl",
            "overview": PROCESS_CTL_OVERVIEW,
            "actions": action_index(),
            "hint": "For one action only, use cq_help with tool_ref=cq_process_ctl#<action>.",
        }

    if base == "cq_docker_ctl":
        if frag:
            block = _docker_action_block(frag, include_examples)
            if "error" in block:
                return {
                    "kind": "manual",
                    "tool_ref": f"cq_docker_ctl#{frag}",
                    **block,
                    "overview_ref": "cq_docker_ctl",
                }
            return {
                "kind": "manual",
                "tool_ref": f"cq_docker_ctl#{frag}",
                "overview": DOCKER_CTL_OVERVIEW,
                **block,
            }
        return {
            "kind": "manual",
            "tool_ref": "cq_docker_ctl",
            "overview": DOCKER_CTL_OVERVIEW,
            "actions": docker_action_index(),
            "hint": "Use cq_help tool_ref=cq_docker_ctl#<action> for one branch.",
        }

    for spec in COLLOQUIUM_CTLS:
        ctl_base = str(spec["base"])
        if base != ctl_base:
            continue
        actions = spec["actions"]
        assert isinstance(actions, dict)
        overview = spec["overview"]
        if frag:
            block = _colloquium_ctl_action_block(ctl_base, actions, frag)
            if "error" in block:
                return {
                    "kind": "manual",
                    "tool_ref": f"{ctl_base}#{frag}",
                    **block,
                    "overview_ref": ctl_base,
                }
            return {
                "kind": "manual",
                "tool_ref": f"{ctl_base}#{frag}",
                "overview": overview,
                **block,
            }
        return {
            "kind": "manual",
            "tool_ref": ctl_base,
            "overview": overview,
            "actions": _ctl_action_index(actions, ctl_base),
            "hint": f"Use cq_help tool_ref={ctl_base}#<action> for one branch.",
        }

    return {
        "error": "unknown_tool",
        "tool_ref": base,
        "fragment": frag,
        "catalog": HELP_CATALOG,
    }


async def handle(
    name: str, arguments: dict[str, Any], ctx: RunContext | None = None
) -> CallToolResult | None:
    if name != "cq_help":
        return None

    # Некоторые MCP-клиенты при пустой схеме шлют arguments=null вместо {}.
    if not isinstance(arguments, dict):
        arguments = {}

    include_examples = bool(arguments.get("include_examples", True))
    tool_ref = arguments.get("tool_ref", "")
    raw = tool_ref if tool_ref is not None else ""
    base, frag = _parse_tool_ref(raw)
    if base == "cq_help" and frag == "core_status":
        if ctx is None:
            return _json_text(
                {
                    "ok": False,
                    "kind": "core_status",
                    "error": "no_run_context",
                    "message": "cq_help#core_status needs Colloquium client (cqds_mcp_mini).",
                }
            )
        try:
            core = await ctx.client.get_core_status()
        except Exception as exc:  # noqa: BLE001
            return _json_text(
                {
                    "ok": False,
                    "kind": "core_status",
                    "tool_ref": "cq_help#core_status",
                    "error": str(exc),
                }
            )
        return _json_text(
            {
                "ok": True,
                "kind": "core_status",
                "tool_ref": "cq_help#core_status",
                "core": core,
                "hint": "Same payload as GET /api/core/status; manuals remain under tool_ref=cq_help.",
            }
        )

    payload = _build_payload(raw, include_examples)
    return _json_text(payload)
