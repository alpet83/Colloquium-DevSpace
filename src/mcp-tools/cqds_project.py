from __future__ import annotations

import base64
import re
from typing import Any

from mcp.types import CallToolResult, Tool  # type: ignore[import]

from cqds_client import (
    _DEFAULT_MCP_SERVER_URL,
    _PROJECT_MCP_URL_CACHE,
    _apply_mcp_host_remap,
    _cache_project_mcp_urls,
    set_active_project_id,
)
from cqds_helpers import _json_text, _text
from cqds_host_grep_jobs import host_grep_poll_hint_sec, take_host_grep_snapshot
from cqds_result_pages import (
    DEFAULT_PAGE_SIZE,
    extra_result_page,
    finalize_smart_grep_response,
    get_page_store,
)
from cqds_run_ctx import RunContext


TOOLS: list[Tool] = [
    Tool(
        name="cq_fetch_result",
        description=(
            "Три режима (ровно один на вызов): "
            "(1) Пейджинг MCP — paging.handle из cq_start_grep; page_index 0 — первая страница из кэша. "
            "(2) Следующий stateless-чанк — chunk_continuation из cq_start_grep/cq_fetch_result (пока scan_complete=false). "
            "(3) host_fs async — host_grep_job_id из cq_start_grep с host_async=true; периодически опрашивать (hint host_grep_poll_hint_sec). "
            "TTL page-store: после scan_complete — ~30 мин; истёкший job → unknown_or_expired_host_grep_job."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "handle": {
                    "type": "string",
                    "description": "Opaque handle из cq_start_grep → paging.handle (режим пейджинга по hits).",
                },
                "chunk_continuation": {
                    "type": "object",
                    "description": "Объект из поля chunk_continuation предыдущего ответа (следующий POST smart_grep/chunk).",
                },
                "page_index": {
                    "type": "integer",
                    "description": "0-based page index для handle (0 = первая страница как в inline hits).",
                    "default": 1,
                },
                "page_size": {
                    "type": "integer",
                    "description": "Hits per page для handle (1..500).",
                    "default": DEFAULT_PAGE_SIZE,
                },
                "max_returned_items": {
                    "type": "integer",
                    "description": "Для chunk_continuation и host_grep_job_id: сколько hits в ответе (1..500), остальное в paging.",
                    "default": DEFAULT_PAGE_SIZE,
                },
                "host_grep_job_id": {
                    "type": "string",
                    "description": "ID фонового host_fs-поиска (cq_start_grep с host_async=true).",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="cq_list_projects",
        description=(
            "List all projects registered in Colloquium-DevSpace with id and metadata. "
            "Typical first step before cq_select_project, cq_exec, cq_start_grep, or cq_list_files."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
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
                }
            },
            "required": ["project_id"],
        },
    ),
    Tool(
        name="cq_query_db",
        description=(
            "Execute SQL query through Colloquium backend DB layer and return rows as JSON. "
            "By default only read-only SQL is allowed (SELECT/EXPLAIN/WITH). "
            "Mutating SQL can be enabled only with allow_write=true and only for local/private endpoints."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "integer",
                    "description": "Project ID (from cq_list_projects).",
                },
                "query": {
                    "type": "string",
                    "description": "SQL query string.",
                },
                "allow_write": {
                    "type": "boolean",
                    "description": "Allow mutating SQL (INSERT/UPDATE/DELETE/ALTER/etc). Works only for local/private Colloquium endpoints.",
                    "default": False,
                },
                "timeout": {
                    "type": "integer",
                    "description": "Max execution time in seconds (1-300, default 30).",
                    "default": 30,
                },
            },
            "required": ["project_id", "query"],
        },
    ),
    Tool(
        name="cq_set_sync_mode",
        description=(
            "Enable or disable synchronous mode for cq_send_message. "
            "When enabled (timeout > 0), cq_send_message automatically waits for the AI reply "
            "up to 'timeout' seconds — eliminating the need for a separate cq_wait_reply call. "
            "Set timeout=0 to disable (default). Recommended: timeout=60 for typical LLM responses."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "timeout": {
                    "type": "integer",
                    "description": "Seconds to wait for reply after send (0 = off, max 300).",
                    "default": 0,
                }
            },
            "required": ["timeout"],
        },
    ),
    Tool(
        name="cq_project_status",
        description=(
            "Get health status and diagnostics for a project.\n"
            "Returns: status (ok/info/warning/error), problems[] with severity codes,\n"
            "file link counts (total/active), backup/undo stack info (count, size_bytes, oldest_ts, newest_ts),\n"
            "scan state and index cache state.\n"
            "Use this to quickly check if a project has stale file links, a failing scan,\n"
            "or a missing index cache. 'problems' drives the frontend warning indicator."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "integer",
                    "description": "Project ID (use cq_list_projects to get IDs).",
                }
            },
            "required": ["project_id"],
        },
    ),
]


