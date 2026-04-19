"""Лаконичный MCP-интерфейс ко всему инструментарию CQDS (stdio, Colloquium HTTP API).

Расположение: корень ``mcp-tools/`` (рядом с ``cqds_mcp_full.py``). Модули инструментов —
в ``mcp-tools/runtime/`` (``cq_runtime_*``).

Бывшие имена: ``copilot_mcp_runtime.py``, затем ``runtime/cqds_mcp_mini.py``.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

# Импорты из корня mcp-tools (cqds_*) и из runtime/ (cq_runtime_*).
_MCP_TOOLS_DIR = Path(__file__).resolve().parent
_RUNTIME_DIR = _MCP_TOOLS_DIR / "runtime"
for _p in (_MCP_TOOLS_DIR, _RUNTIME_DIR):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

# Guard: warn if this running MCP process is stale vs filesystem changes
# in any imported `.py` under `mcp-tools/`.
from lib.version_guard import VersionGuard

_MCP_OBSOLETE_RESTART_WARN = "WARN: obsolete MCP server was used, restart for check new version."
_VERSION_GUARD = VersionGuard(
    base_dir=_MCP_TOOLS_DIR,
    message=_MCP_OBSOLETE_RESTART_WARN,
    track_new_modules=True,
    check_interval_sec=1.0,
)

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
import cq_runtime_host_heartbeat
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


def _attach_staleness_warning(result: CallToolResult) -> CallToolResult:
    warn = _VERSION_GUARD.get_warning()
    if not warn:
        return result
    try:
        content = getattr(result, "content", None)
        if isinstance(content, list) and content:
            first = content[0]
            txt = getattr(first, "text", None)
            if isinstance(txt, str):
                stripped = txt.strip()
                parsed: Any = None
                if stripped.startswith("{") or stripped.startswith("["):
                    try:
                        parsed = json.loads(txt)
                    except Exception:
                        parsed = None
                if isinstance(parsed, dict):
                    existing = parsed.get("warnings")
                    if not isinstance(existing, list):
                        existing = []
                    if warn not in existing:
                        existing.append(warn)
                    parsed["warnings"] = existing
                    new_txt = json.dumps(parsed, ensure_ascii=False, indent=2)
                else:
                    new_txt = warn + "\n" + txt
                return CallToolResult(
                    isError=bool(getattr(result, "isError", False)),
                    # Recreate the same MCP content type without importing it explicitly.
                    content=[type(first)(type="text", text=new_txt)],
                )
    except Exception:
        pass
    return result


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
    server = Server("cqds-mcp-mini")
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
                try:
                    wcap = float(os.environ.get("CQDS_MCP_INDEX_WORKER_HTTP_MAX_SEC", "120"))
                except ValueError:
                    wcap = 120.0
                wcap = max(30.0, wcap)
                payload = await client.get_code_index(
                    project_id,
                    timeout=min(300, int(wcap)),
                    client_http_max_sec=wcap,
                )
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
                return _attach_staleness_warning(
                    _text(
                        f"Tool '{name}' is excluded for this configuration (CQ_HIDE_TOOLS)."
                    )
                )
            for mod in HELP_MODULES:
                delegated = await mod.handle(name, arguments, run_ctx)
                if delegated is not None:
                    return _attach_staleness_warning(delegated)
            for mod in CTX_MODULES:
                delegated = await mod.handle(name, arguments, run_ctx)
                if delegated is not None:
                    return _attach_staleness_warning(delegated)
            return _attach_staleness_warning(_text(f"Unknown tool: {name}"))
        except Exception as exc:
            http_msg = _http_status_error_payload(exc)
            if http_msg is not None:
                LOGGER.exception("RUNTIME TOOL http error name=%s", name)
                return _attach_staleness_warning(_text(http_msg))
            LOGGER.exception("RUNTIME TOOL call error name=%s", name)
            return _attach_staleness_warning(_text(f"Error: {exc}"))
        finally:
            CURRENT_TOOL.reset(token)

    async with stdio_server() as (read_stream, write_stream):
        hb_task: asyncio.Task[None] | None = None
        if cq_runtime_host_heartbeat.should_run_heartbeat_loop():
            hb_task = asyncio.create_task(
                cq_runtime_host_heartbeat.host_project_heartbeat_loop(LOGGER),
                name="cq-host-project-heartbeat",
            )
        try:
            await server.run(read_stream, write_stream, server.create_initialization_options())
        finally:
            if hb_task is not None:
                hb_task.cancel()
                try:
                    await hb_task
                except asyncio.CancelledError:
                    pass


def main() -> None:
    parser = argparse.ArgumentParser(description="CQDS MCP mini — компактный доступ к инструментарию CQDS")
    parser.add_argument("--url", default=os.environ.get("COLLOQUIUM_URL", "http://localhost:8008"))
    parser.add_argument("--username", default=os.environ.get("COLLOQUIUM_USERNAME", "copilot"))
    parser.add_argument("--password", default=os.environ.get("COLLOQUIUM_PASSWORD", "devspace"))
    args = parser.parse_args()

    print_ident_stderr("mini")
    os.environ.setdefault("COLLOQUIUM_MCP_LOG_STEM", "cqds_mcp_mini")
    _setup_logging()
    LOGGER.info(
        "CQDS MCP mini pid=%s url=%s user=%s",
        os.getpid(),
        args.url,
        args.username,
    )
    if cq_hide_tool_names():
        LOGGER.info("CQ_HIDE_TOOLS active: %s", sorted(cq_hide_tool_names()))
    if cq_runtime_host_heartbeat.heartbeat_enabled():
        if not shutil.which("docker") and not cq_runtime_host_heartbeat.fallback_projects_parent_configured():
            LOGGER.warning(
                "CQDS_MCP_PROJECT_HEARTBEAT is on but docker CLI not found and CQDS_MCP_HEARTBEAT_PROJECTS_DIR unset; "
                "host heartbeat will not write .cqds_mcp_active.pid (see cq_runtime_host_heartbeat)"
            )
    client = ColloquiumClient(base_url=args.url, username=args.username, password=args.password)
    try:
        asyncio.run(run_server(client))
    finally:
        asyncio.run(client.aclose())


if __name__ == "__main__":
    main()
