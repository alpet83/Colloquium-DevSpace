# cqds_chat.py — Tools and handlers for chat operations (list, create, send, wait, history, stats)
from __future__ import annotations

import time
from typing import Any

from mcp.types import CallToolResult, Tool  # type: ignore[import]

from cqds_helpers import _extract_latest_message, _is_progress_stub, _json_text, _text
from cqds_run_ctx import RunContext

# ---------------------------------------------------------------------------
# Tool definitions
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
]

# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

async def handle(
    name: str, arguments: dict[str, Any], ctx: RunContext
) -> CallToolResult | None:
    client = ctx.client

    if name == "cq_list_chats":
        chats = await client.list_chats()
        return _json_text(chats)

    if name == "cq_create_chat":
        description = arguments.get("description", "MCP Session")
        chat_id = await client.create_chat(description)
        return _text(f"Created chat with chat_id={chat_id}")

    if name == "cq_send_message":
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
            return _text(
                f"Message sent to chat_id={chat_id} (sync: no reply in {client._sync_timeout}s)"
            )
        return _text(f"Message sent to chat_id={chat_id}")

    if name == "cq_wait_reply":
        chat_id = int(arguments["chat_id"])
        resp = await client.get_reply(chat_id)
        return _json_text(resp)

    if name == "cq_get_history":
        chat_id = int(arguments["chat_id"])
        resp = await client.get_history(chat_id)
        return _json_text(resp)

    if name == "cq_chat_stats":
        chat_id = int(arguments["chat_id"])
        since_seconds_raw = arguments.get("since_seconds", 0)
        since_seconds = int(since_seconds_raw) if since_seconds_raw is not None else 0
        since_seconds = max(0, min(since_seconds, 30 * 24 * 3600))
        resp = await client.get_chat_stats(
            chat_id=chat_id,
            since_seconds=since_seconds if since_seconds > 0 else None,
        )
        return _json_text(resp)

    return None
