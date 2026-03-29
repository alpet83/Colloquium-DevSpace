# cqds_run_ctx.py — RunContext: per-server-run shared state passed to all module handlers
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from cqds_client import ColloquiumClient


@dataclass
class RunContext:
    """All mutable per-run state shared across handler modules."""

    client: ColloquiumClient
    host_proc_registry: dict[str, Any] = field(default_factory=dict)
    index_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    index_jobs: dict[int, dict[str, Any]] = field(default_factory=dict)
    # Callables injected by run_server() after initialisation:
    ensure_index_worker: Callable[[], Awaitable[None]] | None = None
    queue_status: Callable[[int], dict[str, Any]] | None = None
