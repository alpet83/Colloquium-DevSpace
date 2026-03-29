# copilot_mcp_tool.py — MCP server bridging GitHub Copilot to Colloquium-DevSpace
# Place: P:\GitHub\Colloquium-DevSpace\src\mcp-tools\copilot_mcp_tool.py
#
# Usage:
#   python copilot_mcp_tool.py [--url URL] [--username USER]
#                               [--password PASS | --password-file FILE]
#                               [--chat-id ID] [--timeout SEC]
#
# Default URL: http://localhost:8008
# Credentials can also be set via env vars:
#   COLLOQUIUM_URL, COLLOQUIUM_USERNAME, COLLOQUIUM_PASSWORD,
#   COLLOQUIUM_PASSWORD_FILE, COLLOQUIUM_CHAT_ID

from __future__ import annotations

import argparse
import asyncio
import base64
from contextvars import ContextVar
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sys
import time
import textwrap
import uuid
from typing import Any
from urllib.parse import urlparse

import httpx  # type: ignore[import]
from mcp.server import Server  # type: ignore[import]
from mcp.server.stdio import stdio_server  # type: ignore[import]
from mcp.types import (  # type: ignore[import]
    CallToolResult,
    TextContent,
    Tool,
)

LOGGER = logging.getLogger("copilot_mcp_tool")
CURRENT_TOOL: ContextVar[str] = ContextVar("copilot_mcp_current_tool", default="-")


def _setup_logging() -> Path:
    default_log = Path(__file__).resolve().parent / "logs" / "copilot_mcp_tool.runtime.log"
    log_file = Path(os.environ.get("COLLOQUIUM_MCP_LOG_FILE", str(default_log))).resolve()
    log_level_name = os.environ.get("COLLOQUIUM_MCP_LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_name, logging.INFO)

    log_file.parent.mkdir(parents=True, exist_ok=True)

    LOGGER.handlers.clear()
    LOGGER.setLevel(log_level)
    LOGGER.propagate = False

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)
    LOGGER.addHandler(file_handler)

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(log_level)
    stderr_handler.setFormatter(formatter)
    LOGGER.addHandler(stderr_handler)

    return log_file


def _read_mcp_json_token() -> str | None:
    """Walk up from cwd/script dir for .vscode/mcp.json and .cursor/mcp.json (MCP_AUTH_TOKEN)."""
    script_path = str(Path(__file__).resolve())
    seen: set[Path] = set()
    candidates: list[Path] = []
    for base in (Path.cwd(), Path(__file__).resolve().parent):
        for parent in (base, *base.parents):
            for rel in (".vscode/mcp.json", ".cursor/mcp.json"):
                p = parent / rel
                if p not in seen:
                    seen.add(p)
                    candidates.append(p)
    for path in candidates:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        # Top-level env block (non-standard but convenient)
        token = (data.get("env") or {}).get("MCP_AUTH_TOKEN")
        if token:
            return str(token)
        # Per-server env: VS Code uses "servers", Cursor uses "mcpServers"
        for key in ("servers", "mcpServers"):
            block = data.get(key) or {}
            if not isinstance(block, dict):
                continue
            for server_name, server_cfg in block.items():
                if not isinstance(server_cfg, dict):
                    continue
                args = server_cfg.get("args") or []
                if not (
                    any(script_path in str(a) for a in args)
                    or server_name in ("cqds", "colloquium")
                ):
                    continue
                token = (server_cfg.get("env") or {}).get("MCP_AUTH_TOKEN")
                if token:
                    return str(token)
    return None


def _resolve_mcp_auth_token() -> str:
    """Resolve MCP_AUTH_TOKEN: env var → mcp.json → built-in default."""
    token = os.environ.get("MCP_AUTH_TOKEN") or _read_mcp_json_token()
    return token or "Grok-xAI-Agent-The-Best"


# Resolved once at startup; used by all process-management and docker-control handlers.
_MCP_AUTH_TOKEN: str = _resolve_mcp_auth_token()
_MCP_SERVER_URL: str = os.environ.get("MCP_SERVER_URL", "http://localhost:8084")


def _preview_text(text: str, limit: int = 200) -> str:
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[:limit]}..."


def _summarize_arguments(arguments: dict[str, Any]) -> str:
    if not isinstance(arguments, dict):
        return str(type(arguments).__name__)
    parts: list[str] = []
    for key in sorted(arguments.keys()):
        value = arguments[key]
        if key.lower() in {"password", "token", "authorization"}:
            parts.append(f"{key}=<redacted>")
            continue
        if isinstance(value, str):
            parts.append(f"{key}=str(len={len(value)}, preview='{_preview_text(value, 64)}')")
            continue
        if isinstance(value, list):
            parts.append(f"{key}=list(len={len(value)})")
            continue
        if isinstance(value, dict):
            parts.append(f"{key}=dict(keys={len(value)})")
            continue
        parts.append(f"{key}={value!r}")
    return ", ".join(parts)

# ---------------------------------------------------------------------------
# Colloquium HTTP client
# ---------------------------------------------------------------------------

