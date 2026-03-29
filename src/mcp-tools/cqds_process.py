from __future__ import annotations

import base64
from typing import Any

import httpx  # type: ignore[import]
from mcp.types import CallToolResult, Tool  # type: ignore[import]

from cqds_client import (
    _DEFAULT_MCP_SERVER_URL,
    _MCP_AUTH_TOKEN,
    _PROCESS_GUID_TO_MCP_URL,
    _resolve_project_mcp_server_url,
)
from cqds_helpers import _json_text, _text
from cqds_run_ctx import RunContext


TOOLS: list[Tool] = [
    Tool(
        name="cq_process_spawn",
        description=(
            "Spawn a subprocess in mcp_server.py and return process_guid (opaque UUID, not OS pid). "
            "Use for long-running or interactive jobs; pair with cq_process_io, cq_process_wait, cq_process_status, cq_process_kill. "
            "For one-shot commands that finish quickly, cq_exec is usually simpler."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "integer", "description": "Project ID (for logging and process isolation)."},
                "command": {"type": "string", "description": "Shell command or python code (depending on engine)."},
                "engine": {"type": "string", "enum": ["bash", "python"], "description": "Execution engine (bash or python).", "default": "bash"},
                "cwd": {"type": "string", "description": "Working directory (default: current)."},
                "env": {"type": "object", "description": "Environment variables as dict (default: inherit parent)."},
                "timeout": {"type": "integer", "description": "TTL timeout in seconds (1-7200, default 3600).", "default": 3600},
            },
            "required": ["project_id", "command"],
        },
    ),
    Tool(
        name="cq_process_io",
        description=(
            "Read from and/or write to a running process via process_guid. "
            "Returns recent stdout/stderr fragments and current status."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "integer", "description": "Optional project ID; when set, route I/O to this project's mcp_server_url."},
                "process_guid": {"type": "string", "description": "Process GUID (returned by cq_process_spawn)."},
                "input": {"type": "string", "description": "Optional data to write to process stdin (base64 encoded or plain text)."},
                "read_timeout_ms": {"type": "integer", "description": "Read timeout in milliseconds (default 5000).", "default": 5000},
                "max_bytes": {"type": "integer", "description": "Max bytes to return from each buffer (default 65536).", "default": 65536},
            },
            "required": ["process_guid"],
        },
    ),
    Tool(
        name="cq_process_kill",
        description="Terminate a running process by sending a signal (SIGTERM or SIGKILL).",
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "integer", "description": "Optional project ID; when set, route signal to this project's mcp_server_url."},
                "process_guid": {"type": "string", "description": "Process GUID to terminate."},
                "signal": {"type": "string", "enum": ["SIGTERM", "SIGKILL"], "description": "Signal to send (default SIGTERM).", "default": "SIGTERM"},
            },
            "required": ["process_guid"],
        },
    ),
    Tool(
        name="cq_process_status",
        description="Get current status of a process (alive, exit_code, timestamps, runtime_ms, cpu_time_ms).",
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "integer", "description": "Optional project ID; when set, query this project's mcp_server_url."},
                "process_guid": {"type": "string", "description": "Process GUID to query."},
            },
            "required": ["process_guid"],
        },
    ),
    Tool(
        name="cq_process_list",
        description="List all processes, optionally filtered by project_id.",
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "integer", "description": "Optional project ID to filter processes."}
            },
        },
    ),
    Tool(
        name="cq_process_wait",
        description="Wait for a process condition (output or exit) with timeout. Non-blocking poll.",
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "integer", "description": "Optional project ID; when set, wait via this project's mcp_server_url."},
                "process_guid": {"type": "string", "description": "Process GUID to wait for."},
                "wait_timeout_ms": {"type": "integer", "description": "Wait timeout in milliseconds (default 30000).", "default": 30000},
                "wait_condition": {"type": "string", "enum": ["any_output", "finished"], "description": "Condition: any_output (stdout/stderr available) or finished (process exited).", "default": "any_output"},
            },
            "required": ["process_guid"],
        },
    ),
]


