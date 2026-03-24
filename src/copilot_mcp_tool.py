# copilot_mcp_tool.py — MCP server bridging GitHub Copilot to Colloquium-DevSpace
# Place: P:\GitHub\Colloquium-DevSpace\src\copilot_mcp_tool.py
#
# Usage:
#   python copilot_mcp_tool.py [--url URL] [--username USER] [--password PASS]
#                               [--chat-id ID] [--timeout SEC]
#
# Default URL: http://localhost:8008
# Credentials can also be set via env vars:
#   COLLOQUIUM_URL, COLLOQUIUM_USERNAME, COLLOQUIUM_PASSWORD, COLLOQUIUM_CHAT_ID

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
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
        )
        self._logged_in = False

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
        """Set the active project context on the server (global, until fixed)."""
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

    async def get_index(self, chat_id: int) -> dict:
        """Return the rich entity index for a chat via /api/chat/index."""
        await self._ensure_login()
        resp = await self._client.get("/api/chat/index", params={"chat_id": chat_id})
        resp.raise_for_status()
        return resp.json()

    async def get_code_index(self, project_id: int) -> dict:
        """Build and return the rich entity index for a project on demand via /api/project/code_index."""
        await self._ensure_login()
        resp = await self._client.get("/api/project/code_index", params={"project_id": project_id})
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
            "Return the rich entity index built from the last LLM context assembly for a chat. "
            "Includes all parsed functions, classes, methods and variables with their file_id, "
            "line ranges and token counts. Useful for code navigation and understanding project structure.\n"
            "Format: sandwiches_index.jsl — 'entities' is a list of CSV strings, layout described in 'templates.entities':\n"
            "  vis,type,parent,name,file_id,start_line-end_line,tokens\n"
            "  e.g. 'pub,function,,fetchData,3,45-67,120'\n"
            "'filelist' maps file IDs to file names (same format as cq_list_files).\n"
            "Returns 404 if no LLM response has been generated yet for this chat."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "chat_id": {
                    "type": "integer",
                    "description": "Chat ID whose index to retrieve (use cq_list_chats to get IDs).",
                },
            },
            "required": ["chat_id"],
        },
    ),
    Tool(
        name="cq_get_code_index",
        description=(
            "Build the rich entity index for a project on demand — no prior LLM interaction needed.\n"
            "Runs context assembly (loads all project files → SandwichPack.pack) and returns\n"
            "the full sandwiches_index.jsl format JSON with 'entities' and 'filelist'.\n"
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
            },
            "required": ["project_id"],
        },
    ),
]


def _text(content: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=content)])


def _json_text(obj: Any) -> CallToolResult:
    return _text(json.dumps(obj, ensure_ascii=False, indent=2))


async def run_server(client: ColloquiumClient) -> None:
    server = Server("colloquium-mcp")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return TOOLS

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
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
                chat_id = int(arguments["chat_id"])
                index = await client.get_index(chat_id)
                return _json_text(index)

            # ---- get code index (on-demand, project-level) ----
            elif name == "cq_get_code_index":
                project_id = int(arguments["project_id"])
                index = await client.get_code_index(project_id)
                return _json_text(index)

            else:
                return _text(f"Unknown tool: {name}")

        except httpx.HTTPStatusError as exc:
            return _text(f"HTTP error {exc.response.status_code}: {exc.response.text}")
        except Exception as exc:
            return _text(f"Error: {exc}")

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="MCP proxy server for Colloquium-DevSpace",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Environment variables (override CLI defaults):
              COLLOQUIUM_URL       Base URL of Colloquium-DevSpace  (default: http://localhost:8008)
              COLLOQUIUM_USERNAME  Login username                   (default: admin)
              COLLOQUIUM_PASSWORD  Login password                   (required if not via --password)
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
        "--password", default=os.environ.get("COLLOQUIUM_PASSWORD", "devspace"),
        help="Password for Colloquium login (default: devspace)",
    )
    parser.add_argument(
        "--chat-id", type=int, default=int(os.environ.get("COLLOQUIUM_CHAT_ID", "0") or "0"),
        help="Default chat ID (informational; individual tools accept chat_id)",
    )
    args = parser.parse_args()

    if not args.password:
        print(
            "ERROR: Colloquium password is required. "
            "Set --password or COLLOQUIUM_PASSWORD env var (default: devspace).",
            file=sys.stderr,
        )
        sys.exit(1)

    client = ColloquiumClient(
        base_url=args.url,
        username=args.username,
        password=args.password,
    )

    try:
        asyncio.run(run_server(client))
    finally:
        asyncio.run(client.aclose())


if __name__ == "__main__":
    main()
