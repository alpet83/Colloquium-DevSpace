# copilot_mcp_tool.py — MCP server bridging GitHub Copilot to Colloquium-DevSpace
# Place: P:\GitHub\Colloquium-DevSpace\src\copilot_mcp_tool.py
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
import json
import logging
import os
from pathlib import Path
import re
import sys
import time
import textwrap
from typing import Any

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
    """Walk up from cwd and script dir looking for .vscode/mcp.json with MCP_AUTH_TOKEN."""
    script_path = str(Path(__file__).resolve())
    seen: set[Path] = set()
    candidates: list[Path] = []
    for base in (Path.cwd(), Path(__file__).resolve().parent):
        for parent in (base, *base.parents):
            p = parent / ".vscode" / "mcp.json"
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
        # Per-server env block — match by script path in args or server name
        for server_name, server_cfg in (data.get("servers") or {}).items():
            if not isinstance(server_cfg, dict):
                continue
            args = server_cfg.get("args") or []
            if not (any(script_path in str(a) for a in args) or server_name == "colloquium"):
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
        description="List all chats available in Colloquium-DevSpace.",
        inputSchema={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    Tool(
        name="cq_create_chat",
        description="Create a new chat in Colloquium-DevSpace. Returns the new chat_id.",
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
            "Use colloquium_wait_reply to get the AI response."
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
        name="cq_edit_file",
        description=(
            "Ask Colloquium to write (create or overwrite) a file inside the active project. "
            "Sends a <code_file> XML block as a chat message, which the backend processes "
            "and saves to disk on the Colloquium host."
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
            "Sends a <patch> XML block as a chat message."
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
        description="List all projects registered in Colloquium-DevSpace.",
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
            "NOTE: these IDs are NOT the same as sandwich-pack index numbers."
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
            "Execute a shell command in a project's working directory and return stdout/stderr immediately. "
            "Supports string command or JSON command batches in a single call. "
            "Direct call — no LLM or chat round-trip required. "
            "Use cq_list_projects to find project_id. "
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
            "Execute a read-only SQL query through Colloquium backend DB layer and return rows as JSON. "
            "Designed for debugging. SELECT/EXPLAIN/WITH only; mutating SQL is rejected."
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
                    "description": "Read-only SQL query (SELECT/EXPLAIN/WITH).",
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
            "Search text or regex in predefined project file sets (code/logs/docs/all) in one direct call. "
            "Useful for fast code/log analysis without LLM chat loop."
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
            "Accepts file masks (glob array) and returns JSON map: {logname: [matched lines]}."
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
                    "description": "One or more glob masks, e.g. ['logs/*.log', 'logs/**/*.txt'].",
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
            "required": ["project_id", "query", "log_masks"],
        },
    ),
    Tool(
        name="cq_replace",
        description=(
            "Replace text in one file directly by file_id, with optional regex mode. "
            "No chat/LLM round-trip; safe for targeted mechanical edits."
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
            "Supports bash or python scripts with custom cwd/env/timeout."
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


async def run_server(client: ColloquiumClient) -> None:
    server = Server("colloquium-mcp")
    index_queue: asyncio.Queue[int] = asyncio.Queue()
    index_jobs: dict[int, dict[str, Any]] = {}
    index_worker_task: asyncio.Task | None = None

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
                modified_since = int(modified_since) if modified_since is not None else None
                file_ids = [int(x.strip()) for x in file_ids_raw.split(",")] if file_ids_raw else None
                files = await client.list_files(project_id, modified_since, file_ids, include_size)
                return _json_text(files)

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
                timeout = int(arguments.get("timeout", 30))
                if not query:
                    raise ValueError("query must be non-empty")

                ql = query.lower().lstrip()
                if not (ql.startswith("select") or ql.startswith("with") or ql.startswith("explain")):
                    raise ValueError("Only read-only SQL is allowed (SELECT/WITH/EXPLAIN)")
                if re.search(r"\b(insert|update|delete|drop|alter|truncate|create|grant|revoke|comment)\b", ql):
                    raise ValueError("Mutating SQL keywords are not allowed in cq_query_db")

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
                if not isinstance(log_masks_raw, list) or not log_masks_raw:
                    raise ValueError("log_masks must be a non-empty array of glob masks")

                log_masks = [str(mask).strip() for mask in log_masks_raw if str(mask).strip()]
                if not log_masks:
                    raise ValueError("log_masks must contain at least one non-empty mask")

                tail_lines = int(arguments.get("tail_lines", 100))
                tail_lines = max(1, min(tail_lines, 5000))
                since_seconds = int(arguments.get("since_seconds", 0))
                since_seconds = max(0, min(since_seconds, 7 * 24 * 3600))
                case_sensitive = bool(arguments.get("case_sensitive", False))

                encoded_query = base64.b64encode(query.encode("utf-8")).decode("ascii")
                encoded_masks = base64.b64encode(
                    json.dumps(log_masks, ensure_ascii=False).encode("utf-8")
                ).decode("ascii")

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
                return _json_text(parsed)

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

                allowed = {"status", "restart", "rebuild", "clear-logs"}
                if command not in allowed:
                    raise ValueError(f"Unknown command '{command}'. Allowed: {', '.join(sorted(allowed))}")

                ctl_script = Path(__file__).resolve().parent / "scripts" / "cqds_ctl.py"
                if not ctl_script.is_file():
                    raise RuntimeError(f"cqds_ctl.py not found at {ctl_script}")

                # Inherit current env and fill required docker-compose vars if absent,
                # so that `docker compose` can parse the compose file without hanging on stdin.
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

                LOGGER.info("cq_docker_control: %s", " ".join(cmd_args))
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
                    return _text(f"cq_docker_control: timed out after {proc_timeout}s")

                stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
                stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()

                try:
                    payload = json.loads(stdout_text)
                except Exception:
                    return _text(
                        f"cq_docker_control: non-JSON output from cqds_ctl.py\n"
                        f"stdout: {stdout_text[:600]}\nstderr: {stderr_text[:300]}"
                    )
                return _json_text(payload)

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
