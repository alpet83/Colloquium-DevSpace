from __future__ import annotations

import json
from typing import Any

from mcp.server import Server  # type: ignore[import]
from mcp.types import CallToolResult, TextContent, Tool  # type: ignore[import]

from pathlib import Path

from lib.version_guard import VersionGuard

from .basic_logger import make_logger
from .config import default_data_dir, load_policy, policy_meta, workspace_discovery_debug
from .errors import EditorError
from .service import EditorService
from .storage import Storage

_MCP_OBSOLETE_RESTART_WARN = "WARN: obsolete MCP server was used, restart for check new version."
_VERSION_GUARD = VersionGuard(
    base_dir=Path(__file__).resolve().parents[1],
    message=_MCP_OBSOLETE_RESTART_WARN,
    track_new_modules=True,
    check_interval_sec=1.0,
)


def _result(payload: dict[str, Any], *, is_error: bool = False) -> CallToolResult:
    warn = _VERSION_GUARD.get_warning()
    if warn:
        if isinstance(payload, dict):
            warnings = payload.get("warnings")
            if not isinstance(warnings, list):
                warnings = []
            if warn not in warnings:
                warnings.append(warn)
            payload = dict(payload)
            payload["warnings"] = warnings
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
        isError=is_error,
    )


def create_server() -> Server:
    data_dir = default_data_dir()
    policy = load_policy(data_dir)
    storage = Storage(data_dir, policy)
    service = EditorService(storage, policy)
    log = make_logger(data_dir, "text_editor_server")
    meta = policy_meta(data_dir)
    discovery = workspace_discovery_debug()
    log.info(
        "workspace_discovery cwd=%s workspace_candidates=%s env_hints=%s",
        str(discovery.get("cwd") or ""),
        discovery.get("workspace_candidates") or [],
        discovery.get("env_hints") or {},
    )
    log.info(
        "server_initialized data_dir=%s allowed_roots=%s max_file_size=%s policy_source=%s workspace_file=%s",
        str(data_dir),
        policy.allowed_roots,
        policy.max_file_size_bytes,
        str(meta.get("source") or "unknown"),
        str(meta.get("workspace_file") or ""),
    )

    server = Server("cqds-text-editor")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="session_open",
                description="Open or reopen a text editing session by path.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "profile_id": {"type": "string"},
                        "profile_auto": {"type": "boolean", "default": True},
                        "response_mode_default": {"type": "string"},
                        "capabilities_hint": {"type": "string"},
                        "include_recent_ops": {"type": "boolean", "default": True},
                        "recent_ops_limit": {"type": "integer", "default": 3},
                    },
                    "required": ["path"],
                },
            ),
            Tool(
                name="session_cmd",
                description="Execute a session command (get_view/search/replace/undo/redo/save/op_help).",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "expected_revision": {"type": "integer"},
                        "op": {"type": "string"},
                        "op_args": {"type": "object", "default": {}},
                        "dry_run": {"type": "boolean", "default": False},
                        "confirm": {"type": "boolean", "default": False},
                        "response_mode": {"type": "string"},
                        "response_as": {"type": "string"},
                    },
                    "required": ["op"],
                },
            ),
            Tool(
                name="session_mod",
                description="Derive and run a modified command from last successful session command.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "derived_from": {"type": "string", "default": "last_success"},
                        "run_mode": {"type": "string", "default": "execute"},
                        "target_op": {"type": "string"},
                        "expected_revision": {"type": "integer"},
                        "response_mode": {"type": "string"},
                        "dry_run": {"type": "boolean"},
                        "op_args": {"type": "object"},
                    },
                    "required": ["session_id"],
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
        try:
            log.info("tool_call tool=%s op=%s", name, str(arguments.get("op") or ""))
            if name == "session_open":
                payload = service.open_session(arguments)
                log.info("tool_call_ok tool=%s session_id=%s", name, str(payload.get("session_id") or ""))
                return _result(payload)
            if name == "session_cmd":
                payload = service.execute(arguments)
                log.info("tool_call_ok tool=%s op=%s session_id=%s", name, str(arguments.get("op") or ""), str(arguments.get("session_id") or ""))
                return _result(payload)
            if name == "session_mod":
                payload = service.execute_mod(arguments)
                log.info("tool_call_ok tool=%s session_id=%s", name, str(arguments.get("session_id") or ""))
                return _result(payload)
            log.warn("tool_unknown tool=%s", name)
            return _result({"ok": False, "error": {"class": "validation", "code": "unknown_tool", "message": f"Unknown tool: {name}"}}, is_error=True)
        except EditorError as exc:
            log.warn("tool_call_error tool=%s class=%s code=%s session_id=%s", name, exc.err_class, exc.code, str(arguments.get("session_id") or ""))
            return _result({"ok": False, "error": exc.to_payload(), "session_id": arguments.get("session_id")}, is_error=True)
        except Exception as exc:  # noqa: BLE001
            log.error("tool_call_exception tool=%s session_id=%s error=%s", name, str(arguments.get("session_id") or ""), str(exc))
            return _result(
                {
                    "ok": False,
                    "error": {
                        "class": "internal",
                        "code": "internal_error",
                        "message": str(exc),
                        "retryable": False,
                        "details": {},
                    },
                    "session_id": arguments.get("session_id"),
                },
                is_error=True,
            )

    return server