async def handle(name: str, arguments: dict[str, Any], ctx: RunContext) -> CallToolResult | None:
    client = ctx.client

    if name == "cq_fetch_result":
        chunk_cont = arguments.get("chunk_continuation")
        if chunk_cont is not None:
            if not isinstance(chunk_cont, dict):
                raise ValueError("chunk_continuation must be an object")
            max_returned = max(1, min(int(arguments.get("max_returned_items", DEFAULT_PAGE_SIZE)), 500))
            p_req = dict(chunk_cont)
            chunk = await client.smart_grep_chunk_stable(p_req)
            search_mode_label = str(chunk_cont.get("search_mode", "project_registered"))
            next_cont: dict[str, Any] | None = None
            if not bool(chunk.get("scan_complete")):
                next_cont = {
                    **dict(chunk_cont),
                    "index_epoch": int(chunk.get("index_epoch", p_req.get("index_epoch", 0))),
                    "offset": int(chunk.get("next_offset", 0)),
                    "search_mode": "project_registered",
                }
            for_finalize = {**chunk, "search_mode": search_mode_label}
            out = await finalize_smart_grep_response(
                for_finalize,
                page_size=max_returned,
                store=get_page_store(),
                source_tool="cq_start_grep",
                scan_complete=bool(chunk.get("scan_complete")),
            )
            if next_cont is not None:
                out["chunk_continuation"] = next_cont
            return _json_text(out)

        host_jid = str(arguments.get("host_grep_job_id") or "").strip()
        if host_jid:
            max_returned = max(1, min(int(arguments.get("max_returned_items", DEFAULT_PAGE_SIZE)), 500))
            snap = await take_host_grep_snapshot(host_jid)
            if snap is None:
                return _json_text(
                    {
                        "status": "error",
                        "error": "unknown_or_expired_host_grep_job",
                        "host_grep_job_id": host_jid,
                    }
                )
            scan_done = bool(snap["scan_complete"])
            for_finalize: dict[str, Any] = {
                "status": "ok",
                "search_mode": "host_fs",
                "engine": snap["engine"],
                "host_path": snap["host_path"],
                "mode": snap["mode"],
                "profile": snap["profile"],
                "query": snap["query"],
                "is_regex": snap["is_regex"],
                "case_sensitive": snap["case_sensitive"],
                "hits": snap["hits"],
                "total": len(snap["hits"]),
                "truncated": snap["truncated"],
                "scan_complete": scan_done,
                "host_grep_job_id": snap["job_id"],
                "host_grep_snapshot_seq": snap["snapshot_seq"],
                "host_grep_poll_hint_sec": host_grep_poll_hint_sec(),
            }
            if snap.get("error"):
                for_finalize["status"] = "error"
                for_finalize["host_grep_error"] = snap["error"]
            out = await finalize_smart_grep_response(
                for_finalize,
                page_size=max_returned,
                store=get_page_store(),
                source_tool="cq_start_grep",
                scan_complete=scan_done,
            )
            return _json_text(out)

        handle_id = str(arguments.get("handle") or "").strip()
        if not handle_id:
            raise ValueError(
                "cq_fetch_result: укажите ровно один из: handle | chunk_continuation | host_grep_job_id"
            )
        page_index = int(arguments.get("page_index", 1))
        page_size = int(arguments.get("page_size", DEFAULT_PAGE_SIZE))
        data = await extra_result_page(handle_id, page_index, page_size, get_page_store())
        return _json_text(data)

    if name == "cq_list_projects":
        projects = await client.list_projects()
        _cache_project_mcp_urls(projects)
        return _json_text(projects)

    if name == "cq_select_project":
        project_id = int(arguments["project_id"])
        result = await client.select_project(project_id)
        set_active_project_id(project_id)
        if project_id not in _PROJECT_MCP_URL_CACHE:
            projects_list = await client.list_projects()
            _cache_project_mcp_urls(projects_list)
        mcp_url = _apply_mcp_host_remap(
            _PROJECT_MCP_URL_CACHE.get(project_id, _DEFAULT_MCP_SERVER_URL)
        )
        return _text(f"Project {project_id} selected: {result}\nmcp_server_url: {mcp_url}")

    if name == "cq_query_db":
        project_id = int(arguments["project_id"])
        query = str(arguments["query"] or "").strip()
        allow_write = bool(arguments.get("allow_write", False))
        timeout = int(arguments.get("timeout", 30))
        if not query:
            raise ValueError("query must be non-empty")

        ql = query.lower().lstrip()
        if not allow_write:
            if not (ql.startswith("select") or ql.startswith("with") or ql.startswith("explain")):
                raise ValueError(
                    "Only read-only SQL is allowed (SELECT/WITH/EXPLAIN). Set allow_write=true for local/private endpoints."
                )
            if re.search(r"\b(insert|update|delete|drop|alter|truncate|create|grant|revoke|comment)\b", ql):
                raise ValueError(
                    "Mutating SQL keywords are not allowed in cq_query_db without allow_write=true"
                )
        elif not client.is_local_or_private_endpoint():
            raise ValueError(
                "allow_write=true is permitted only for local/private Colloquium endpoints"
            )

        encoded = base64.b64encode(query.encode("utf-8")).decode("ascii")
        command = (
            "PYTHONPATH=/app/agent /app/venv/bin/python - <<'PY'\n"
            "import base64, json\n"
            "from managers.db import Database\n"
            f"q = base64.b64decode('{encoded}').decode('utf-8')\n"
            "db = Database.get_database()\n"
            "rows = db.fetch_all(q)\n"
            "print(json.dumps({'status': 'success', 'rows': [list(r) for r in rows]}, ensure_ascii=False))\n"
            "PY"
        )
        result = await client.exec_command(project_id, command, timeout)
        return _json_text(result)

    if name == "cq_set_sync_mode":
        timeout = max(0, min(int(arguments.get("timeout", 0)), 300))
        client._sync_timeout = timeout
        if timeout > 0:
            return _text(f"Sync mode ON: cq_send_message will wait up to {timeout}s for AI reply.")
        return _text("Sync mode OFF: cq_send_message returns immediately.")

    if name == "cq_project_status":
        project_id = int(arguments["project_id"])
        status = await client.get_project_status(project_id)
        return _json_text(status)

    return None