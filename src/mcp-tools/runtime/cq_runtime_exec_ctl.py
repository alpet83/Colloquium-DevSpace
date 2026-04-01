from __future__ import annotations

from typing import Any

from mcp.types import CallToolResult  # type: ignore[import]

import cqds_exec
from cq_runtime_ctl_dispatch import handle_ctl, make_ctl_tool
from cqds_run_ctx import RunContext

_ACTION_TO_LEGACY: dict[str, str] = {
    "exec": "cq_exec",
    "spawn_script": "cq_spawn_script",
}

TOOLS = [
    make_ctl_tool(
        name="cq_exec_ctl",
        headline="Colloquium workspace shell: cq_exec (incl. command batches) and cq_spawn_script.",
        actions_enum=list(_ACTION_TO_LEGACY),
    )
]


async def handle(name: str, arguments: dict[str, Any], ctx: RunContext) -> CallToolResult | None:
    return await handle_ctl(
        "cq_exec_ctl",
        name,
        arguments,
        ctx,
        legacy_handle=cqds_exec.handle,
        action_to_legacy=_ACTION_TO_LEGACY,
    )