class ColloquiumClient:
    """Async HTTP client for Colloquium-DevSpace."""

    def __init__(self, base_url: str, username: str, password: str) -> None:
        self._base = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._client = httpx.AsyncClient(
            base_url=self._base,
            follow_redirects=True,
            timeout=30.0,
            event_hooks={
                "request": [self._log_request],
                "response": [self._log_response],
            },
        )
        self._logged_in = False
        self._sync_timeout: int = 0

    def is_local_or_private_endpoint(self) -> bool:
        """Allow elevated DB actions only for local/private Colloquium endpoints."""
        try:
            host = (urlparse(self._base).hostname or "").strip().lower()
        except Exception:
            return False

        if not host:
            return False

        if host in {"localhost", "127.0.0.1", "::1", "host.docker.internal"}:
            return True

        try:
            ip = ipaddress.ip_address(host)
            return ip.is_loopback or ip.is_private
        except ValueError:
            # Non-IP hostname: treat as local only for common local suffixes.
            return host.endswith(".local") or host.endswith(".lan")

    async def _log_request(self, request: httpx.Request) -> None:
        LOGGER.info("HTTP -> %s %s", request.method, request.url)

    async def _log_response(self, response: httpx.Response) -> None:
        request = response.request
        LOGGER.info("HTTP <- %s %s status=%s", request.method, request.url, response.status_code)

    async def _ensure_login(self) -> None:
        if self._logged_in:
            return
        resp = await self._client.post(
            "/api/login",
            json={"username": self._username, "password": self._password},
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Colloquium login failed: {resp.status_code} {resp.text}"
            )
        self._logged_in = True

    async def list_chats(self) -> list[dict]:
        await self._ensure_login()
        resp = await self._client.get("/api/chat/list")
        resp.raise_for_status()
        return resp.json()

    async def create_chat(self, description: str = "MCP Session") -> int:
        await self._ensure_login()
        resp = await self._client.post(
            "/api/chat/create", json={"description": description}
        )
        resp.raise_for_status()
        return resp.json()["chat_id"]

    async def post_message(self, chat_id: int, message: str) -> dict:
        await self._ensure_login()
        resp = await self._client.post(
            "/api/chat/post", json={"chat_id": chat_id, "message": message}
        )
        resp.raise_for_status()
        return resp.json()

    async def get_reply(self, chat_id: int, wait: bool = True, timeout: float = 15.0) -> dict:
        """Poll for chat changes. Returns the raw API response."""
        await self._ensure_login()
        resp = await self._client.get(
            "/api/chat/get",
            params={"chat_id": chat_id, "wait_changes": 1 if wait else 0},
            timeout=timeout + 5.0,
        )
        resp.raise_for_status()
        return resp.json()

    async def get_history(self, chat_id: int) -> dict:
        """Fetch current chat history snapshot (no waiting)."""
        await self._ensure_login()
        resp = await self._client.get(
            "/api/chat/get",
            params={"chat_id": chat_id, "wait_changes": 0},
            timeout=15.0,
        )
        resp.raise_for_status()
        return resp.json()

    async def get_chat_stats(self, chat_id: int, since_seconds: int | None = None) -> dict:
        """Fetch aggregated usage stats for a chat via /api/chat/stats (with legacy fallback)."""
        await self._ensure_login()
        params: dict[str, Any] = {"chat_id": chat_id}
        if since_seconds is not None and since_seconds > 0:
            params["since_seconds"] = since_seconds

        resp = await self._client.get(
            "/api/chat/stats",
            params=params,
            timeout=30.0,
        )
        if resp.status_code == 404:
            # Backward compatibility for older runtime images.
            resp = await self._client.get(
                "/api/chat/get_stats",
                params=params,
                timeout=30.0,
            )
        resp.raise_for_status()
        return resp.json()

    async def list_projects(self) -> list[dict]:
        await self._ensure_login()
        resp = await self._client.get("/api/project/list")
        resp.raise_for_status()
        return resp.json()

    async def select_project(self, project_id: int) -> dict:
        """Set the active project context for the current authenticated session."""
        await self._ensure_login()
        resp = await self._client.post(
            "/api/project/select",
            json={"project_id": project_id},
        )
        resp.raise_for_status()
        return resp.json()

    async def list_files(
        self,
        project_id: int,
        modified_since: int | None = None,
        file_ids: list[int] | None = None,
        include_size: bool = False,
    ) -> list[dict]:
        """Return file index (no content) via /api/project/file_index."""
        await self._ensure_login()
        params: dict[str, Any] = {"project_id": project_id}
        if modified_since is not None:
            params["modified_since"] = modified_since
        if file_ids is not None:
            params["file_ids"] = ",".join(str(i) for i in file_ids)
        if include_size:
            params["include_size"] = 1
        resp = await self._client.get("/api/project/file_index", params=params)
        resp.raise_for_status()
        return resp.json()

    async def get_index(self, chat_id: int | None = None, project_id: int | None = None) -> dict:
        """Return the rich entity index for a chat or cached project index via /api/chat/index."""
        await self._ensure_login()
        params: dict[str, Any] = {}
        if chat_id is not None:
            params["chat_id"] = chat_id
        if project_id is not None:
            params["project_id"] = project_id
        resp = await self._client.get("/api/chat/index", params=params)
        resp.raise_for_status()
        return resp.json()

    async def get_project_status(self, project_id: int) -> dict:
        """Fetch health status and diagnostics for a project via /api/project/status."""
        await self._ensure_login()
        resp = await self._client.get(
            "/api/project/status",
            params={"project_id": project_id},
        )
        resp.raise_for_status()
        return resp.json()

    async def get_code_index(self, project_id: int, timeout: int = 300) -> dict:
        """Build and return the rich entity index for a project on demand via /api/project/code_index."""
        await self._ensure_login()
        http_timeout = httpx.Timeout(float(max(timeout, 30) + 30))
        resp = await self._client.get(
            "/api/project/code_index",
            params={"project_id": project_id, "timeout": timeout},
            timeout=http_timeout,
        )
        resp.raise_for_status()
        return resp.json()

    async def read_file(self, file_id: int) -> str:
        """Fetch raw file contents by DB file_id via /api/chat/file_contents."""
        await self._ensure_login()
        resp = await self._client.get(
            "/api/chat/file_contents",
            params={"file_id": file_id},
        )
        resp.raise_for_status()
        ct = resp.headers.get("content-type", "")
        if "application/json" in ct:
            return json.dumps(resp.json(), ensure_ascii=False, indent=2)
        return resp.text

    async def exec_command(
        self, project_id: int, command: str, timeout: int = 30
    ) -> dict:
        """Execute a shell command in a project sandbox via /api/project/exec."""
        await self._ensure_login()
        resp = await self._client.post(
            "/api/project/exec",
            json={"project_id": project_id, "command": command, "timeout": timeout},
            timeout=httpx.Timeout(timeout + 15.0),
        )
        resp.raise_for_status()
        return resp.json()

    async def smart_grep(
        self,
        project_id: int,
        query: str,
        mode: str = "code",
        profile: str = "all",
        time_strict: str | None = None,
        is_regex: bool = False,
        case_sensitive: bool = False,
        max_results: int = 100,
        context_lines: int = 0,
        include_glob: list[str] | None = None,
    ) -> dict:
        """Search occurrences in project file sets via /api/project/smart_grep."""
        await self._ensure_login()
        payload: dict[str, Any] = {
            "project_id": project_id,
            "query": query,
            "mode": mode,
            "profile": profile,
            "is_regex": is_regex,
            "case_sensitive": case_sensitive,
            "max_results": max_results,
            "context_lines": context_lines,
        }
        if time_strict:
            payload["time_strict"] = time_strict
        if include_glob:
            payload["include_glob"] = include_glob
        resp = await self._client.post("/api/project/smart_grep", json=payload)
        resp.raise_for_status()
        return resp.json()

    async def replace_file(
        self,
        project_id: int,
        file_id: int,
        old: str,
        new: str,
        is_regex: bool = False,
        case_sensitive: bool = True,
        max_replacements: int = 0,
    ) -> dict:
        """Replace text in one file via /api/project/replace."""
        await self._ensure_login()
        resp = await self._client.post(
            "/api/project/replace",
            json={
                "project_id": project_id,
                "file_id": file_id,
                "old": old,
                "new": new,
                "is_regex": is_regex,
                "case_sensitive": case_sensitive,
                "max_replacements": max_replacements,
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def aclose(self) -> None:
        await self._client.aclose()


# ---------------------------------------------------------------------------
# XML helpers
# ---------------------------------------------------------------------------

def _xml_code_file(path: str, content: str) -> str:
    return f'<code_file name="{path}">\n{content}\n</code_file>'


def _xml_patch(path: str, diff: str) -> str:
    return f'<patch name="{path}">\n{diff}\n</patch>'


def _xml_undo(file_id: int, time_back: int = 3600) -> str:
    return f'<undo file_id={file_id} time_back={time_back}>'


def _unwrap_exec_output(raw_output: str) -> dict[str, str]:
    text = str(raw_output or "")
    stdout_match = re.search(r"<stdout>(.*?)</stdout>", text, flags=re.DOTALL)
    stderr_match = re.search(r"<stderr>(.*?)</stderr>", text, flags=re.DOTALL)

    if stdout_match or stderr_match:
        return {
            "stdout": (stdout_match.group(1) if stdout_match else "").strip(),
            "stderr": (stderr_match.group(1) if stderr_match else "").strip(),
        }

    return {
        "stdout": text.strip(),
        "stderr": "",
    }


def _normalize_exec_result(result: dict[str, Any], command: str, timeout: int) -> dict[str, Any]:
    output_raw = str(result.get("output", ""))
    streams = _unwrap_exec_output(output_raw)
    normalized: dict[str, Any] = {
        "status": result.get("status"),
        "project": result.get("project"),
        "command": command,
        "timeout": timeout,
        "stdout": streams["stdout"],
        "stderr": streams["stderr"],
        "output": streams["stdout"],
        "output_raw": output_raw,
    }
    for key in ("exit_code", "signal", "duration_ms"):
        if key in result:
            normalized[key] = result[key]
    return normalized


def _parse_exec_commands(
    command_arg: Any,
    default_timeout: int,
) -> list[tuple[str, int]]:
    if isinstance(command_arg, str):
        candidate = command_arg.strip()
        if candidate.startswith("{") or candidate.startswith("["):
            try:
                parsed = json.loads(candidate)
                return _parse_exec_commands(parsed, default_timeout)
            except Exception:
                pass
        if not candidate:
            raise ValueError("command must be non-empty")
        return [(candidate, default_timeout)]

    def normalize_item(item: Any) -> tuple[str, int]:
        if isinstance(item, str):
            cmd = item.strip()
            if not cmd:
                raise ValueError("command item must be non-empty")
            return cmd, default_timeout
        if isinstance(item, dict):
            cmd = str(item.get("command", "")).strip()
            if not cmd:
                raise ValueError("command item dict requires non-empty 'command'")
            cmd_timeout = int(item.get("timeout", default_timeout))
            cmd_timeout = max(1, min(cmd_timeout, 300))
            return cmd, cmd_timeout
        raise ValueError("command item must be string or object")

    if isinstance(command_arg, list):
        if not command_arg:
            raise ValueError("command list must be non-empty")
        return [normalize_item(item) for item in command_arg]

    if isinstance(command_arg, dict):
        if "commands" in command_arg:
            commands = command_arg["commands"]
            if not isinstance(commands, list) or not commands:
                raise ValueError("command.commands must be a non-empty array")
            return [normalize_item(item) for item in commands]
        if "command" in command_arg:
            return [normalize_item(command_arg)]
        raise ValueError("command object must contain 'command' or 'commands'")

    raise ValueError("command must be string, object, or array")


def _build_spawn_script_command(script_payload: dict[str, Any]) -> str:
    encoded = base64.b64encode(json.dumps(script_payload, ensure_ascii=False).encode("utf-8")).decode("ascii")
    return (
        "python3 - <<'PY'\n"
        "import base64, json, os, subprocess, tempfile\n"
        f"cfg = json.loads(base64.b64decode('{encoded}').decode('utf-8'))\n"
        "engine = cfg.get('engine', 'bash')\n"
        "commands = cfg.get('commands', [])\n"
        "script_name = cfg.get('script_name') or f'cq_spawn_{os.getpid()}'\n"
        "keep_file = bool(cfg.get('keep_file', False))\n"
        "if engine not in ('bash', 'python'):\n"
        "    raise SystemExit('Unsupported engine, expected bash or python')\n"
        "suffix = '.py' if engine == 'python' else '.sh'\n"
        "runner = 'python3' if engine == 'python' else '/bin/bash'\n"
        "script_text = '\n'.join(commands) + '\n'\n"
        "tmp_path = os.path.join(tempfile.gettempdir(), script_name + suffix)\n"
        "with open(tmp_path, 'w', encoding='utf-8') as handle:\n"
        "    handle.write(script_text)\n"
        "if engine == 'bash':\n"
        "    os.chmod(tmp_path, 0o755)\n"
        "proc = subprocess.run([runner, tmp_path], capture_output=True, text=True)\n"
        "if (not keep_file) and os.path.exists(tmp_path):\n"
        "    os.remove(tmp_path)\n"
        "print(json.dumps({\n"
        "    'script_path': tmp_path,\n"
        "    'engine': engine,\n"
        "    'returncode': proc.returncode,\n"
        "    'stdout': proc.stdout,\n"
        "    'stderr': proc.stderr,\n"
        "    'kept': keep_file,\n"
        "}, ensure_ascii=False))\n"
        "PY"
    )


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="cq_list_chats",
        description=(
            "List all chats available in Colloquium-DevSpace. "
            "Use when you need chat_id for cq_send_message or history tools; "
            "not a substitute for cq_list_projects (projects vs chats)."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    Tool(
        name="cq_create_chat",
        description=(
            "Create a new chat in Colloquium-DevSpace. Returns the new chat_id. "
            "Use before cq_edit_file/cq_patch_file/cq_undo_file when those must post via chat messages."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "Short description / title for the new chat.",
                }
            },
            "required": [],
        },
    ),
    Tool(
        name="cq_send_message",
        description=(
            "Send a plain text message to a Colloquium chat and return immediately. "
            "Use cq_wait_reply (or cq_get_history) to read the AI response; "
            "or cq_set_sync_mode with timeout>0 so cq_send_message waits for the reply."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "chat_id": {"type": "integer", "description": "Target chat ID."},
                "message": {"type": "string", "description": "Message text to send."},
            },
            "required": ["chat_id", "message"],
        },
    ),
    Tool(
        name="cq_wait_reply",
        description=(
            "Long-poll a Colloquium chat for new AI messages (up to 15 s). "
            "Returns the latest posts or 'no changes' if nothing arrived. "
            "Use cq_get_history instead if you need to read existing messages."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "chat_id": {"type": "integer", "description": "Chat ID to poll."},
            },
            "required": ["chat_id"],
        },
    ),
    Tool(
        name="cq_get_history",
        description=(
            "Fetch the current chat history snapshot immediately (no waiting). "
            "Use this to read messages that already arrived, e.g. after cq_send_message "
            "when cq_wait_reply returned 'no changes'."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "chat_id": {"type": "integer", "description": "Chat ID to read."},
            },
            "required": ["chat_id"],
        },
    ),
    Tool(
        name="cq_chat_stats",
        description=(
            "Get aggregated chat usage stats (calls, tokens, model breakdown, costs). "
            "Optional since_seconds limits stats to the last N seconds."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "chat_id": {
                    "type": "integer",
                    "description": "Chat ID to aggregate stats for.",
                },
                "since_seconds": {
                    "type": "integer",
                    "description": "Optional lookback window in seconds (0 = full history).",
                    "default": 0,
                },
            },
            "required": ["chat_id"],
        },
    ),
    Tool(
        name="cq_edit_file",
        description=(
            "Ask Colloquium to write (create or overwrite) a file inside the active project. "
            "Sends a <code_file> XML block as a chat message (requires chat_id). "
            "For mechanical edits without Colloquium chat messages, prefer cq_replace when a full-file rewrite is not needed."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "chat_id":  {"type": "integer", "description": "Chat ID to post to."},
                "path":     {"type": "string",  "description": "File path relative to project root."},
                "content":  {"type": "string",  "description": "Full file content to write."},
            },
            "required": ["chat_id", "path", "content"],
        },
    ),
    Tool(
        name="cq_patch_file",
        description=(
            "Ask Colloquium to apply a unified-diff patch to a project file. "
            "Sends a <patch> XML block as a chat message (requires chat_id). "
            "For mechanical edits without chat, consider cq_replace (by file_id) when a simple replace suffices."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "chat_id": {"type": "integer", "description": "Chat ID to post to."},
                "path":    {"type": "string",  "description": "File path relative to project root."},
                "diff":    {"type": "string",  "description": "Unified diff to apply."},
            },
            "required": ["chat_id", "path", "diff"],
        },
    ),
    Tool(
        name="cq_undo_file",
        description=(
            "Ask Colloquium to restore a previous version of a file. "
            "Sends an <undo> XML block as a chat message."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "chat_id":   {"type": "integer", "description": "Chat ID to post to."},
                "file_id":   {"type": "integer", "description": "Colloquium file ID to restore."},
                "time_back": {
                    "type": "integer",
                    "description": "Seconds to look back for the backup (default 3600).",
                    "default": 3600,
                },
            },
            "required": ["chat_id", "file_id"],
        },
    ),
    Tool(
        name="cq_list_projects",
        description=(
            "List all projects registered in Colloquium-DevSpace with id and metadata. "
            "Typical first step before cq_select_project, cq_exec, cq_smart_grep, or cq_list_files."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    Tool(
        name="cq_select_project",
        description=(
            "Set the active project on the Colloquium server. "
            "Must be called after a container restart before using shell_code, "
            "code_file, or code_patch. Use cq_list_projects to get project IDs."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "integer",
                    "description": "ID of the project to activate.",
                },
            },
            "required": ["project_id"],
        },
    ),
    Tool(
        name="cq_list_files",
        description=(
            "Return a lightweight file index for a project (id, file_name, ts, size_bytes). "
            "No file content is transferred. Three filters are supported and can be combined:\n"
            "  \u2022 all files \u2014 omit modified_since and file_ids\n"
            "  \u2022 recently modified \u2014 set modified_since to a Unix timestamp (files with ts \u2265 value)\n"
            "  \u2022 specific files \u2014 set file_ids as comma-separated DB IDs, e.g. '42,57,103'\n"
            "The 'id' field is the DB file_id required by cq_patch_file and cq_undo_file.\n"
            "NOTE: these IDs are NOT the same as sandwich-pack index numbers.\n"
            "Set as_tree=true to get JSON tree (kind dir/file, path, children) built from file_name paths; "
            "optional include_flat=true adds the flat list alongside the tree."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "integer",
                    "description": "Project ID (use cq_list_projects to get IDs).",
                },
                "modified_since": {
                    "type": "integer",
                    "description": "Optional Unix timestamp. Only return files with ts >= this value.",
                },
                "file_ids": {
                    "type": "string",
                    "description": "Optional comma-separated DB file IDs to fetch, e.g. '42,57,103'.",
                },
                "include_size": {
                    "type": "boolean",
                    "description": "Set to true to include size_bytes. Slower (~1s for 177 files on Docker FS). Default false.",
                },
                "as_tree": {
                    "type": "boolean",
                    "description": "If true, wrap response as {tree, file_count} with nested dirs/files from path segments.",
                    "default": False,
                },
                "include_flat": {
                    "type": "boolean",
                    "description": "When as_tree is true, also include the flat files array in the response.",
                    "default": False,
                },
            },
            "required": ["project_id"],
        },
    ),
    Tool(
        name="cq_get_index",
        description=(
            "Return the rich entity index built from the last LLM context assembly for a chat, "
            "or read the cached project index from /app/projects/.cache when project_id is provided. "
            "Includes all parsed functions, classes, methods and variables with their file_id, "
            "line ranges and token counts. Useful for code navigation and understanding project structure.\n"
            "Format: sandwiches_index.jsl — 'entities' is a list of CSV strings, layout described in 'templates.entities':\n"
            "  vis,type,parent,name,file_id,start_line-end_line,tokens\n"
            "  e.g. 'pub,function,,fetchData,3,45-67,120'\n"
            "'filelist' maps file IDs to file names (same format as cq_list_files).\n"
            "Provide either chat_id or project_id. For project_id, returns the cached project index if it exists."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "chat_id": {
                    "type": "integer",
                    "description": "Chat ID whose index to retrieve (use cq_list_chats to get IDs).",
                },
                "project_id": {
                    "type": "integer",
                    "description": "Project ID whose cached index to read from /app/projects/.cache.",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="cq_rebuild_index",
        description=(
            "Build the rich entity index for a project on demand — no prior LLM interaction needed.\n"
            "Runs context assembly (loads all project files → SandwichPack.pack) and returns\n"
            "the full sandwiches_index.jsl format JSON with 'entities' and 'filelist'.\n"
            "When background=true, MCP tool queues or reports a background build and stores the result in /app/projects/.cache/{project_name}_index.jsl.\n"
            "Use this to understand project structure, find functions/classes, or plan edits.\n"
            "'entities' is a list of CSV strings: vis,type,parent,name,file_id,start-end,tokens\n"
            "  e.g. 'pub,function,,fetchData,3,45-67,120'\n"
            "'filelist' maps file_id to file_name/md5/tokens/timestamp."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "integer",
                    "description": "Project ID (use cq_list_projects to get IDs).",
                },
                "background": {
                    "type": "boolean",
                    "description": "If true, queue/report a background build and save cache to /app/projects/.cache/{project_name}_index.jsl.",
                    "default": False,
                },
                "timeout": {
                    "type": "integer",
                    "description": "Max seconds to wait for the index build (default: 300). Passed to the backend as a hint and used as the HTTP client timeout.",
                    "default": 300,
                },
            },
            "required": ["project_id"],
        },
    ),
    Tool(
        name="cq_get_code_index",
        description=(
            "DEPRECATED alias for cq_rebuild_index. "
            "Builds the rich entity index for a project on demand and returns sandwiches_index.jsl JSON."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "integer",
                    "description": "Project ID (use cq_list_projects to get IDs).",
                },
                "background": {
                    "type": "boolean",
                    "description": "If true, queue/report a background build and save cache to /app/projects/.cache/{project_name}_index.jsl.",
                    "default": False,
                },
                "timeout": {
                    "type": "integer",
                    "description": "Max seconds to wait for the index build (default: 300). Passed to the backend as a hint and used as the HTTP client timeout.",
                    "default": 300,
                },
            },
            "required": ["project_id"],
        },
    ),
    Tool(
        name="cq_grep_entity",
        description=(
            "Search parsed definition entries in the project code index (sandwiches_index entities). "
            "The index lists declaration sites (function, class, method, variable, …) — not call sites. "
            "Supply one or more regex patterns; a row matches if any pattern matches the chosen field. "
            "Uses cached project index from cq_get_index unless ensure_index triggers cq_rebuild_index. "
            "Limitation: entity CSV rows must split cleanly on commas (names/parents with commas are not supported)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "integer",
                    "description": "Project ID (use cq_list_projects).",
                },
                "patterns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Regex patterns (OR). Alternatively pass a single string via 'pattern'.",
                },
                "pattern": {
                    "type": "string",
                    "description": "Single regex pattern (convenience if only one). Ignored if patterns is non-empty.",
                },
                "match_field": {
                    "type": "string",
                    "description": "Which field to match: name | parent | qualified (parent::name, or name if no parent).",
                    "default": "name",
                },
                "entity_types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional whitelist of entity type strings (e.g. function, class, method). Omit for all types.",
                },
                "is_regex": {
                    "type": "boolean",
                    "description": "If false, patterns are literal substrings (escaped).",
                    "default": True,
                },
                "case_sensitive": {
                    "type": "boolean",
                    "description": "Regex case sensitivity (default false).",
                    "default": False,
                },
                "max_results": {
                    "type": "integer",
                    "description": "Cap on returned matches (1..500, default 100).",
                    "default": 100,
                },
                "ensure_index": {
                    "type": "boolean",
                    "description": "If true and cached index has no entities, run cq_rebuild_index-equivalent sync build.",
                    "default": False,
                },
                "ensure_index_timeout": {
                    "type": "integer",
                    "description": "Seconds for ensure_index build (30..300, default 120).",
                    "default": 120,
                },
            },
            "required": ["project_id"],
        },
    ),
    Tool(
        name="cq_read_file",
        description=(
            "Read the contents of a project file directly by its DB file_id. "
            "Returns raw text (or formatted JSON for .json files). "
            "Use cq_list_files or cq_rebuild_index to look up file_ids. "
            "Direct HTTP call — no LLM or chat round-trip required."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "integer",
                    "description": "DB file_id from cq_list_files or cq_rebuild_index filelist.",
                },
            },
            "required": ["file_id"],
        },
    ),
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
                        {
                            "type": "string",
                            "description": "Single shell command (bash).",
                        },
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
                                ],
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
                                        ],
                                    },
                                },
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
    Tool(
        name="cq_query_db",
        description=(
            "Execute SQL query through Colloquium backend DB layer and return rows as JSON. "
            "By default only read-only SQL is allowed (SELECT/EXPLAIN/WITH). "
            "Mutating SQL can be enabled only with allow_write=true and only for local/private endpoints."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "integer",
                    "description": "Project ID (from cq_list_projects).",
                },
                "query": {
                    "type": "string",
                    "description": "SQL query string.",
                },
                "allow_write": {
                    "type": "boolean",
                    "description": "Allow mutating SQL (INSERT/UPDATE/DELETE/ALTER/etc). Works only for local/private Colloquium endpoints.",
                    "default": False,
                },
                "timeout": {
                    "type": "integer",
                    "description": "Max execution time in seconds (1-300, default 30).",
                    "default": 30,
                },
            },
            "required": ["project_id", "query"],
        },
    ),
    Tool(
        name="cq_set_sync_mode",
        description=(
            "Enable or disable synchronous mode for cq_send_message. "
            "When enabled (timeout > 0), cq_send_message automatically waits for the AI reply "
            "up to 'timeout' seconds — eliminating the need for a separate cq_wait_reply call. "
            "Set timeout=0 to disable (default). Recommended: timeout=60 for typical LLM responses."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "timeout": {
                    "type": "integer",
                    "description": "Seconds to wait for reply after send (0 = off, max 300).",
                    "default": 0,
                },
            },
            "required": ["timeout"],
        },
    ),
    Tool(
        name="cq_smart_grep",
        description=(
            "Search text or regex in predefined project file sets (code/logs/docs/all) on the CQDS project tree in one call. "
            "Prefer this over running grep/find in the IDE terminal on Windows when the task targets Colloquium-attached sources. "
            "Direct call — no LLM chat loop."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "integer", "description": "Project ID."},
                "query": {"type": "string", "description": "Text or regex pattern to find."},
                "mode": {
                    "type": "string",
                    "description": "File set preset: code | logs | docs | all (default: code).",
                    "default": "code",
                },
                "profile": {
                    "type": "string",
                    "description": "Focus profile: all | backend | frontend | docs | infra | tests | logs (default: all).",
                    "default": "all",
                },
                "time_strict": {
                    "type": "string",
                    "description": "Optional time filter, e.g. 'mtime>2026-03-25', 'mtime>=2026-03-25 21:00', 'ctime>1711390800'.",
                },
                "is_regex": {"type": "boolean", "description": "Interpret query as regex.", "default": False},
                "case_sensitive": {"type": "boolean", "description": "Case-sensitive search.", "default": False},
                "max_results": {"type": "integer", "description": "Maximum returned matches (1..500).", "default": 100},
                "context_lines": {"type": "integer", "description": "Context lines before/after match (0..3).", "default": 0},
                "include_glob": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional extra path globs to narrow search, e.g. ['src/**/*.py'].",
                },
            },
            "required": ["project_id", "query"],
        },
    ),
    Tool(
        name="cq_grep_logs",
        description=(
            "Scan one or more log files inside the selected project container context using regex filtering. "
            "Accepts file masks (glob array) and/or docker service pseudo-masks like 'docker:colloquium-core', "
            "returns JSON map: {source: [matched lines]}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "integer",
                    "description": "Project ID (from cq_list_projects).",
                },
                "query": {
                    "type": "string",
                    "description": "Regex pattern for line filtering.",
                },
                "log_masks": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Sources to scan: file globs (e.g. ['logs/*.log']) and/or docker targets (e.g. ['docker:colloquium-core']).",
                },
                "tail_lines": {
                    "type": "integer",
                    "description": "Max matched lines per file to return from tail (default 100).",
                    "default": 100,
                },
                "since_seconds": {
                    "type": "integer",
                    "description": "Optional time window in seconds; when > 0, only lines from the last N seconds are considered.",
                    "default": 0,
                },
                "case_sensitive": {
                    "type": "boolean",
                    "description": "Case-sensitive regex matching (default false).",
                    "default": False,
                },
            },
            "required": ["project_id", "query"],
        },
    ),
    Tool(
        name="cq_replace",
        description=(
            "Replace text in one file directly by file_id, with optional regex mode. "
            "No chat/LLM round-trip (contrast: cq_edit_file/cq_patch_file post via chat messages). "
            "Use cq_list_files or index filelist to resolve file_id."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "integer", "description": "Project ID."},
                "file_id": {"type": "integer", "description": "DB file_id in the selected project."},
                "old": {"type": "string", "description": "Old text or regex pattern."},
                "new": {"type": "string", "description": "Replacement text."},
                "is_regex": {"type": "boolean", "description": "Interpret old as regex.", "default": False},
                "case_sensitive": {"type": "boolean", "description": "Case-sensitive matching.", "default": True},
                "max_replacements": {
                    "type": "integer",
                    "description": "Limit number of replacements (0 = all).",
                    "default": 0,
                },
            },
            "required": ["project_id", "file_id", "old", "new"],
        },
    ),
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
                "project_id": {
                    "type": "integer",
                    "description": "Project ID (for logging and process isolation).",
                },
                "command": {
                    "type": "string",
                    "description": "Shell command or python code (depending on engine).",
                },
                "engine": {
                    "type": "string",
                    "enum": ["bash", "python"],
                    "description": "Execution engine (bash or python).",
                    "default": "bash",
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory (default: current).",
                },
                "env": {
                    "type": "object",
                    "description": "Environment variables as dict (default: inherit parent).",
                },
                "timeout": {
                    "type": "integer",
                    "description": "TTL timeout in seconds (1-7200, default 3600).",
                    "default": 3600,
                },
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
                "process_guid": {
                    "type": "string",
                    "description": "Process GUID (returned by cq_process_spawn).",
                },
                "input": {
                    "type": "string",
                    "description": "Optional data to write to process stdin (base64 encoded or plain text).",
                },
                "read_timeout_ms": {
                    "type": "integer",
                    "description": "Read timeout in milliseconds (default 5000).",
                    "default": 5000,
                },
                "max_bytes": {
                    "type": "integer",
                    "description": "Max bytes to return from each buffer (default 65536).",
                    "default": 65536,
                },
            },
            "required": ["process_guid"],
        },
    ),
    Tool(
        name="cq_process_kill",
        description=(
            "Terminate a running process by sending a signal (SIGTERM or SIGKILL)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "process_guid": {
                    "type": "string",
                    "description": "Process GUID to terminate.",
                },
                "signal": {
                    "type": "string",
                    "enum": ["SIGTERM", "SIGKILL"],
                    "description": "Signal to send (default SIGTERM).",
                    "default": "SIGTERM",
                },
            },
            "required": ["process_guid"],
        },
    ),
    Tool(
        name="cq_process_status",
        description=(
            "Get current status of a process (alive, exit_code, timestamps, runtime_ms, cpu_time_ms)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "process_guid": {
                    "type": "string",
                    "description": "Process GUID to query.",
                },
            },
            "required": ["process_guid"],
        },
    ),
    Tool(
        name="cq_process_list",
        description=(
            "List all processes, optionally filtered by project_id."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "integer",
                    "description": "Optional project ID to filter processes.",
                },
            },
        },
    ),
    Tool(
        name="cq_project_status",
        description=(
            "Get health status and diagnostics for a project.\n"
            "Returns: status (ok/info/warning/error), problems[] with severity codes,\n"
            "file link counts (total/active), backup/undo stack info (count, size_bytes, oldest_ts, newest_ts),\n"
            "scan state and index cache state.\n"
            "Use this to quickly check if a project has stale file links, a failing scan,\n"
            "or a missing index cache. 'problems' drives the frontend warning indicator."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "integer",
                    "description": "Project ID (use cq_list_projects to get IDs).",
                },
            },
            "required": ["project_id"],
        },
    ),
    Tool(
        name="cq_process_wait",
        description=(
            "Wait for a process condition (output or exit) with timeout. Non-blocking poll."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "process_guid": {
                    "type": "string",
                    "description": "Process GUID to wait for.",
                },
                "wait_timeout_ms": {
                    "type": "integer",
                    "description": "Wait timeout in milliseconds (default 30000).",
                    "default": 30000,
                },
                "wait_condition": {
                    "type": "string",
                    "enum": ["any_output", "finished"],
                    "description": "Condition: any_output (stdout/stderr available) or finished (process exited).",
                    "default": "any_output",
                },
            },
            "required": ["process_guid"],
        },
    ),
    Tool(
        name="cq_docker_control",
        description=(
            "Control CQDS Docker Compose services on the host. Wraps scripts/cqds_ctl.py.\n"
            "Commands:\n"
            "  status      — report container state, health, and recent log failures\n"
            "  restart     — docker compose restart + wait for stable/failed\n"
            "  rebuild     — docker compose up -d --build + wait for stable/failed\n"
            "  clear-logs  — truncate container json-file logs via Docker VM\n"
            "Optional 'services' list narrows the scope to specific compose services\n"
            "(e.g. ['colloquium-core', 'frontend']). Omit to target all services.\n"
            "'wait' (bool, status only): block until stable or failed rather than snapshot.\n"
            "'timeout': seconds to wait for stable state (default 90, restart/rebuild only)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "enum": ["status", "restart", "rebuild", "clear-logs"],
                    "description": "Control action to perform.",
                },
                "services": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of compose service names, e.g. ['colloquium-core'].",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Seconds to wait for stable/failed state (default 90). Only for status/restart/rebuild.",
                    "default": 90,
                },
                "wait": {
                    "type": "boolean",
                    "description": "For 'status': block until stable or failed before returning (default false).",
                    "default": False,
                },
            },
            "required": ["command"],
        },
    ),
    Tool(
        name="cq_docker_control_batch",
        description=(
            "Batch CQDS Docker Compose control via cqds_ctl.py: pass a JSON array of requests, "
            "get an array of results in order. Each request has the same fields as cq_docker_control "
            "(command, optional services, timeout, wait). Steps run sequentially on the MCP host. "
            "Use stop_on_error=true to abort after the first failure (default false: run all steps)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "requests": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "command": {
                                "type": "string",
                                "enum": ["status", "restart", "rebuild", "clear-logs"],
                            },
                            "services": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "timeout": {
                                "type": "integer",
                                "description": "Per-step timeout seconds (10–600, default 90).",
                                "default": 90,
                            },
                            "wait": {
                                "type": "boolean",
                                "description": "For status: block until stable/failed.",
                                "default": False,
                            },
                        },
                        "required": ["command"],
                    },
                    "description": "Ordered list of cq_docker_control-equivalent operations.",
                },
                "stop_on_error": {
                    "type": "boolean",
                    "description": "If true, stop after the first failed step (remaining not run).",
                    "default": False,
                },
            },
            "required": ["requests"],
        },
    ),
    Tool(
        name="cq_docker_exec",
        description=(
            "Run `docker exec` on the MCP host (not cqds_ctl). Pass an ordered list of exec requests; "
            "each step runs sequentially. Fields per request: container (required), command (string "
            "→ `sh -c` inside the container, or argv array), optional workdir, user, env object, "
            "stdin (string, UTF-8), interactive (bool; implied true when stdin is set), "
            "timeout_sec (1–600, default 120). Uses the docker CLI from PATH; cwd for the CLI is the "
            "cqds repo root."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "requests": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "container": {"type": "string"},
                            "command": {
                                "oneOf": [
                                    {"type": "string"},
                                    {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "minItems": 1,
                                    },
                                ],
                            },
                            "workdir": {"type": "string"},
                            "user": {"type": "string"},
                            "env": {
                                "type": "object",
                                "description": "Extra -e KEY=value for docker exec.",
                            },
                            "stdin": {
                                "type": "string",
                                "description": "Optional stdin for this exec (UTF-8).",
                            },
                            "interactive": {
                                "type": "boolean",
                                "description": "Pass docker -i (also set automatically if stdin is provided).",
                                "default": False,
                            },
                            "timeout_sec": {
                                "type": "integer",
                                "description": "Per-step timeout seconds (1–600, default 120).",
                                "default": 120,
                            },
                        },
                        "required": ["container", "command"],
                    },
                    "description": "Ordered docker exec operations.",
                },
                "stop_on_error": {
                    "type": "boolean",
                    "description": "If true, stop after the first failed step.",
                    "default": False,
                },
            },
            "required": ["requests"],
        },
    ),
    Tool(
        name="cq_host_process_spawn",
        description=(
            "Spawn a subprocess on the machine where this MCP server runs (local host), not in "
            "Colloquium/mcp-sandbox. Same interaction model as cq_process_spawn: use cq_host_process_io "
            "/ wait / status / kill afterward. command: shell string (asyncio.create_subprocess_shell) "
            "or argv array (create_subprocess_exec). Optional cwd, env, timeout seconds (1–7200, default 3600) "
            "after which the process is killed if still running."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "command": {
                    "oneOf": [
                        {"type": "string"},
                        {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                        },
                    ],
                    "description": "Shell string or argv list.",
                },
                "cwd": {"type": "string"},
                "env": {"type": "object"},
                "timeout": {
                    "type": "integer",
                    "description": "TTL in seconds; process killed if still alive (default 3600).",
                    "default": 3600,
                },
            },
            "required": ["command"],
        },
    ),
    Tool(
        name="cq_host_process_io",
        description=(
            "Read accumulated stdout/stderr from a cq_host_process_spawn process and optionally write "
            "to stdin. Returns text fragments (UTF-8, replacement on errors), not base64."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "process_guid": {"type": "string"},
                "input": {"type": "string", "description": "Optional plain text written to stdin."},
                "read_timeout_ms": {"type": "integer", "default": 5000},
                "max_bytes": {"type": "integer", "default": 65536},
            },
            "required": ["process_guid"],
        },
    ),
    Tool(
        name="cq_host_process_kill",
        description=(
            "Send SIGTERM or SIGKILL to a host process spawned via cq_host_process_spawn and remove "
            "it from the local registry."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "process_guid": {"type": "string"},
                "signal": {
                    "type": "string",
                    "enum": ["SIGTERM", "SIGKILL"],
                    "default": "SIGTERM",
                },
            },
            "required": ["process_guid"],
        },
    ),
    Tool(
        name="cq_host_process_status",
        description="Status for a local host process (alive, returncode, pid, runtime_ms).",
        inputSchema={
            "type": "object",
            "properties": {"process_guid": {"type": "string"}},
            "required": ["process_guid"],
        },
    ),
    Tool(
        name="cq_host_process_list",
        description="List subprocesses spawned via cq_host_process_spawn on this MCP host.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="cq_host_process_wait",
        description=(
            "Poll a host process for new output or exit (same semantics as cq_process_wait: "
            "any_output vs finished)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "process_guid": {"type": "string"},
                "wait_timeout_ms": {"type": "integer", "default": 30000},
                "wait_condition": {
                    "type": "string",
                    "enum": ["any_output", "finished"],
                    "default": "any_output",
                },
            },
            "required": ["process_guid"],
        },
    ),
]


