from __future__ import annotations

import json
from typing import Any

from mcp.types import CallToolResult, Tool  # type: ignore[import]

from cqds_helpers import (
    _build_spawn_script_command,
    _json_text,
    _normalize_exec_result,
    _parse_exec_commands,
)
from cqds_run_ctx import RunContext


TOOLS: list[Tool] = [
    Tool(
        name="cq_exec",
        description=(
            "Execute a shell command in a project's working directory on the Colloquium/CQDS side and return stdout/stderr. "
            "Environment is Linux/bash (project container or agent workspace), not the Windows PowerShell host. "
            "Supports string command or JSON command batches in a single call. "
            "Direct HTTP — no LLM or chat round-trip. Use cq_list_projects for project_id. "
            "Returns {status, output, project}. Max timeout 300s."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "integer",
                    "description": "Project ID (from cq_list_projects).",
                },
                "command": {
                    "oneOf": [
                        {"type": "string", "description": "Single shell command (bash)."},
                        {
                            "type": "array",
                            "items": {
                                "oneOf": [
                                    {"type": "string"},
                                    {
                                        "type": "object",
                                        "properties": {
                                            "command": {"type": "string"},
                                            "timeout": {"type": "integer"},
                                        },
                                        "required": ["command"],
                                    },
                                ]
                            },
                            "description": "Batch commands, executed sequentially.",
                        },
                        {
                            "type": "object",
                            "properties": {
                                "commands": {
                                    "type": "array",
                                    "items": {
                                        "oneOf": [
                                            {"type": "string"},
                                            {
                                                "type": "object",
                                                "properties": {
                                                    "command": {"type": "string"},
                                                    "timeout": {"type": "integer"},
                                                },
                                                "required": ["command"],
                                            },
                                        ]
                                    },
                                }
                            },
                            "required": ["commands"],
                            "description": "Object form for batch execution.",
                        },
                    ],
                    "description": "Command input: string or JSON batch payload.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Max execution time in seconds (1-300, default 30).",
                    "default": 30,
                },
                "continue_on_error": {
                    "type": "boolean",
                    "description": "For batch commands: continue after a failed command (default true).",
                    "default": True,
                },
            },
            "required": ["project_id", "command"],
        },
    ),
    Tool(
        name="cq_spawn_script",
        description=(
            "Create and run a temporary script in mcp-sandbox in one call. "
            "Supports bash or python script engines. Useful for grouped commands without many cq_exec calls."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "integer",
                    "description": "Project ID (from cq_list_projects).",
                },
                "engine": {
                    "type": "string",
                    "enum": ["bash", "python"],
                    "description": "Script engine (default bash).",
                    "default": "bash",
                },
                "commands": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Script lines to write and execute.",
                },
                "script_name": {
                    "type": "string",
                    "description": "Optional temp script base name.",
                },
                "keep_file": {
                    "type": "boolean",
                    "description": "Keep temporary script after run (default false).",
                    "default": False,
                },
                "timeout": {
                    "type": "integer",
                    "description": "Execution timeout in seconds (1-300, default 60).",
                    "default": 60,
                },
            },
            "required": ["project_id", "commands"],
        },
    ),
]


async def handle(name: str, arguments: dict[str, Any], ctx: RunContext) -> CallToolResult | None:
    client = ctx.client

    if name == "cq_exec":
        project_id = int(arguments["project_id"])
        timeout = int(arguments.get("timeout", 30))
        timeout = max(1, min(timeout, 300))
        continue_on_error = bool(arguments.get("continue_on_error", True))
        command_plan = _parse_exec_commands(arguments.get("command"), timeout)

        results: list[dict[str, Any]] = []
        for command_text, command_timeout in command_plan:
            raw_result = await client.exec_command(project_id, command_text, command_timeout)
            normalized = _normalize_exec_result(raw_result, command_text, command_timeout)
            results.append(normalized)
            failed = str(normalized.get("status", "")).lower() not in {"success", "ok"}
            if failed and not continue_on_error:
                break

        if len(results) == 1:
            return _json_text(results[0])

        failures = [item for item in results if str(item.get("status", "")).lower() not in {"success", "ok"}]
        return _json_text(
            {
                "status": "partial" if failures else "success",
                "project_id": project_id,
                "count": len(results),
                "failures": len(failures),
                "results": results,
            }
        )

    if name == "cq_spawn_script":
        project_id = int(arguments["project_id"])
        commands_raw = arguments.get("commands")
        if not isinstance(commands_raw, list) or not commands_raw:
            raise ValueError("commands must be a non-empty array of strings")
        commands = [str(line) for line in commands_raw]
        engine = str(arguments.get("engine", "bash")).strip().lower()
        if engine not in {"bash", "python"}:
            raise ValueError("engine must be 'bash' or 'python'")
        script_name = str(arguments.get("script_name", "")).strip() or None
        keep_file = bool(arguments.get("keep_file", False))
        timeout = int(arguments.get("timeout", 60))
        timeout = max(1, min(timeout, 300))

        payload = {
            "engine": engine,
            "commands": commands,
            "script_name": script_name,
            "keep_file": keep_file,
        }
        runner_command = _build_spawn_script_command(payload)
        raw_result = await client.exec_command(project_id, runner_command, timeout)
        normalized = _normalize_exec_result(raw_result, f"cq_spawn_script:{engine}", timeout)

        script_result: dict[str, Any] | None = None
        try:
            if normalized.get("stdout"):
                script_result = json.loads(str(normalized["stdout"]))
        except Exception:
            script_result = None

        return _json_text(
            {
                "status": normalized.get("status"),
                "project": normalized.get("project"),
                "engine": engine,
                "script": script_result,
                "exec": normalized,
            }
        )

    return None