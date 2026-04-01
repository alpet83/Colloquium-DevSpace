from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any

# Make parent mcp-tools importable; script dir (runtime/) is already on sys.path[0].
_THIS_DIR = Path(__file__).resolve().parent
_PARENT = _THIS_DIR.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

from mcp.server import Server  # type: ignore[import]
from mcp.server.stdio import stdio_server  # type: ignore[import]
from mcp.types import CallToolResult, Tool  # type: ignore[import]

from cqds_client import ColloquiumClient
from cqds_mcp_version import print_ident_stderr
from cqds_helpers import (
    CURRENT_TOOL,
    LOGGER,
    _index_counts,
    _setup_logging,
    _summarize_arguments,
    _text,
    cq_filter_tools_for_list,
    cq_hide_tool_names,
    cq_tool_is_hidden,
)
from cqds_run_ctx import RunContext

import cq_runtime_chat_ctl
import cq_runtime_docker_ctl
import cq_runtime_exec_ctl
import cq_runtime_files_ctl
import cq_runtime_help
import cq_runtime_process_ctl
import cq_runtime_project_ctl

HELP_MODULES = (cq_runtime_help,)
CTX_MODULES = (
    cq_runtime_chat_ctl,
    cq_runtime_project_ctl,
    cq_runtime_files_ctl,
    cq_runtime_exec_ctl,
    cq_runtime_process_ctl,
    cq_runtime_docker_ctl,
)
ALL_MODULES = HELP_MODULES + CTX_MODULES


def _http_status_error_payload(exc: BaseException) -> str | None:
    """Avoid importing httpx here; match HTTPStatusError by shape (same as httpx)."""
    if type(exc).__name__ != "HTTPStatusError":
        return None
    resp = getattr(exc, "response", None)
    if resp is None or not hasattr(resp, "status_code"):
        return None
    try:
        text = getattr(resp, "text", "") or ""
    except Exception:
        text = ""
    return f"HTTP error {resp.status_code}: {text}"


def _registered_tools() -> list[Tool]:
    out: list[Tool] = []
    for mod in ALL_MODULES:
        out.extend(getattr(mod, "TOOLS", []))
    return out


async def run_server(client: ColloquiumClient) -> None:
    server = Server("cqds-mcp-runtime")
    index_queue: asyncio.Queue[int] = asyncio.Queue()
    index_jobs: dict[int, dict[str, object]] = {}
    index_worker_task: asyncio.Task[None] | None = None

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

    def queue_status(project_id: int) -> dict[str, object]:
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

    run_ctx = RunContext(
        client=client,
        host_proc_registry={},
        index_queue=index_queue,
        index_jobs=index_jobs,
        ensure_index_worker=ensure_index_worker,
        queue_status=queue_status,
    )

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return cq_filter_tools_for_list(_registered_tools())

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
        token = CURRENT_TOOL.set(name)
        LOGGER.info("RUNTIME TOOL call start name=%s args=[%s]", name, _summarize_arguments(arguments))
        try:
            if cq_tool_is_hidden(name):
                return _text(
                    f"Tool '{name}' is excluded for this configuration (CQ_HIDE_TOOLS)."
                )
            for mod in HELP_MODULES:
                delegated = await mod.handle(name, arguments)
                if delegated is not None:
                    return delegated
            for mod in CTX_MODULES:
                delegated = await mod.handle(name, arguments, run_ctx)
                if delegated is not None:
                    return delegated
            return _text(f"Unknown tool: {name}")
        except Exception as exc:
            http_msg = _http_status_error_payload(exc)
            if http_msg is not None:
                LOGGER.exception("RUNTIME TOOL http error name=%s", name)
                return _text(http_msg)
            LOGGER.exception("RUNTIME TOOL call error name=%s", name)
            return _text(f"Error: {exc}")
        finally:
            CURRENT_TOOL.reset(token)

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    parser = argparse.ArgumentParser(description="CQDS runtime MCP (compact toolset)")
    parser.add_argument("--url", default=os.environ.get("COLLOQUIUM_URL", "http://localhost:8008"))
    parser.add_argument("--username", default=os.environ.get("COLLOQUIUM_USERNAME", "copilot"))
    parser.add_argument("--password", default=os.environ.get("COLLOQUIUM_PASSWORD", "devspace"))
    args = parser.parse_args()

    print_ident_stderr("runtime")
    os.environ.setdefault("COLLOQUIUM_MCP_LOG_STEM", "cqds_mcp_runtime")
    _setup_logging()
    LOGGER.info(
        "CQDS runtime MCP (compact) pid=%s url=%s user=%s",
        os.getpid(),
        args.url,
        args.username,
    )
    if cq_hide_tool_names():
        LOGGER.info("CQ_HIDE_TOOLS active: %s", sorted(cq_hide_tool_names()))
    client = ColloquiumClient(base_url=args.url, username=args.username, password=args.password)
    try:
        asyncio.run(run_server(client))
    finally:
        asyncio.run(client.aclose())


if __name__ == "__main__":
    main()