def _text(content: str) -> CallToolResult:
    LOGGER.info(
        "TOOL result name=%s content=%s",
        CURRENT_TOOL.get(),
        _preview_text(content, 220),
    )
    return CallToolResult(content=[TextContent(type="text", text=content)])


def _json_text(obj: Any) -> CallToolResult:
    return _text(json.dumps(obj, ensure_ascii=False, indent=2))


def _index_counts(index_payload: dict[str, Any]) -> tuple[int | None, int | None]:
    entities = index_payload.get("entities") if isinstance(index_payload, dict) else None
    filelist = None
    if isinstance(index_payload, dict):
        filelist = index_payload.get("files")
        if filelist is None:
            filelist = index_payload.get("filelist")
    entities_count = len(entities) if isinstance(entities, list) else None
    files_count = len(filelist) if isinstance(filelist, (list, dict)) else None
    return entities_count, files_count


def _index_file_rows(index_payload: dict[str, Any]) -> list[str]:
    files = index_payload.get("files") if isinstance(index_payload, dict) else None
    if files is None and isinstance(index_payload, dict):
        files = index_payload.get("filelist")
    if isinstance(files, list):
        return [r for r in files if isinstance(r, str)]
    return []


def _file_id_to_name_map(rows: list[str]) -> dict[int, str]:
    out: dict[int, str] = {}
    for row in rows:
        parts = row.split(",")
        if not parts:
            continue
        try:
            fid = int(parts[0].strip())
        except ValueError:
            continue
        if len(parts) > 1:
            out[fid] = parts[1]
    return out


