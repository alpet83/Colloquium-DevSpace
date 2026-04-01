from __future__ import annotations

from typing import Any

from mcp.types import CallToolResult  # type: ignore[import]

import cqds_project
from cq_runtime_ctl_dispatch import handle_ctl, make_ctl_tool
from cqds_run_ctx import RunContext

_ACTION_TO_LEGACY: dict[str, str] = {
    "fetch_result": "cq_fetch_result",
    "list_projects": "cq_list_projects",
    "select_project": "cq_select_project",
    "query_db": "cq_query_db",
    "set_sync_mode": "cq_set_sync_mode",
    "project_status": "cq_project_status",
}

TOOLS = [
    make_ctl_tool(
        name="cq_project_ctl",
        headline="Colloquium projects: list/select, DB query, status, grep paging (fetch_result), sync mode.",
        actions_enum=list(_ACTION_TO_LEGACY),
    )
]


async def handle(name: str, arguments: dict[str, Any], ctx: RunContext) -> CallToolResult | None:
    return await handle_ctl(
        "cq_project_ctl",
        name,
        arguments,
        ctx,
        legacy_handle=cqds_project.handle,
        action_to_legacy=_ACTION_TO_LEGACY,
    )