async def handle(name: str, arguments: dict[str, Any], ctx: RunContext) -> CallToolResult | None:
    client = ctx.client

    if name == "cq_process_spawn":
        project_id = int(arguments["project_id"])
        command = str(arguments["command"])
        engine = str(arguments.get("engine", "bash"))
        cwd = arguments.get("cwd")
        env = arguments.get("env")
        timeout = int(arguments.get("timeout", 3600))
        mcp_server_url = await _resolve_project_mcp_server_url(client, project_id)
        async with httpx.AsyncClient() as raw_client:
            resp = await raw_client.post(
                f"{mcp_server_url}/process/spawn",
                json={
                    "project_id": project_id,
                    "command": command,
                    "engine": engine,
                    "cwd": cwd,
                    "env": env,
                    "timeout": timeout,
                },
                headers={"Authorization": f"Bearer {_MCP_AUTH_TOKEN}"},
                timeout=30.0,
            )
        resp.raise_for_status()
        result = resp.json()
        process_guid = str(result.get("process_guid") or "")
        if process_guid:
            _PROCESS_GUID_TO_MCP_URL[process_guid] = mcp_server_url
        return _json_text(result)

    if name == "cq_process_io":
        process_guid = str(arguments.get("process_guid") or "")
        if not process_guid:
            return _text("Missing required argument: process_guid")
        input_data = arguments.get("input")
        read_timeout_ms = int(arguments.get("read_timeout_ms", 5000))
        max_bytes = int(arguments.get("max_bytes", 65536))
        project_id = arguments.get("project_id")
        if project_id is not None:
            mcp_server_url = await _resolve_project_mcp_server_url(client, int(project_id))
        else:
            mcp_server_url = _PROCESS_GUID_TO_MCP_URL.get(process_guid, _DEFAULT_MCP_SERVER_URL)
        async with httpx.AsyncClient() as raw_client:
            resp = await raw_client.post(
                f"{mcp_server_url}/process/io",
                json={
                    "process_guid": process_guid,
                    "input": input_data,
                    "read_timeout_ms": read_timeout_ms,
                    "max_bytes": max_bytes,
                },
                headers={"Authorization": f"Bearer {_MCP_AUTH_TOKEN}"},
                timeout=30.0,
            )
        resp.raise_for_status()
        result = resp.json()
        if result.get("stdout_fragment"):
            try:
                result["stdout_fragment"] = base64.b64decode(result["stdout_fragment"]).decode(
                    "utf-8", errors="replace"
                )
            except Exception:
                pass
        if result.get("stderr_fragment"):
            try:
                result["stderr_fragment"] = base64.b64decode(result["stderr_fragment"]).decode(
                    "utf-8", errors="replace"
                )
            except Exception:
                pass
        return _json_text(result)

    if name == "cq_process_kill":
        process_guid = str(arguments.get("process_guid") or "")
        if not process_guid:
            return _text("Missing required argument: process_guid")
        signal_name = str(arguments.get("signal", "SIGTERM"))
        project_id = arguments.get("project_id")
        if project_id is not None:
            mcp_server_url = await _resolve_project_mcp_server_url(client, int(project_id))
        else:
            mcp_server_url = _PROCESS_GUID_TO_MCP_URL.get(process_guid, _DEFAULT_MCP_SERVER_URL)
        async with httpx.AsyncClient() as raw_client:
            resp = await raw_client.post(
                f"{mcp_server_url}/process/kill",
                json={"process_guid": process_guid, "signal": signal_name},
                headers={"Authorization": f"Bearer {_MCP_AUTH_TOKEN}"},
                timeout=30.0,
            )
        resp.raise_for_status()
        _PROCESS_GUID_TO_MCP_URL.pop(process_guid, None)
        return _json_text(resp.json())

    if name == "cq_process_status":
        process_guid = str(arguments.get("process_guid") or "")
        if not process_guid:
            return _text("Missing required argument: process_guid")
        project_id = arguments.get("project_id")
        if project_id is not None:
            mcp_server_url = await _resolve_project_mcp_server_url(client, int(project_id))
        else:
            mcp_server_url = _PROCESS_GUID_TO_MCP_URL.get(process_guid, _DEFAULT_MCP_SERVER_URL)
        async with httpx.AsyncClient() as raw_client:
            resp = await raw_client.get(
                f"{mcp_server_url}/process/status",
                params={"process_guid": process_guid},
                headers={"Authorization": f"Bearer {_MCP_AUTH_TOKEN}"},
                timeout=30.0,
            )
        resp.raise_for_status()
        return _json_text(resp.json())

    if name == "cq_process_list":
        project_id = arguments.get("project_id")
        params: dict[str, Any] = {}
        if project_id:
            params["project_id"] = int(project_id)
            mcp_server_url = await _resolve_project_mcp_server_url(client, int(project_id))
        else:
            mcp_server_url = _DEFAULT_MCP_SERVER_URL
        async with httpx.AsyncClient() as raw_client:
            resp = await raw_client.get(
                f"{mcp_server_url}/process/list",
                params=params,
                headers={"Authorization": f"Bearer {_MCP_AUTH_TOKEN}"},
                timeout=30.0,
            )
        resp.raise_for_status()
        return _json_text(resp.json())

    if name == "cq_process_wait":
        process_guid = str(arguments.get("process_guid") or "")
        if not process_guid:
            return _text("Missing required argument: process_guid")
        wait_timeout_ms = int(arguments.get("wait_timeout_ms", 30000))
        wait_condition = str(arguments.get("wait_condition", "any_output"))
        project_id = arguments.get("project_id")
        if project_id is not None:
            mcp_server_url = await _resolve_project_mcp_server_url(client, int(project_id))
        else:
            mcp_server_url = _PROCESS_GUID_TO_MCP_URL.get(process_guid, _DEFAULT_MCP_SERVER_URL)
        async with httpx.AsyncClient() as raw_client:
            resp = await raw_client.post(
                f"{mcp_server_url}/process/wait",
                json={
                    "process_guid": process_guid,
                    "wait_timeout_ms": wait_timeout_ms,
                    "wait_condition": wait_condition,
                },
                headers={"Authorization": f"Bearer {_MCP_AUTH_TOKEN}"},
                timeout=(wait_timeout_ms / 1000.0) + 10,
            )
        resp.raise_for_status()
        return _json_text(resp.json())

    return None