def _parse_entity_csv_row(line: str) -> dict[str, Any] | None:
    """Parse one sandwiches_index entities CSV row: vis,type,parent,name,file_id,start-end,tokens."""
    if not isinstance(line, str) or not line.strip():
        return None
    parts = line.split(",")
    if len(parts) < 7:
        return None
    vis, e_type, parent, name = parts[0], parts[1], parts[2], parts[3]
    file_id_s, span_s, tokens_s = parts[4], parts[5], parts[6]
    try:
        file_id = int(file_id_s.strip())
    except ValueError:
        return None
    m = re.match(r"^(\d+)-(\d+)$", span_s.strip())
    if not m:
        return None
    start_line, end_line = int(m.group(1)), int(m.group(2))
    try:
        tokens = int(tokens_s.strip())
    except ValueError:
        tokens = 0
    return {
        "vis": vis,
        "type": e_type,
        "parent": parent,
        "name": name,
        "file_id": file_id,
        "start_line": start_line,
        "end_line": end_line,
        "tokens": tokens,
    }


def _build_file_tree_from_index(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Nest flat file_index rows by file_name path segments (posix-style)."""
    root: dict[str, Any] = {"kind": "dir", "name": "", "path": "", "children": []}

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        raw = entry.get("file_name")
        if raw is None:
            continue
        fn = str(raw).replace("\\", "/").strip("/")
        if not fn:
            continue
        parts = [p for p in fn.split("/") if p]
        node = root
        acc: list[str] = []
        for i, seg in enumerate(parts):
            acc.append(seg)
            is_leaf = i == len(parts) - 1
            children: list[dict[str, Any]] = node.setdefault("children", [])
            if is_leaf:
                leaf: dict[str, Any] = {
                    "kind": "file",
                    "name": seg,
                    "file_name": fn,
                    "id": entry.get("id"),
                    "ts": entry.get("ts"),
                }
                if "project_id" in entry:
                    leaf["project_id"] = entry["project_id"]
                if "size_bytes" in entry:
                    leaf["size_bytes"] = entry["size_bytes"]
                children.append(leaf)
            else:
                dir_node: dict[str, Any] | None = None
                for c in children:
                    if c.get("kind") == "dir" and c.get("name") == seg:
                        dir_node = c
                        break
                if dir_node is None:
                    dir_path = "/".join(acc)
                    dir_node = {
                        "kind": "dir",
                        "name": seg,
                        "path": dir_path,
                        "children": [],
                    }
                    children.append(dir_node)
                node = dir_node

    def sort_children(n: dict[str, Any]) -> None:
        ch = n.get("children")
        if not isinstance(ch, list):
            return
        for c in ch:
            if isinstance(c, dict) and c.get("kind") == "dir":
                sort_children(c)
        ch.sort(
            key=lambda x: (
                0 if (isinstance(x, dict) and x.get("kind") == "dir") else 1,
                str(x.get("name", "")).lower() if isinstance(x, dict) else "",
            )
        )

    sort_children(root)
    return root


def _is_progress_stub(message: str) -> bool:
    msg = (message or "").strip().lower()
    if not msg:
        return False
    markers = (
        "llm request accepted",
        "preparing response",
        "response in progress",
        "⏳",
    )
    return any(marker in msg for marker in markers)


def _extract_latest_message(payload: Any) -> str | None:
    latest_rank: int | None = None
    latest_message: str | None = None

    def walk(node: Any) -> None:
        nonlocal latest_rank, latest_message
        if isinstance(node, dict):
            msg = node.get("message")
            if isinstance(msg, str):
                rank_raw = node.get("id", node.get("post_id", node.get("timestamp", 0)))
                try:
                    rank = int(rank_raw)
                except Exception:
                    rank = 0
                if latest_rank is None or rank >= latest_rank:
                    latest_rank = rank
                    latest_message = msg
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return latest_message


_DOCKER_CTL_ALLOWED = frozenset({"status", "restart", "rebuild", "clear-logs"})


async def _invoke_cqds_ctl(
    command: str,
    services: list[str],
    timeout: int,
    wait: bool,
) -> dict[str, Any]:
    """Run scripts/cqds_ctl.py once. Returns ok+data or ok False with error/stdout/stderr."""
    if command not in _DOCKER_CTL_ALLOWED:
        return {
            "ok": False,
            "error": (
                f"Unknown command '{command}'. Allowed: {', '.join(sorted(_DOCKER_CTL_ALLOWED))}"
            ),
            "stdout": "",
            "stderr": "",
        }

    ctl_script = Path(__file__).resolve().parent / "scripts" / "cqds_ctl.py"
    if not ctl_script.is_file():
        return {
            "ok": False,
            "error": f"cqds_ctl.py not found at {ctl_script}",
            "stdout": "",
            "stderr": "",
        }

    proc_env = dict(os.environ)
    if not proc_env.get("MCP_AUTH_TOKEN"):
        proc_env["MCP_AUTH_TOKEN"] = _MCP_AUTH_TOKEN
    if not proc_env.get("DB_ROOT_PASSWD"):
        db_passwd_file = ctl_script.parent.parent / "secrets" / "cqds_db_password"
        if db_passwd_file.is_file():
            proc_env["DB_ROOT_PASSWD"] = db_passwd_file.read_text(encoding="utf-8").strip()

    cmd_args = [sys.executable, str(ctl_script), command]
    if command in {"status", "restart", "rebuild"}:
        cmd_args.append(f"--timeout={timeout}")
    if command == "status" and wait:
        cmd_args.append("--wait")
    cmd_args.extend(services)

    LOGGER.info("cqds_ctl: %s", " ".join(cmd_args))
    proc = await asyncio.create_subprocess_exec(
        *cmd_args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
        env=proc_env,
    )
    proc_timeout = timeout + 60
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=proc_timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        return {
            "ok": False,
            "error": f"cqds_ctl timed out after {proc_timeout}s",
            "stdout": "",
            "stderr": "",
        }

    stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
    stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()

    try:
        payload = json.loads(stdout_text)
    except Exception:
        return {
            "ok": False,
            "error": "non-JSON output from cqds_ctl.py",
            "stdout": stdout_text[:2000],
            "stderr": stderr_text[:1000],
        }
    return {"ok": True, "data": payload}


_MAX_HOST_PROC_BUFF = 4 * 1024 * 1024
_MAX_HOST_PROCS = 48


async def _host_pump_stream(
    reader: asyncio.StreamReader | None,
    acc: bytearray,
    cap: int,
) -> None:
    if reader is None:
        return
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            acc.extend(chunk)
            if len(acc) > cap:
                del acc[: len(acc) - cap]
    except Exception as exc:
        LOGGER.debug("host process pump ended: %s", exc)


class HostProcRecord:
    __slots__ = (
        "proc",
        "argv_desc",
        "started",
        "stdout_acc",
        "stderr_acc",
        "pump_out",
        "pump_err",
        "ttl_task",
    )

    def __init__(self, proc: asyncio.subprocess.Process, argv_desc: str) -> None:
        self.proc = proc
        self.argv_desc = argv_desc
        self.started = time.monotonic()
        self.stdout_acc = bytearray()
        self.stderr_acc = bytearray()
        self.pump_out: asyncio.Task[None] | None = None
        self.pump_err: asyncio.Task[None] | None = None
        self.ttl_task: asyncio.Task[None] | None = None


def _host_tail_text(acc: bytearray, max_bytes: int) -> str:
    if max_bytes <= 0:
        return ""
    raw = bytes(acc[-max_bytes:]) if len(acc) > max_bytes else bytes(acc)
    return raw.decode("utf-8", errors="replace")


def _docker_cli_exe() -> str:
    return shutil.which("docker") or "docker"


def _cqds_repo_root() -> Path:
    return Path(__file__).resolve().parent


def _docker_exec_argv(
    container: str,
    command: str | list[Any],
    *,
    workdir: str | None,
    user: str | None,
    env: dict[str, Any] | None,
    interactive: bool,
) -> list[str]:
    exe = _docker_cli_exe()
    argv: list[str] = [exe, "exec"]
    if interactive:
        argv.append("-i")
    if workdir:
        argv.extend(["-w", str(workdir)])
    if user:
        argv.extend(["-u", str(user)])
    if env:
        for k, v in env.items():
            argv.extend(["-e", f"{str(k)}={str(v)}"])
    argv.append(container)
    if isinstance(command, str):
        argv.extend(["sh", "-c", command])
    elif isinstance(command, list):
        if not command:
            raise ValueError("command list must be non-empty")
        for part in command:
            argv.append(str(part))
    else:
        raise TypeError("command must be str or list")
    return argv


async def _docker_exec_batch_item(raw: dict[str, Any]) -> dict[str, Any]:
    try:
        container = str(raw.get("container", "")).strip()
        if not container:
            return {"ok": False, "error": "missing container", "request": raw}
        command = raw.get("command")
        if command is None:
            return {"ok": False, "error": "missing command", "request": raw}
        workdir = raw.get("workdir")
        workdir_s = str(workdir) if workdir is not None else None
        user = raw.get("user")
        user_s = str(user) if user is not None else None
        env = raw.get("env")
        env_d: dict[str, Any] | None = env if isinstance(env, dict) else None
        stdin_raw = raw.get("stdin")
        stdin_b = str(stdin_raw).encode("utf-8") if stdin_raw is not None else None
        interactive_flag = bool(raw.get("interactive", False)) or (stdin_b is not None)
        argv = _docker_exec_argv(
            container,
            command,
            workdir=workdir_s,
            user=user_s,
            env=env_d,
            interactive=interactive_flag,
        )
        timeout_sec = max(1, min(int(raw.get("timeout_sec", 120)), 600))
        LOGGER.info("cq_docker_exec: %s", argv)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(_cqds_repo_root()),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE if stdin_b is not None else asyncio.subprocess.DEVNULL,
            env=os.environ.copy(),
        )
        try:
            out_b, err_b = await asyncio.wait_for(
                proc.communicate(stdin_b),
                timeout=float(timeout_sec) + 15.0,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {
                "ok": False,
                "error": f"docker exec timed out after {timeout_sec}s",
                "request": raw,
            }
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": out_b.decode("utf-8", errors="replace") if out_b else "",
            "stderr": err_b.decode("utf-8", errors="replace") if err_b else "",
            "request": raw,
        }
    except Exception as exc:
        LOGGER.exception("docker exec batch item")
        return {"ok": False, "error": str(exc), "request": raw}


async def run_server(client: ColloquiumClient) -> None:
    server = Server("cqds-mcp")
    index_queue: asyncio.Queue[int] = asyncio.Queue()
    index_jobs: dict[int, dict[str, Any]] = {}
    index_worker_task: asyncio.Task | None = None
    host_proc_registry: dict[str, HostProcRecord] = {}

    async def index_worker() -> None:
        while True:
            project_id = await index_queue.get()
            job = index_jobs.setdefault(project_id, {"project_id": project_id})
            job.update(
                {
                    "status": "running",
                    "running": True,
                    "queued": False,
                    "started_at": int(time.time()),
                    "finished_at": None,
                    "error": None,
                }
            )
            try:
                payload = await client.get_code_index(project_id)
                entities_count, files_count = _index_counts(payload)
                job.update(
                    {
                        "status": "ready",
                        "running": False,
                        "queued": False,
                        "finished_at": int(time.time()),
                        "error": None,
                        "entities": entities_count,
                        "files": files_count,
                    }
                )
            except Exception as exc:
                job.update(
                    {
                        "status": "error",
                        "running": False,
                        "queued": False,
                        "finished_at": int(time.time()),
                        "error": str(exc),
                    }
                )
            finally:
                index_queue.task_done()

    async def ensure_index_worker() -> None:
        nonlocal index_worker_task
        if index_worker_task is None or index_worker_task.done():
            index_worker_task = asyncio.create_task(index_worker(), name="cq-index-worker")

    def queue_status(project_id: int) -> dict[str, Any]:
        job = index_jobs.get(project_id)
        if not job:
            return {
                "project_id": project_id,
                "status": "idle",
                "running": False,
                "queued": False,
                "queue_size": index_queue.qsize(),
                "started_at": None,
                "finished_at": None,
                "error": None,
                "files": None,
                "entities": None,
            }
        merged = dict(job)
        merged["queue_size"] = index_queue.qsize()
        return merged

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return TOOLS

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
        tool_token = CURRENT_TOOL.set(name)
        started_at = time.monotonic()
        LOGGER.info("TOOL call start name=%s args=[%s]", name, _summarize_arguments(arguments))
        try:
            # ---- list chats ----
            if name == "cq_list_chats":
                chats = await client.list_chats()
                return _json_text(chats)

            # ---- create chat ----
            elif name == "cq_create_chat":
                description = arguments.get("description", "MCP Session")
                chat_id = await client.create_chat(description)
                return _text(f"Created chat with chat_id={chat_id}")

            # ---- send message ----
            elif name == "cq_send_message":
                chat_id = int(arguments["chat_id"])
                message = str(arguments["message"])
                await client.post_message(chat_id, message)
                if client._sync_timeout > 0:
                    deadline = time.monotonic() + client._sync_timeout
                    saw_progress_stub = False
                    while time.monotonic() < deadline:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        resp = await client.get_reply(chat_id, wait=True, timeout=min(remaining, 15.0))
                        hist = resp.get("chat_history", "") if isinstance(resp, dict) else ""
                        if hist not in ("no changes", "chat switch"):
                            latest_message = _extract_latest_message(resp)
                            if latest_message and _is_progress_stub(latest_message):
                                saw_progress_stub = True
                                continue
                            return _json_text(resp)
                    if saw_progress_stub:
                        return _text(
                            f"Message sent to chat_id={chat_id} "
                            f"(sync: only progress stub seen within {client._sync_timeout}s)"
                        )
                    return _text(f"Message sent to chat_id={chat_id} (sync: no reply in {client._sync_timeout}s)")
                return _text(f"Message sent to chat_id={chat_id}")

            # ---- wait reply ----
            elif name == "cq_wait_reply":
                chat_id = int(arguments["chat_id"])
                resp = await client.get_reply(chat_id)
                return _json_text(resp)

            # ---- get history ----
            elif name == "cq_get_history":
                chat_id = int(arguments["chat_id"])
                resp = await client.get_history(chat_id)
                return _json_text(resp)

            # ---- chat stats ----
            elif name == "cq_chat_stats":
                chat_id = int(arguments["chat_id"])
                since_seconds_raw = arguments.get("since_seconds", 0)
                since_seconds = int(since_seconds_raw) if since_seconds_raw is not None else 0
                since_seconds = max(0, min(since_seconds, 30 * 24 * 3600))
                resp = await client.get_chat_stats(
                    chat_id=chat_id,
                    since_seconds=since_seconds if since_seconds > 0 else None,
                )
                return _json_text(resp)

            # ---- edit file ----
            elif name == "cq_edit_file":
                chat_id = int(arguments["chat_id"])
                path = str(arguments["path"])
                content = str(arguments["content"])
                xml = _xml_code_file(path, content)
                await client.post_message(chat_id, xml)
                return _text(f"<code_file> sent for '{path}' to chat_id={chat_id}")

            # ---- patch file ----
            elif name == "cq_patch_file":
                chat_id = int(arguments["chat_id"])
                path = str(arguments["path"])
                diff = str(arguments["diff"])
                xml = _xml_patch(path, diff)
                await client.post_message(chat_id, xml)
                return _text(f"<patch> sent for '{path}' to chat_id={chat_id}")

            # ---- undo file ----
            elif name == "cq_undo_file":
                chat_id = int(arguments["chat_id"])
                file_id = int(arguments["file_id"])
                time_back = int(arguments.get("time_back", 3600))
                xml = _xml_undo(file_id, time_back)
                await client.post_message(chat_id, xml)
                return _text(f"<undo> sent for file_id={file_id} to chat_id={chat_id}")

            # ---- list projects ----
            elif name == "cq_list_projects":
                projects = await client.list_projects()
                return _json_text(projects)

            # ---- select project ----
            elif name == "cq_select_project":
                project_id = int(arguments["project_id"])
                result = await client.select_project(project_id)
                return _text(f"Project {project_id} selected: {result}")

            # ---- list files ----
            elif name == "cq_list_files":
                project_id = int(arguments["project_id"])
                modified_since = arguments.get("modified_since")
                file_ids_raw = arguments.get("file_ids")
                include_size = bool(arguments.get("include_size", False))
                as_tree = bool(arguments.get("as_tree", False))
                include_flat = bool(arguments.get("include_flat", False))
                modified_since = int(modified_since) if modified_since is not None else None
                file_ids = [int(x.strip()) for x in file_ids_raw.split(",")] if file_ids_raw else None
                files = await client.list_files(project_id, modified_since, file_ids, include_size)
                if not as_tree:
                    return _json_text(files)
                tree = _build_file_tree_from_index(files)
                payload: dict[str, Any] = {
                    "tree": tree,
                    "file_count": len(files),
                }
                if include_flat:
                    payload["files"] = files
                return _json_text(payload)

            # ---- get index (chat-based cache) ----
            elif name == "cq_get_index":
                chat_id_raw = arguments.get("chat_id")
                project_id_raw = arguments.get("project_id")
                if chat_id_raw is None and project_id_raw is None:
                    raise ValueError("cq_get_index requires chat_id or project_id")
                chat_id = int(chat_id_raw) if chat_id_raw is not None else None
                project_id = int(project_id_raw) if project_id_raw is not None else None
                index = await client.get_index(chat_id=chat_id, project_id=project_id)
                return _json_text(index)

            # ---- get code index (on-demand, project-level) ----
            elif name in {"cq_rebuild_index", "cq_get_code_index"}:
                project_id = int(arguments["project_id"])
                background = bool(arguments.get("background", False))
                timeout = int(arguments.get("timeout", 300))
                if background:
                    await ensure_index_worker()
                    current = index_jobs.get(project_id)
                    if current and current.get("status") in {"queued", "running"}:
                        return _json_text(queue_status(project_id))

                    try:
                        cached = await client.get_index(project_id=project_id)
                        entities_count, files_count = _index_counts(cached)
                        index_jobs[project_id] = {
                            "project_id": project_id,
                            "status": "ready",
                            "running": False,
                            "queued": False,
                            "started_at": None,
                            "finished_at": int(time.time()),
                            "error": None,
                            "files": files_count,
                            "entities": entities_count,
                            "source": "cache",
                        }
                        return _json_text(queue_status(project_id))
                    except httpx.HTTPStatusError as exc:
                        if exc.response.status_code != 404:
                            raise

                    index_jobs[project_id] = {
                        "project_id": project_id,
                        "status": "queued",
                        "running": False,
                        "queued": True,
                        "started_at": None,
                        "finished_at": None,
                        "error": None,
                        "files": None,
                        "entities": None,
                        "source": "mcp-queue",
                    }
                    await index_queue.put(project_id)
                    return _json_text(queue_status(project_id))

                index = await client.get_code_index(project_id, timeout=timeout)
                entities_count, files_count = _index_counts(index)
                index_jobs[project_id] = {
                    "project_id": project_id,
                    "status": "ready",
                    "running": False,
                    "queued": False,
                    "started_at": None,
                    "finished_at": int(time.time()),
                    "error": None,
                    "files": files_count,
                    "entities": entities_count,
                    "source": "sync",
                }
                return _json_text(index)

            # ---- grep entities in code index (definitions only) ----
            elif name == "cq_grep_entity":
                project_id = int(arguments["project_id"])
                patterns_raw = arguments.get("patterns")
                if patterns_raw is None or patterns_raw == []:
                    single = arguments.get("pattern")
                    patterns_raw = [single] if single else []
                if isinstance(patterns_raw, str):
                    patterns_list = [patterns_raw] if patterns_raw.strip() else []
                elif isinstance(patterns_raw, list):
                    patterns_list = [str(p) for p in patterns_raw if str(p).strip()]
                else:
                    raise ValueError("cq_grep_entity: patterns must be an array of strings or use pattern (string)")
                if not patterns_list:
                    raise ValueError("cq_grep_entity: provide patterns (non-empty array) or pattern (string)")

                match_field = str(arguments.get("match_field", "name")).lower()
                if match_field not in {"name", "parent", "qualified"}:
                    raise ValueError("match_field must be one of: name, parent, qualified")

                type_filter = arguments.get("entity_types")
                type_allow: set[str] | None = None
                if type_filter is not None:
                    if not isinstance(type_filter, list):
                        raise ValueError("entity_types must be an array of strings or omitted")
                    type_allow = {str(t) for t in type_filter if str(t).strip()}
                    if not type_allow:
                        type_allow = None

                is_regex = bool(arguments.get("is_regex", True))
                case_sensitive = bool(arguments.get("case_sensitive", False))
                max_results = int(arguments.get("max_results", 100))
                max_results = max(1, min(max_results, 500))

                ensure_index = bool(arguments.get("ensure_index", False))
                ensure_timeout = int(arguments.get("ensure_index_timeout", 120))
                ensure_timeout = max(30, min(ensure_timeout, 300))

                index_payload = await client.get_index(project_id=project_id)
                entities = index_payload.get("entities") if isinstance(index_payload, dict) else None

                if (not isinstance(entities, list) or len(entities) == 0) and ensure_index:
                    index_payload = await client.get_code_index(project_id, timeout=ensure_timeout)
                    entities = index_payload.get("entities") if isinstance(index_payload, dict) else None

                if not isinstance(entities, list) or len(entities) == 0:
                    return _json_text(
                        {
                            "matches": [],
                            "count": 0,
                            "truncated": False,
                            "hint": (
                                "No entities in index. Run cq_rebuild_index, or call again with ensure_index=true."
                            ),
                            "project_id": project_id,
                        }
                    )

                flags = 0 if case_sensitive else re.IGNORECASE
                compiled: list[re.Pattern[str]] = []
                for pat in patterns_list:
                    try:
                        if is_regex:
                            compiled.append(re.compile(pat, flags))
                        else:
                            compiled.append(re.compile(re.escape(pat), flags))
                    except re.error as exc:
                        raise ValueError(f"Invalid pattern {pat!r}: {exc}") from exc

                def qualified_name(row: dict[str, Any]) -> str:
                    par, nm = row.get("parent") or "", row.get("name") or ""
                    if par:
                        return f"{par}::{nm}"
                    return nm

                def text_for_match(row: dict[str, Any]) -> str:
                    if match_field == "parent":
                        return row.get("parent") or ""
                    if match_field == "qualified":
                        return qualified_name(row)
                    return row.get("name") or ""

                file_rows = _index_file_rows(index_payload)
                fid_to_name = _file_id_to_name_map(file_rows)

                matches: list[dict[str, Any]] = []
                truncated = False
                for line in entities:
                    if not isinstance(line, str):
                        continue
                    row = _parse_entity_csv_row(line)
                    if row is None:
                        continue
                    if type_allow is not None and row["type"] not in type_allow:
                        continue
                    haystack = text_for_match(row)
                    if not any(c.search(haystack) for c in compiled):
                        continue
                    row_out = {
                        **row,
                        "file_name": fid_to_name.get(row["file_id"]),
                    }
                    matches.append(row_out)
                    if len(matches) >= max_results:
                        truncated = True
                        break

                matches.sort(key=lambda r: (r.get("file_id", 0), r.get("start_line", 0), r.get("name", "")))

                return _json_text(
                    {
                        "matches": matches,
                        "count": len(matches),
                        "truncated": truncated,
                        "max_results": max_results,
                        "project_id": project_id,
                        "note": (
                            "Matches are definition rows from the sandwiches index only (not call-site grep)."
                        ),
                    }
                )

            # ---- read file ----
            elif name == "cq_read_file":
                file_id = int(arguments["file_id"])
                content = await client.read_file(file_id)
                return _text(content)

            # ---- exec command ----
            elif name == "cq_exec":
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

            # ---- spawn temp script ----
            elif name == "cq_spawn_script":
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

            # ---- query DB (read-only) ----
            elif name == "cq_query_db":
                project_id = int(arguments["project_id"])
                query = str(arguments["query"] or "").strip()
                allow_write = bool(arguments.get("allow_write", False))
                timeout = int(arguments.get("timeout", 30))
                if not query:
                    raise ValueError("query must be non-empty")

                ql = query.lower().lstrip()
                if not allow_write:
                    if not (ql.startswith("select") or ql.startswith("with") or ql.startswith("explain")):
                        raise ValueError("Only read-only SQL is allowed (SELECT/WITH/EXPLAIN). Set allow_write=true for local/private endpoints.")
                    if re.search(r"\b(insert|update|delete|drop|alter|truncate|create|grant|revoke|comment)\b", ql):
                        raise ValueError("Mutating SQL keywords are not allowed in cq_query_db without allow_write=true")
                else:
                    if not client.is_local_or_private_endpoint():
                        raise ValueError("allow_write=true is permitted only for local/private Colloquium endpoints")

                encoded = base64.b64encode(query.encode("utf-8")).decode("ascii")
                command = (
                    "PYTHONPATH=/app/agent /app/venv/bin/python - <<'PY'\n"
                    "import base64, json\n"
                    "from managers.db import Database\n"
                    f"q = base64.b64decode('{encoded}').decode('utf-8')\n"
                    "db = Database.get_database()\n"
                    "rows = db.fetch_all(q)\n"
                    "print(json.dumps({'status': 'success', 'rows': [list(r) for r in rows]}, ensure_ascii=False))\n"
                    "PY"
                )
                result = await client.exec_command(project_id, command, timeout)
                return _json_text(result)

            # ---- set sync mode ----
            elif name == "cq_set_sync_mode":
                timeout = max(0, min(int(arguments.get("timeout", 0)), 300))
                client._sync_timeout = timeout
                if timeout > 0:
                    return _text(f"Sync mode ON: cq_send_message will wait up to {timeout}s for AI reply.")
                return _text("Sync mode OFF: cq_send_message returns immediately.")

            # ---- smart grep ----
            elif name == "cq_smart_grep":
                project_id = int(arguments["project_id"])
                query = str(arguments["query"])
                mode = str(arguments.get("mode", "code"))
                profile = str(arguments.get("profile", "all"))
                time_strict = arguments.get("time_strict")
                is_regex = bool(arguments.get("is_regex", False))
                case_sensitive = bool(arguments.get("case_sensitive", False))
                max_results = int(arguments.get("max_results", 100))
                context_lines = int(arguments.get("context_lines", 0))
                include_glob = arguments.get("include_glob")
                result = await client.smart_grep(
                    project_id=project_id,
                    query=query,
                    mode=mode,
                    profile=profile,
                    time_strict=str(time_strict) if time_strict is not None else None,
                    is_regex=is_regex,
                    case_sensitive=case_sensitive,
                    max_results=max_results,
                    context_lines=context_lines,
                    include_glob=include_glob,
                )
                return _json_text(result)

            # ---- grep logs ----
            elif name == "cq_grep_logs":
                project_id = int(arguments["project_id"])
                query = str(arguments["query"] or "").strip()
                log_masks_raw = arguments.get("log_masks")
                if not query:
                    raise ValueError("query must be non-empty")
                if log_masks_raw is None:
                    log_masks_raw = []
                if not isinstance(log_masks_raw, list):
                    raise ValueError("log_masks must be an array when provided")

                raw_sources = [str(mask).strip() for mask in log_masks_raw if str(mask).strip()]
                docker_services: list[str] = []
                file_masks: list[str] = []
                for source in raw_sources:
                    if source.lower().startswith("docker:"):
                        service = source.split(":", 1)[1].strip()
                        if service:
                            docker_services.append(service)
                    else:
                        file_masks.append(source)

                if not docker_services and not file_masks:
                    raise ValueError("Provide at least one source in log_masks: file glob or docker:<service>")

                tail_lines = int(arguments.get("tail_lines", 100))
                tail_lines = max(1, min(tail_lines, 5000))
                since_seconds = int(arguments.get("since_seconds", 0))
                since_seconds = max(0, min(since_seconds, 7 * 24 * 3600))
                case_sensitive = bool(arguments.get("case_sensitive", False))

                result_payload: dict[str, Any] = {}
                flags = re.MULTILINE if case_sensitive else (re.MULTILINE | re.IGNORECASE)
                pattern = re.compile(query, flags)

                if docker_services:
                    compose_dir = str(Path(__file__).resolve().parent)
                    docker_errors: dict[str, str] = {}
                    for service in docker_services:
                        cmd = [
                            "docker",
                            "compose",
                            "logs",
                            service,
                            "--no-color",
                            "--tail",
                            str(tail_lines),
                        ]
                        if since_seconds > 0:
                            cmd.extend(["--since", f"{since_seconds}s"])

                        proc = await asyncio.create_subprocess_exec(
                            *cmd,
                            cwd=compose_dir,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                            stdin=asyncio.subprocess.DEVNULL,
                        )
                        stdout_bytes, stderr_bytes = await proc.communicate()
                        stdout_text = stdout_bytes.decode("utf-8", errors="replace")
                        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()

                        key = f"docker:{service}"
                        if proc.returncode != 0:
                            result_payload[key] = []
                            docker_errors[key] = stderr_text or f"docker compose logs failed with exit code {proc.returncode}"
                            continue

                        matched = [line for line in stdout_text.splitlines() if pattern.search(line)]
                        result_payload[key] = matched[-tail_lines:] if tail_lines > 0 else matched

                    if docker_errors:
                        result_payload["_docker_errors"] = docker_errors

                encoded_query = base64.b64encode(query.encode("utf-8")).decode("ascii")
                encoded_masks = base64.b64encode(
                    json.dumps(file_masks, ensure_ascii=False).encode("utf-8")
                ).decode("ascii")

                if file_masks:
                    command = (
                        "python3 - <<'PY'\n"
                        "import base64, glob, json, os, re, time\n"
                        "from datetime import datetime\n"
                        f"query = base64.b64decode('{encoded_query}').decode('utf-8')\n"
                        f"masks = json.loads(base64.b64decode('{encoded_masks}').decode('utf-8'))\n"
                        f"tail_lines = {tail_lines}\n"
                        f"since_seconds = {since_seconds}\n"
                        f"case_sensitive = {str(case_sensitive)}\n"
                        "cutoff_ts = (time.time() - since_seconds) if since_seconds > 0 else None\n"
                        "flags = re.MULTILINE if case_sensitive else (re.MULTILINE | re.IGNORECASE)\n"
                        "pattern = re.compile(query, flags)\n"
                        "ts_patterns = [\n"
                        "    re.compile(r'^\\[(\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}:\\d{2})(?:[\\.,]\\d+)?\\]'),\n"
                        "    re.compile(r'^(\\d{4}-\\d{2}-\\d{2}[ T]\\d{2}:\\d{2}:\\d{2})(?:[\\.,]\\d+)?'),\n"
                        "]\n"
                        "ts_formats = ['%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S']\n"
                        "def parse_line_ts(line):\n"
                        "    for rx in ts_patterns:\n"
                        "        m = rx.search(line)\n"
                        "        if not m:\n"
                        "            continue\n"
                        "        raw = m.group(1).replace('T', ' ')\n"
                        "        for fmt in ts_formats:\n"
                        "            try:\n"
                        "                dt = datetime.strptime(raw, fmt.replace('T', ' '))\n"
                        "                return dt.timestamp()\n"
                        "            except ValueError:\n"
                        "                pass\n"
                        "    return None\n"
                        "paths = []\n"
                        "seen = set()\n"
                        "for mask in masks:\n"
                        "    for path in glob.glob(mask, recursive=True):\n"
                        "        norm = os.path.normpath(path)\n"
                        "        if not os.path.isfile(norm):\n"
                        "            continue\n"
                        "        if norm in seen:\n"
                        "            continue\n"
                        "        seen.add(norm)\n"
                        "        paths.append(norm)\n"
                        "paths.sort()\n"
                        "result = {}\n"
                        "for path in paths:\n"
                        "    try:\n"
                        "        with open(path, 'r', encoding='utf-8', errors='replace') as fh:\n"
                        "            lines = fh.read().splitlines()\n"
                        "    except OSError:\n"
                        "        result[path] = []\n"
                        "        continue\n"
                        "    matched = []\n"
                        "    current_ts = None\n"
                        "    for line in lines:\n"
                        "        parsed_ts = parse_line_ts(line)\n"
                        "        if parsed_ts is not None:\n"
                        "            current_ts = parsed_ts\n"
                        "        effective_ts = parsed_ts if parsed_ts is not None else current_ts\n"
                        "        if cutoff_ts is not None and (effective_ts is None or effective_ts < cutoff_ts):\n"
                        "            continue\n"
                        "        if pattern.search(line):\n"
                        "            matched.append(line)\n"
                        "    if tail_lines > 0:\n"
                        "        matched = matched[-tail_lines:]\n"
                        "    result[path] = matched\n"
                        "print(json.dumps(result, ensure_ascii=False))\n"
                        "PY"
                    )

                    result = await client.exec_command(project_id, command, 120)
                    output = result.get("output", "") if isinstance(result, dict) else ""
                    parsed_output = output.strip()
                    if parsed_output.startswith("<stdout>") and "</stdout>" in parsed_output:
                        parsed_output = parsed_output[len("<stdout>"):parsed_output.rfind("</stdout>")].strip()
                    try:
                        parsed = json.loads(parsed_output)
                    except Exception as exc:
                        return _json_text(
                            {
                                "status": "error",
                                "error": f"Failed to parse cq_grep_logs output as JSON: {exc}",
                                "raw_output": output,
                                "exec": result,
                            }
                        )
                    result_payload.update(parsed)

                return _json_text(result_payload)

            # ---- replace in file ----
            elif name == "cq_replace":
                project_id = int(arguments["project_id"])
                file_id = int(arguments["file_id"])
                old = str(arguments["old"])
                new = str(arguments["new"])
                is_regex = bool(arguments.get("is_regex", False))
                case_sensitive = bool(arguments.get("case_sensitive", True))
                max_replacements = int(arguments.get("max_replacements", 0))
                result = await client.replace_file(
                    project_id=project_id,
                    file_id=file_id,
                    old=old,
                    new=new,
                    is_regex=is_regex,
                    case_sensitive=case_sensitive,
                    max_replacements=max_replacements,
                )
                return _json_text(result)

            # ---- process management ----
            elif name == "cq_process_spawn":
                project_id = int(arguments["project_id"])
                command = str(arguments["command"])
                engine = str(arguments.get("engine", "bash"))
                cwd = arguments.get("cwd")
                env = arguments.get("env")
                timeout = int(arguments.get("timeout", 3600))
                
                resp = await httpx.AsyncClient().post(
                    f"{_MCP_SERVER_URL}/process/spawn",
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
                return _json_text(resp.json())

            elif name == "cq_process_io":
                process_guid = str(arguments.get("process_guid") or "")
                if not process_guid:
                    return _text("Missing required argument: process_guid")
                input_data = arguments.get("input")
                read_timeout_ms = int(arguments.get("read_timeout_ms", 5000))
                max_bytes = int(arguments.get("max_bytes", 65536))
                
                resp = await httpx.AsyncClient().post(
                    f"{_MCP_SERVER_URL}/process/io",
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
                # Decode base64 fragments for display
                if result.get("stdout_fragment"):
                    try:
                        result["stdout_fragment"] = base64.b64decode(result["stdout_fragment"]).decode("utf-8", errors="replace")
                    except:
                        pass
                if result.get("stderr_fragment"):
                    try:
                        result["stderr_fragment"] = base64.b64decode(result["stderr_fragment"]).decode("utf-8", errors="replace")
                    except:
                        pass
                return _json_text(result)

            elif name == "cq_process_kill":
                process_guid = str(arguments.get("process_guid") or "")
                if not process_guid:
                    return _text("Missing required argument: process_guid")
                signal_name = str(arguments.get("signal", "SIGTERM"))
                
                resp = await httpx.AsyncClient().post(
                    f"{_MCP_SERVER_URL}/process/kill",
                    json={
                        "process_guid": process_guid,
                        "signal": signal_name,
                    },
                    headers={"Authorization": f"Bearer {_MCP_AUTH_TOKEN}"},
                    timeout=30.0,
                )
                resp.raise_for_status()
                return _json_text(resp.json())

            elif name == "cq_project_status":
                project_id = int(arguments["project_id"])
                status = await client.get_project_status(project_id)
                return _json_text(status)

            elif name == "cq_process_status":
                process_guid = str(arguments.get("process_guid") or "")
                if not process_guid:
                    return _text("Missing required argument: process_guid")
                
                resp = await httpx.AsyncClient().get(
                    f"{_MCP_SERVER_URL}/process/status",
                    params={"process_guid": process_guid},
                    headers={"Authorization": f"Bearer {_MCP_AUTH_TOKEN}"},
                    timeout=30.0,
                )
                resp.raise_for_status()
                return _json_text(resp.json())

            elif name == "cq_process_list":
                project_id = arguments.get("project_id")
                
                params = {}
                if project_id:
                    params["project_id"] = int(project_id)
                
                resp = await httpx.AsyncClient().get(
                    f"{_MCP_SERVER_URL}/process/list",
                    params=params,
                    headers={"Authorization": f"Bearer {_MCP_AUTH_TOKEN}"},
                    timeout=30.0,
                )
                resp.raise_for_status()
                return _json_text(resp.json())

            elif name == "cq_process_wait":
                process_guid = str(arguments.get("process_guid") or "")
                if not process_guid:
                    return _text("Missing required argument: process_guid")
                wait_timeout_ms = int(arguments.get("wait_timeout_ms", 30000))
                wait_condition = str(arguments.get("wait_condition", "any_output"))
                
                resp = await httpx.AsyncClient().post(
                    f"{_MCP_SERVER_URL}/process/wait",
                    json={
                        "process_guid": process_guid,
                        "wait_timeout_ms": wait_timeout_ms,
                        "wait_condition": wait_condition,
                    },
                    headers={"Authorization": f"Bearer {_MCP_AUTH_TOKEN}"},
                    timeout=(wait_timeout_ms / 1000.0) + 10,  # Add 10s overhead
                )
                resp.raise_for_status()
                return _json_text(resp.json())

            # ---- docker control ----
            elif name == "cq_docker_control":
                command = str(arguments.get("command", "status"))
                services = [str(s) for s in (arguments.get("services") or [])]
                timeout = max(10, min(int(arguments.get("timeout", 90)), 600))
                wait = bool(arguments.get("wait", False))

                out = await _invoke_cqds_ctl(command, services, timeout, wait)
                if out["ok"]:
                    return _json_text(out["data"])
                err = str(out.get("error", "error"))
                if out.get("stdout") or out.get("stderr"):
                    return _text(
                        f"cq_docker_control: {err}\n"
                        f"stdout: {str(out.get('stdout', ''))[:600]}\n"
                        f"stderr: {str(out.get('stderr', ''))[:300]}"
                    )
                return _text(f"cq_docker_control: {err}")

            # ---- docker control batch (cqds_ctl) ----
            elif name == "cq_docker_control_batch":
                raw_reqs = arguments.get("requests")
                if not isinstance(raw_reqs, list) or len(raw_reqs) == 0:
                    raise ValueError("cq_docker_control_batch requires a non-empty requests array")
                stop_on_error = bool(arguments.get("stop_on_error", False))
                results: list[dict[str, Any]] = []

                for i, raw in enumerate(raw_reqs):
                    if not isinstance(raw, dict):
                        row: dict[str, Any] = {
                            "index": i,
                            "ok": False,
                            "error": "request must be an object",
                            "request": raw,
                        }
                        results.append(row)
                        if stop_on_error:
                            break
                        continue

                    command = str(raw.get("command", "status"))
                    services = [str(s) for s in (raw.get("services") or [])]
                    timeout = max(10, min(int(raw.get("timeout", 90)), 600))
                    wait = bool(raw.get("wait", False))

                    if command not in _DOCKER_CTL_ALLOWED:
                        row = {
                            "index": i,
                            "ok": False,
                            "error": (
                                f"Unknown command '{command}'. "
                                f"Allowed: {', '.join(sorted(_DOCKER_CTL_ALLOWED))}"
                            ),
                            "request": raw,
                        }
                        results.append(row)
                        if stop_on_error:
                            break
                        continue

                    out = await _invoke_cqds_ctl(command, services, timeout, wait)
                    if out["ok"]:
                        results.append(
                            {
                                "index": i,
                                "ok": True,
                                "request": raw,
                                "response": out["data"],
                            }
                        )
                    else:
                        row = {
                            "index": i,
                            "ok": False,
                            "request": raw,
                            "error": out.get("error"),
                            "stdout": out.get("stdout"),
                            "stderr": out.get("stderr"),
                        }
                        results.append(row)
                        if stop_on_error:
                            break

                all_ok = all(r.get("ok") for r in results)
                return _json_text(
                    {
                        "results": results,
                        "all_ok": all_ok,
                        "count": len(results),
                    }
                )

            # ---- docker exec (CLI on MCP host) ----
            elif name == "cq_docker_exec":
                raw_reqs = arguments.get("requests")
                if not isinstance(raw_reqs, list) or len(raw_reqs) == 0:
                    raise ValueError("cq_docker_exec requires a non-empty requests array")
                stop_on_error = bool(arguments.get("stop_on_error", False))
                results: list[dict[str, Any]] = []
                for i, raw in enumerate(raw_reqs):
                    if not isinstance(raw, dict):
                        results.append(
                            {
                                "index": i,
                                "ok": False,
                                "error": "request must be an object",
                                "request": raw,
                            }
                        )
                        if stop_on_error:
                            break
                        continue
                    row = await _docker_exec_batch_item(raw)
                    row["index"] = i
                    results.append(row)
                    if stop_on_error and not row.get("ok"):
                        break
                all_ok = all(r.get("ok") for r in results)
                return _json_text(
                    {"results": results, "all_ok": all_ok, "count": len(results)}
                )

            # ---- host-local subprocesses (MCP machine, not sandbox) ----
            elif name == "cq_host_process_spawn":
                if len(host_proc_registry) >= _MAX_HOST_PROCS:
                    return _text(f"Too many host processes (max {_MAX_HOST_PROCS})")
                cmd = arguments.get("command")
                if cmd is None:
                    return _text("Missing required argument: command")
                cwd = arguments.get("cwd")
                cwd_s = str(cwd) if cwd else None
                env_arg = arguments.get("env")
                env_merged = os.environ.copy()
                if isinstance(env_arg, dict):
                    for k, v in env_arg.items():
                        env_merged[str(k)] = str(v)
                timeout_sec = max(1, min(int(arguments.get("timeout", 3600)), 7200))

                try:
                    if isinstance(cmd, str):
                        proc = await asyncio.create_subprocess_shell(
                            cmd,
                            cwd=cwd_s,
                            env=env_merged,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                            stdin=asyncio.subprocess.PIPE,
                        )
                        desc = cmd[:500]
                    elif isinstance(cmd, list):
                        if not cmd:
                            return _text("command array must be non-empty")
                        argv = [str(x) for x in cmd]
                        proc = await asyncio.create_subprocess_exec(
                            *argv,
                            cwd=cwd_s,
                            env=env_merged,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                            stdin=asyncio.subprocess.PIPE,
                        )
                        desc = " ".join(argv)[:500]
                    else:
                        return _text("command must be string or array")
                except Exception as exc:
                    return _text(f"spawn failed: {exc}")

                guid = str(uuid.uuid4())
                rec = HostProcRecord(proc, desc)
                rec.pump_out = asyncio.create_task(
                    _host_pump_stream(proc.stdout, rec.stdout_acc, _MAX_HOST_PROC_BUFF)
                )
                rec.pump_err = asyncio.create_task(
                    _host_pump_stream(proc.stderr, rec.stderr_acc, _MAX_HOST_PROC_BUFF)
                )

                async def _host_ttl_kill() -> None:
                    await asyncio.sleep(float(timeout_sec))
                    if proc.returncode is None:
                        proc.kill()

                rec.ttl_task = asyncio.create_task(_host_ttl_kill())
                host_proc_registry[guid] = rec
                return _json_text(
                    {"process_guid": guid, "pid": proc.pid, "command": desc, "timeout": timeout_sec}
                )

            elif name == "cq_host_process_io":
                process_guid = str(arguments.get("process_guid") or "")
                if not process_guid:
                    return _text("Missing required argument: process_guid")
                rec = host_proc_registry.get(process_guid)
                if not rec:
                    return _text("Unknown process_guid")
                inp = arguments.get("input")
                if inp is not None and rec.proc.stdin:
                    try:
                        if rec.proc.stdin.is_closing():
                            pass
                        else:
                            rec.proc.stdin.write(str(inp).encode("utf-8", errors="replace"))
                            await rec.proc.stdin.drain()
                    except Exception as exc:
                        return _json_text(
                            {
                                "error": f"stdin write failed: {exc}",
                                "stdout_fragment": _host_tail_text(
                                    rec.stdout_acc, int(arguments.get("max_bytes", 65536))
                                ),
                                "stderr_fragment": _host_tail_text(
                                    rec.stderr_acc, int(arguments.get("max_bytes", 65536))
                                ),
                                "alive": rec.proc.returncode is None,
                                "returncode": rec.proc.returncode,
                            }
                        )
                max_bytes = int(arguments.get("max_bytes", 65536))
                read_timeout_ms = max(0, int(arguments.get("read_timeout_ms", 5000)))
                await asyncio.sleep(min(read_timeout_ms / 1000.0, 2.0))
                return _json_text(
                    {
                        "stdout_fragment": _host_tail_text(rec.stdout_acc, max_bytes),
                        "stderr_fragment": _host_tail_text(rec.stderr_acc, max_bytes),
                        "alive": rec.proc.returncode is None,
                        "returncode": rec.proc.returncode,
                    }
                )

            elif name == "cq_host_process_kill":
                process_guid = str(arguments.get("process_guid") or "")
                if not process_guid:
                    return _text("Missing required argument: process_guid")
                rec = host_proc_registry.get(process_guid)
                if not rec:
                    return _text("Unknown process_guid")
                signal_name = str(arguments.get("signal", "SIGTERM"))
                if rec.ttl_task and not rec.ttl_task.done():
                    rec.ttl_task.cancel()
                for t in (rec.pump_out, rec.pump_err):
                    if t and not t.done():
                        t.cancel()
                try:
                    if rec.proc.returncode is None:
                        if signal_name == "SIGKILL":
                            rec.proc.kill()
                        else:
                            rec.proc.terminate()
                        try:
                            await asyncio.wait_for(rec.proc.wait(), timeout=8.0)
                        except asyncio.TimeoutError:
                            rec.proc.kill()
                            await rec.proc.wait()
                finally:
                    for t in (rec.pump_out, rec.pump_err):
                        if t and not t.done():
                            try:
                                await t
                            except asyncio.CancelledError:
                                pass
                    host_proc_registry.pop(process_guid, None)
                return _json_text({"stopped": True, "returncode": rec.proc.returncode})

            elif name == "cq_host_process_status":
                process_guid = str(arguments.get("process_guid") or "")
                if not process_guid:
                    return _text("Missing required argument: process_guid")
                rec = host_proc_registry.get(process_guid)
                if not rec:
                    return _text("Unknown process_guid")
                alive = rec.proc.returncode is None
                runtime_ms = int((time.monotonic() - rec.started) * 1000)
                return _json_text(
                    {
                        "alive": alive,
                        "returncode": rec.proc.returncode,
                        "pid": rec.proc.pid,
                        "runtime_ms": runtime_ms,
                        "command": rec.argv_desc,
                    }
                )

            elif name == "cq_host_process_list":
                items: list[dict[str, Any]] = []
                for g, r in host_proc_registry.items():
                    items.append(
                        {
                            "process_guid": g,
                            "pid": r.proc.pid,
                            "alive": r.proc.returncode is None,
                            "returncode": r.proc.returncode,
                            "command": r.argv_desc,
                        }
                    )
                return _json_text({"processes": items, "count": len(items)})

            elif name == "cq_host_process_wait":
                process_guid = str(arguments.get("process_guid") or "")
                if not process_guid:
                    return _text("Missing required argument: process_guid")
                rec = host_proc_registry.get(process_guid)
                if not rec:
                    return _text("Unknown process_guid")
                wait_ms = max(0, int(arguments.get("wait_timeout_ms", 30000)))
                condition = str(arguments.get("wait_condition", "any_output"))
                baseline = len(rec.stdout_acc) + len(rec.stderr_acc)
                deadline = time.monotonic() + wait_ms / 1000.0
                while time.monotonic() < deadline:
                    if rec.proc.returncode is not None:
                        return _json_text(
                            {"finished": True, "returncode": rec.proc.returncode}
                        )
                    if condition == "any_output" and (
                        len(rec.stdout_acc) + len(rec.stderr_acc) > baseline
                    ):
                        return _json_text({"finished": False, "saw_output": True})
                    await asyncio.sleep(0.05)
                return _json_text({"finished": False, "timed_out": True})

            else:

                return _text(f"Unknown tool: {name}")

        except httpx.HTTPStatusError as exc:
            LOGGER.exception("TOOL call http error name=%s status=%s", name, exc.response.status_code)
            return _text(f"HTTP error {exc.response.status_code}: {exc.response.text}")
        except Exception as exc:
            LOGGER.exception("TOOL call error name=%s", name)
            return _text(f"Error: {exc}")
        finally:
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            LOGGER.info("TOOL call end name=%s elapsed_ms=%d", name, elapsed_ms)
            CURRENT_TOOL.reset(tool_token)

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _read_password_file(password_file: str) -> str:
    try:
        with open(password_file, "r", encoding="utf-8") as handle:
            password = handle.read().strip()
    except OSError as exc:
        raise RuntimeError(f"Failed to read password file '{password_file}': {exc}") from exc
    if not password:
        raise RuntimeError(f"Password file '{password_file}' is empty")
    return password


def _resolve_password(
    cli_password: str | None,
    cli_password_file: str | None,
) -> tuple[str, str]:
    if cli_password:
        return cli_password, "--password"
    if cli_password_file:
        return _read_password_file(cli_password_file), "--password-file"

    env_password = os.environ.get("COLLOQUIUM_PASSWORD")
    if env_password:
        return env_password, "COLLOQUIUM_PASSWORD"

    env_password_file = os.environ.get("COLLOQUIUM_PASSWORD_FILE")
    if env_password_file:
        return _read_password_file(env_password_file), "COLLOQUIUM_PASSWORD_FILE"

    sidecar_secret = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "copilot_mcp_tool.secret",
    )
    if os.path.isfile(sidecar_secret):
        return _read_password_file(sidecar_secret), "copilot_mcp_tool.secret"

    return "devspace", "default"


def _password_preview(password: str) -> str:
    if not password:
        return "<empty>"
    if len(password) <= 2:
        return password
    return f"{password[:2]}..."

def main() -> None:
    parser = argparse.ArgumentParser(
        description="MCP proxy server for Colloquium-DevSpace",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Environment variables (override CLI defaults):
              COLLOQUIUM_URL       Base URL of Colloquium-DevSpace  (default: http://localhost:8008)
              COLLOQUIUM_USERNAME  Login username                   (default: copilot)
              COLLOQUIUM_PASSWORD  Login password                   (higher priority than file/env file)
              COLLOQUIUM_PASSWORD_FILE  Path to file containing only the password
        """),
    )
    parser.add_argument(
        "--url", default=os.environ.get("COLLOQUIUM_URL", "http://localhost:8008"),
        help="Base URL of Colloquium-DevSpace (default: http://localhost:8008)",
    )
    parser.add_argument(
        "--username", default=os.environ.get("COLLOQUIUM_USERNAME", "copilot"),
        help="Username for Colloquium login (default: copilot)",
    )
    parser.add_argument(
        "--password", default=None,
        help="Password for Colloquium login (overrides password file and env file)",
    )
    parser.add_argument(
        "--password-file", default=None,
        help="Path to a file containing only the Colloquium password",
    )
    parser.add_argument(
        "--chat-id", type=int, default=int(os.environ.get("COLLOQUIUM_CHAT_ID", "0") or "0"),
        help="Default chat ID (informational; individual tools accept chat_id)",
    )
    args = parser.parse_args()

    log_file = _setup_logging()
    LOGGER.info("MCP tool start url=%s username=%s pid=%s", args.url, args.username, os.getpid())

    password, password_source = _resolve_password(args.password, args.password_file)

    if not password:
        print(
            "ERROR: Colloquium password is required. "
            "Set --password, --password-file, COLLOQUIUM_PASSWORD, "
            "COLLOQUIUM_PASSWORD_FILE, or create copilot_mcp_tool.secret next to the script.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(
        "MCP auth password source: "
        f"{password_source}; preview={_password_preview(password)}",
        file=sys.stderr,
    )
    LOGGER.info(
        "MCP auth password source=%s preview=%s log_file=%s",
        password_source,
        _password_preview(password),
        str(log_file),
    )

    client = ColloquiumClient(
        base_url=args.url,
        username=args.username,
        password=password,
    )

    try:
        asyncio.run(run_server(client))
    finally:
        asyncio.run(client.aclose())


if __name__ == "__main__":
    main()
