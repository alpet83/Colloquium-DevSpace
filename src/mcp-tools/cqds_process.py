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
from cqds_helpers import _json_text
from cqds_run_ctx import RunContext


# ---------------------------------------------------------------------------
# Module-internal HTTP helpers for mcp_server.py /process/* endpoints
# ---------------------------------------------------------------------------

async def _proc_url(client: Any, process_guid: str, project_id: Any) -> str:
    """Resolve the mcp_server_url for a process/project context."""
    if project_id is not None:
        return await _resolve_project_mcp_server_url(client, int(project_id))
    return _PROCESS_GUID_TO_MCP_URL.get(process_guid, _DEFAULT_MCP_SERVER_URL)


async def _mcp_post(
    url: str, path: str, body: dict[str, Any], timeout: float = 30.0
) -> dict[str, Any]:
    async with httpx.AsyncClient() as raw:
        resp = await raw.post(
            f"{url}{path}",
            json=body,
            headers={"Authorization": f"Bearer {_MCP_AUTH_TOKEN}"},
            timeout=timeout,
        )
    resp.raise_for_status()
    return resp.json()  # type: ignore[no-any-return]


async def _mcp_get(
    url: str, path: str, params: dict[str, Any], timeout: float = 30.0
) -> dict[str, Any]:
    async with httpx.AsyncClient() as raw:
        resp = await raw.get(
            f"{url}{path}",
            params=params,
            headers={"Authorization": f"Bearer {_MCP_AUTH_TOKEN}"},
            timeout=timeout,
        )
    resp.raise_for_status()
    return resp.json()  # type: ignore[no-any-return]


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
        timeout = int(arguments.get("timeout", 3600))
        mcp_server_url = await _resolve_project_mcp_server_url(client, project_id)
        result = await _mcp_post(mcp_server_url, "/process/spawn", {
            "project_id": project_id,
            "command": str(arguments["command"]),
            "engine": str(arguments.get("engine", "bash")),
            "cwd": arguments.get("cwd"),
            "env": arguments.get("env"),
            "timeout": timeout,
        })
        process_guid = str(result.get("process_guid") or "")
        if process_guid:
            _PROCESS_GUID_TO_MCP_URL[process_guid] = mcp_server_url
        return _json_text(result)

    if name == "cq_process_io":
        process_guid = str(arguments.get("process_guid") or "")
        if not process_guid:
            raise ValueError("Missing required argument: process_guid")
        mcp_server_url = await _proc_url(client, process_guid, arguments.get("project_id"))
        result = await _mcp_post(mcp_server_url, "/process/io", {
            "process_guid": process_guid,
            "input": arguments.get("input"),
            "read_timeout_ms": int(arguments.get("read_timeout_ms", 5000)),
            "max_bytes": int(arguments.get("max_bytes", 65536)),
        })
        for key in ("stdout_fragment", "stderr_fragment"):
            if result.get(key):
                try:
                    result[key] = base64.b64decode(result[key]).decode("utf-8", errors="replace")
                except Exception:
                    pass
        return _json_text(result)

    if name == "cq_process_kill":
        process_guid = str(arguments.get("process_guid") or "")
        if not process_guid:
            raise ValueError("Missing required argument: process_guid")
        mcp_server_url = await _proc_url(client, process_guid, arguments.get("project_id"))
        result = await _mcp_post(mcp_server_url, "/process/kill", {
            "process_guid": process_guid,
            "signal": str(arguments.get("signal", "SIGTERM")),
        })
        _PROCESS_GUID_TO_MCP_URL.pop(process_guid, None)
        return _json_text(result)

    if name == "cq_process_status":
        process_guid = str(arguments.get("process_guid") or "")
        if not process_guid:
            raise ValueError("Missing required argument: process_guid")
        mcp_server_url = await _proc_url(client, process_guid, arguments.get("project_id"))
        return _json_text(await _mcp_get(mcp_server_url, "/process/status", {
            "process_guid": process_guid,
        }))

    if name == "cq_process_list":
        project_id = arguments.get("project_id")
        params: dict[str, Any] = {}
        if project_id:
            params["project_id"] = int(project_id)
            mcp_server_url = await _resolve_project_mcp_server_url(client, int(project_id))
        else:
            mcp_server_url = _DEFAULT_MCP_SERVER_URL
        return _json_text(await _mcp_get(mcp_server_url, "/process/list", params))

    if name == "cq_process_wait":
        process_guid = str(arguments.get("process_guid") or "")
        if not process_guid:
            raise ValueError("Missing required argument: process_guid")
        wait_timeout_ms = int(arguments.get("wait_timeout_ms", 30000))
        mcp_server_url = await _proc_url(client, process_guid, arguments.get("project_id"))
        return _json_text(await _mcp_post(mcp_server_url, "/process/wait", {
            "process_guid": process_guid,
            "wait_timeout_ms": wait_timeout_ms,
            "wait_condition": str(arguments.get("wait_condition", "any_output")),
        }, timeout=(wait_timeout_ms / 1000.0) + 10))

    return None