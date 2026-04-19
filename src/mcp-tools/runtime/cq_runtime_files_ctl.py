from __future__ import annotations

from typing import Any

from mcp.types import CallToolResult  # type: ignore[import]

import cqds_files
from cq_runtime_ctl_dispatch import handle_ctl, make_ctl_tool
from cqds_run_ctx import RunContext

_ACTION_TO_LEGACY: dict[str, str] = {
    "edit_file": "cq_edit_file",
    "patch_file": "cq_patch_file",
    "undo_file": "cq_undo_file",
    "list_files": "cq_list_files",
    "get_index": "cq_get_index",
    "index_job_status": "cq_index_job_status",
    "rebuild_index": "cq_rebuild_index",
    "get_code_index": "cq_get_code_index",
    "grep_entity": "cq_grep_entity",
    "read_file": "cq_read_file",
    "start_grep": "cq_start_grep",
    "grep_logs": "cq_grep_logs",
    "replace": "cq_replace",
}

TOOLS = [
    make_ctl_tool(
        name="cq_files_ctl",
        headline="Colloquium files/index/grep: list/read/replace, chat XML edits, smart grep, entity search.",
        actions_enum=list(_ACTION_TO_LEGACY),
    )
]


async def handle(name: str, arguments: dict[str, Any], ctx: RunContext) -> CallToolResult | None:
    return await handle_ctl(
        "cq_files_ctl",
        name,
        arguments,
        ctx,
        legacy_handle=cqds_files.handle,
        action_to_legacy=_ACTION_TO_LEGACY,
    )
