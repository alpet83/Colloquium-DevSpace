# copilot_mcp_tool.py — thin MCP entrypoint for Colloquium-DevSpace

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import textwrap
import time

import httpx  # type: ignore[import]
from mcp.server import Server  # type: ignore[import]
from mcp.server.stdio import stdio_server  # type: ignore[import]
from mcp.types import CallToolResult, Tool  # type: ignore[import]

import cqds_chat
import cqds_credentials as cq_cred
import cqds_docker
import cqds_exec
import cqds_files
import cqds_host
import cqds_process
import cqds_project
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


MODULE_HANDLERS = (
    cqds_chat,
    cqds_files,
    cqds_project,
    cqds_exec,
    cqds_process,
    cqds_docker,
    cqds_host,
)


def _registered_tools() -> list[Tool]:
    by_name: dict[str, Tool] = {}
    for module in MODULE_HANDLERS:
        for tool in getattr(module, "TOOLS", []):
            by_name[tool.name] = tool
    return list(by_name.values())


async def run_server(client: ColloquiumClient) -> None:
    server = Server("cqds-mcp")
    index_queue: asyncio.Queue[int] = asyncio.Queue()
    index_jobs: dict[int, dict[str, object]] = {}
    index_worker_task: asyncio.Task[None] | None = None
    host_proc_registry: dict[str, object] = {}

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
        host_proc_registry=host_proc_registry,
        index_queue=index_queue,
        index_jobs=index_jobs,
        ensure_index_worker=ensure_index_worker,
        queue_status=queue_status,
    )

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return cq_filter_tools_for_list(_registered_tools())

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, object]) -> CallToolResult:
        tool_token = CURRENT_TOOL.set(name)
        started_at = time.monotonic()
        LOGGER.info("TOOL call start name=%s args=[%s]", name, _summarize_arguments(arguments))
        try:
            if cq_tool_is_hidden(name):
                return _text(
                    f"Tool '{name}' is excluded for this configuration (CQ_HIDE_TOOLS)."
                )
            for module in MODULE_HANDLERS:
                delegated = await module.handle(name, arguments, run_ctx)
                if delegated is not None:
                    return delegated
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
        await server.run(read_stream, write_stream, server.create_initialization_options())


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
        epilog=textwrap.dedent(
            """\
            Environment variables (override CLI defaults):
              COLLOQUIUM_URL       Base URL of Colloquium-DevSpace  (default: http://localhost:8008)
              COLLOQUIUM_USERNAME  Login username                   (default: copilot)
              COLLOQUIUM_PASSWORD  Login password                   (higher priority than file/env file)
              COLLOQUIUM_PASSWORD_FILE  Path to file containing only the password
              CQ_HIDE_TOOLS        Comma-separated tool names to hide from list_tools / block calls
        """
        ),
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("COLLOQUIUM_URL", "http://localhost:8008"),
        help="Base URL of Colloquium-DevSpace (default: http://localhost:8008)",
    )
    parser.add_argument(
        "--username",
        default=os.environ.get("COLLOQUIUM_USERNAME", "copilot"),
        help="Username for Colloquium login (default: copilot)",
    )
    parser.add_argument(
        "--password",
        default=None,
        help="Password for Colloquium login (overrides password file and env file)",
    )
    parser.add_argument(
        "--password-file",
        default=None,
        help="Path to a file containing only the Colloquium password",
    )
    parser.add_argument(
        "--chat-id",
        type=int,
        default=int(os.environ.get("COLLOQUIUM_CHAT_ID", "0") or "0"),
        help="Default chat ID (informational; individual tools accept chat_id)",
    )
    args = parser.parse_args()

    print_ident_stderr("full")
    log_file = _setup_logging()
    LOGGER.info("MCP tool start url=%s username=%s pid=%s", args.url, args.username, os.getpid())
    if cq_hide_tool_names():
        LOGGER.info("CQ_HIDE_TOOLS active: %s", sorted(cq_hide_tool_names()))

    password, password_source = cq_cred.resolve_password(args.password, args.password_file)
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

    client = ColloquiumClient(base_url=args.url, username=args.username, password=password)
    try:
        asyncio.run(run_server(client))
    finally:
        asyncio.run(client.aclose())


if __name__ == "__main__":
    main()
