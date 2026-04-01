from __future__ import annotations

from typing import Any

from mcp.types import CallToolResult  # type: ignore[import]

import cqds_chat
from cq_runtime_ctl_dispatch import handle_ctl, make_ctl_tool
from cqds_run_ctx import RunContext

_ACTION_TO_LEGACY: dict[str, str] = {
    "list_chats": "cq_list_chats",
    "create_chat": "cq_create_chat",
    "send_message": "cq_send_message",
    "wait_reply": "cq_wait_reply",
    "get_history": "cq_get_history",
    "chat_stats": "cq_chat_stats",
}

TOOLS = [
    make_ctl_tool(
        name="cq_chat_ctl",
        headline="Colloquium chats: list, create, send, wait, history, stats.",
        actions_enum=list(_ACTION_TO_LEGACY),
    )
]


async def handle(name: str, arguments: dict[str, Any], ctx: RunContext) -> CallToolResult | None:
    return await handle_ctl(
        "cq_chat_ctl",
        name,
        arguments,
        ctx,
        legacy_handle=cqds_chat.handle,
        action_to_legacy=_ACTION_TO_LEGACY,
    )
