from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
import re
import time
from datetime import datetime
from typing import Any

import httpx  # type: ignore[import]
from mcp.types import CallToolResult, Tool  # type: ignore[import]

from cqds_host_grep_jobs import host_grep_poll_hint_sec, start_host_grep_job
from cqds_smart_grep_host import smart_grep_host_fs

from cqds_helpers import (
    LOGGER,
    _build_file_tree_from_index,
    _file_id_to_name_map,
    _index_counts,
    _index_file_rows,
    _json_text,
    _parse_entity_csv_row,
    _text,
    _xml_code_file,
    _xml_patch,
    _xml_undo,
)
from cqds_run_ctx import RunContext
from cqds_result_pages import DEFAULT_SCAN_HIT_CAP, finalize_smart_grep_response, get_page_store


def _mcp_index_background_via_maint_pool() -> bool:
    v = (os.environ.get("CQDS_MCP_INDEX_BACKGROUND_VIA_MAINT") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _mcp_sync_code_index_http_max_sec() -> float:
    try:
        return max(5.0, float(os.environ.get("CQDS_MCP_SYNC_CODE_INDEX_MAX_SEC", "30")))
    except ValueError:
        return 30.0


TOOLS: list[Tool] = [
    Tool(
        name="cq_edit_file",
        description=(
            "Ask Colloquium to write (create or overwrite) a file inside the active project. "
            "Sends a <code_file> XML block as a chat message (requires chat_id). "
            "For mechanical edits without Colloquium chat messages, prefer cq_replace when a full-file rewrite is not needed."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "chat_id": {"type": "integer", "description": "Chat ID to post to."},
                "path": {"type": "string", "description": "File path relative to project root."},
                "content": {"type": "string", "description": "Full file content to write."},
            },
            "required": ["chat_id", "path", "content"],
        },
    ),
    Tool(
        name="cq_patch_file",
        description=(
            "Ask Colloquium to apply a unified-diff patch to a project file. "
            "Sends a <patch> XML block as a chat message (requires chat_id). "
            "For mechanical edits without chat, consider cq_replace (by file_id) when a simple replace suffices."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "chat_id": {"type": "integer", "description": "Chat ID to post to."},
                "path": {"type": "string", "description": "File path relative to project root."},
                "diff": {"type": "string", "description": "Unified diff to apply."},
            },
            "required": ["chat_id", "path", "diff"],
        },
    ),
    Tool(
        name="cq_undo_file",
        description=(
            "Ask Colloquium to restore a previous version of a file. "
            "Sends an <undo> XML block as a chat message."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "chat_id": {"type": "integer", "description": "Chat ID to post to."},
                "file_id": {"type": "integer", "description": "Colloquium file ID to restore."},
                "time_back": {
                    "type": "integer",
                    "description": "Seconds to look back for the backup (default 3600).",
                    "default": 3600,
                },
            },
            "required": ["chat_id", "file_id"],
        },
    ),
    Tool(
        name="cq_list_files",
        description=(
            "Return a lightweight file index for a project (id, file_name, ts, size_bytes). "
            "No file content is transferred. Three filters are supported and can be combined:\n"
            "  • all files — omit modified_since and file_ids\n"
            "  • recently modified — set modified_since to a Unix timestamp (files with ts ≥ value)\n"
            "  • specific files — set file_ids as comma-separated DB IDs, e.g. '42,57,103'\n"
            "The 'id' field is the DB file_id required by cq_patch_file and cq_undo_file.\n"
            "NOTE: these IDs are NOT the same as sandwich-pack index numbers.\n"
            "Set as_tree=true to get JSON tree (kind dir/file, path, children) built from file_name paths; "
            "optional include_flat=true adds the flat list alongside the tree."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "integer", "description": "Project ID (use cq_list_projects to get IDs)."},
                "modified_since": {"type": "integer", "description": "Optional Unix timestamp. Only return files with ts >= this value."},
                "file_ids": {"type": "string", "description": "Optional comma-separated DB file IDs to fetch, e.g. '42,57,103'."},
                "include_size": {"type": "boolean", "description": "Set to true to include size_bytes. Slower (~1s for 177 files on Docker FS). Default false."},
                "as_tree": {"type": "boolean", "description": "If true, wrap response as {tree, file_count} with nested dirs/files from path segments.", "default": False},
                "include_flat": {"type": "boolean", "description": "When as_tree is true, also include the flat files array in the response.", "default": False},
            },
            "required": ["project_id"],
        },
    ),
    Tool(
        name="cq_get_index",
        description=(
            "Return the rich entity index built from the last LLM context assembly for a chat, "
            "or read the cached project index from /app/projects/.cache when project_id is provided. "
            "Includes all parsed functions, classes, methods and variables with their file_id, "
            "line ranges and token counts. Useful for code navigation and understanding project structure.\n"
            "Format: sandwiches_index.jsl — 'entities' is a list of CSV strings, layout described in 'templates.entities':\n"
            "  vis,type,parent,name,file_id,start_line-end_line,tokens\n"
            "  e.g. 'pub,function,,fetchData,3,45-67,120'\n"
            "'filelist' maps file IDs to file names (same format as cq_list_files).\n"
            "Provide either chat_id or project_id. For project_id, returns the cached project index if it exists."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "chat_id": {"type": "integer", "description": "Chat ID whose index to retrieve (use cq_list_chats to get IDs)."},
                "project_id": {"type": "integer", "description": "Project ID whose cached index to read from /app/projects/.cache."},
            },
            "required": [],
        },
    ),
    Tool(
        name="cq_rebuild_index",
        description=(
            "Build the rich entity index for a project on demand — no prior LLM interaction needed.\n"
            "Runs context assembly (loads all project files → SandwichPack.pack) and returns\n"
            "the full sandwiches_index.jsl format JSON with 'entities' and 'filelist'.\n"
            "When background=true, MCP tool queues or reports a background build and stores the result in /app/projects/.cache/{project_name}_index.jsl.\n"
            "When cache_only=true, no full rebuild: GET /project/code_index?cache_only=true — try-retrieve session result, else file cache; may include rebuilt_now:1 while maint code_index is active.\n"
            "По умолчанию фон = maint_enqueue на ядре — опрос cq_help#core_status (maint_pool.active_jobs). Fallback = локальная очередь MCP; тогда опрос cq_files_ctl#index_job_status. CQDS_MCP_INDEX_BACKGROUND_VIA_MAINT=0 отключает maint-путь.\n"
            "Use this to understand project structure, find functions/classes, or plan edits.\n"
            "'entities' is a list of CSV strings: vis,type,parent,name,file_id,start-end,tokens\n"
            "  e.g. 'pub,function,,fetchData,3,45-67,120'\n"
            "'filelist' maps file_id to file_name/md5/tokens/timestamp."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "integer", "description": "Project ID (use cq_list_projects to get IDs)."},
                "background": {"type": "boolean", "description": "If true, queue/report a background build and save cache to /app/projects/.cache/{project_name}_index.jsl.", "default": False},
                "cache_only": {
                    "type": "boolean",
                    "description": "If true, no scan/full build: backend returns cached index and/or rebuilt_now (see GET /project/code_index?cache_only=true). Ignores background.",
                    "default": False,
                },
                "timeout": {"type": "integer", "description": "Max seconds to wait for the index build (default: 300). Passed to the backend as a hint and used as the HTTP client timeout.", "default": 300},
            },
            "required": ["project_id"],
        },
    ),
    Tool(
        name="cq_get_code_index",
        description=(
            "DEPRECATED alias for cq_rebuild_index. "
            "Builds the rich entity index for a project on demand and returns sandwiches_index.jsl JSON."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "integer", "description": "Project ID (use cq_list_projects to get IDs)."},
                "background": {"type": "boolean", "description": "If true, queue/report a background build and save cache to /app/projects/.cache/{project_name}_index.jsl.", "default": False},
                "cache_only": {
                    "type": "boolean",
                    "description": "If true, no scan/full build: backend returns cached index and/or rebuilt_now (see GET /project/code_index?cache_only=true). Ignores background.",
                    "default": False,
                },
                "timeout": {"type": "integer", "description": "Max seconds to wait for the index build (default: 300). Passed to the backend as a hint and used as the HTTP client timeout.", "default": 300},
            },
            "required": ["project_id"],
        },
    ),
    Tool(
        name="cq_grep_entity",
        description=(
            "Search parsed definition entries in the project code index (sandwiches_index entities). "
            "The index lists declaration sites (function, class, method, variable, …) — not call sites. "
            "Supply one or more regex patterns; a row matches if any pattern matches the chosen field. "
            "Uses cached project index from cq_get_index unless ensure_index triggers cq_rebuild_index. "
            "Limitation: entity CSV rows must split cleanly on commas (names/parents with commas are not supported)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "integer", "description": "Project ID (use cq_list_projects)."},
                "patterns": {"type": "array", "items": {"type": "string"}, "description": "Regex patterns (OR). Alternatively pass a single string via 'pattern'."},
                "pattern": {"type": "string", "description": "Single regex pattern (convenience if only one). Ignored if patterns is non-empty."},
                "match_field": {"type": "string", "description": "Which field to match: name | parent | qualified (parent::name, or name if no parent).", "default": "name"},
                "entity_types": {"type": "array", "items": {"type": "string"}, "description": "Optional whitelist of entity type strings (e.g. function, class, method, interface, trait, enum). Omit for all types."},
                "is_regex": {"type": "boolean", "description": "If false, patterns are literal substrings (escaped).", "default": True},
                "case_sensitive": {"type": "boolean", "description": "Regex case sensitivity (default false).", "default": False},
                "max_results": {"type": "integer", "description": "Cap on returned matches (1..500, default 100).", "default": 100},
                "ensure_index": {"type": "boolean", "description": "If true and cached index has no entities, run cq_rebuild_index-equivalent sync build.", "default": False},
                "ensure_index_timeout": {"type": "integer", "description": "Seconds for ensure_index build (30..300, default 120).", "default": 120},
            },
            "required": ["project_id"],
        },
    ),
    Tool(
        name="cq_read_file",
        description=(
            "Read the contents of a project file directly by its DB file_id. "
            "Returns raw text (or formatted JSON for .json files). "
            "Use cq_list_files or cq_rebuild_index to look up file_ids. "
            "Direct HTTP call — no LLM or chat round-trip required."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "file_id": {"type": "integer", "description": "DB file_id from cq_list_files or cq_rebuild_index filelist."}
            },
            "required": ["file_id"],
        },
    ),
    Tool(
        name="cq_start_grep",
        description=(
            "Текстовый / regex-поиск с выбором режима на запрос.\n"
            "• search_mode=host_fs — каталог на машине MCP (host_path), без HTTP к Colloquium; "
            "если в PATH есть ripgrep (rg), используется он, иначе многопоточный обход файлов на Python.\n"
            "  host_async=true (только host_fs): фоновая задача читает stdout rg с тиками (см. CQDS_HOST_GREP_POLL_SEC, по умолчанию 5 с); "
            "cq_fetch_result с host_grep_job_id возвращает накопленные hits до scan_complete (аналогично polling cq_host_process_io, но с разбором JSON rg).\n"
            "• search_mode=project_registered / project_refresh — первый шаг через POST /api/project/smart_grep/chunk "
            "(stateless): в ответе поле chunk_continuation для следующего чанка → инструмент cq_fetch_result.\n"
            "  project_refresh допускается только с offset=0 (скан в ядре один раз).\n"
            "Пресеты mode / profile / include_glob как у серверного smart_grep; на host_fs фильтрация на хосте.\n"
            "Много попаданий в одном чанке: при hits > max_returned_items первая страница в ответе, хвост — "
            "cq_fetch_result с paging.handle. max_results — верхняя граница max_hits в чанке и лимит host_fs (scan_hit_cap)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "search_mode": {
                    "type": "string",
                    "enum": ["host_fs", "project_registered", "project_refresh"],
                    "description": "host_fs | project_registered | project_refresh",
                    "default": "project_registered",
                },
                "host_path": {
                    "type": "string",
                    "description": "Абсолютный или пользовательский путь к папке на хосте MCP; обязателен для search_mode=host_fs.",
                },
                "host_timeout_sec": {
                    "type": "integer",
                    "description": "Таймаут ripgrep для host_fs (5..600, по умолчанию 120).",
                    "default": 120,
                },
                "host_workers": {
                    "type": "integer",
                    "description": "Число потоков для Python-fallback на host_fs (1..32, по умолчанию 8).",
                    "default": 8,
                },
                "host_async": {
                    "type": "boolean",
                    "description": "Только host_fs: не блокировать MCP — запустить rg в фоне; опрос через cq_fetch_result + host_grep_job_id.",
                    "default": False,
                },
                "project_id": {
                    "type": "integer",
                    "description": "ID проекта CQDS; обязателен для project_registered и project_refresh.",
                },
                "path_prefix": {
                    "type": "string",
                    "description": "Подкаталог проекта (POSIX, без ведущего /), пусто — весь проект; только API-режимы.",
                    "default": "",
                },
                "offset": {
                    "type": "integer",
                    "description": "Смещение в списке file_id для чанка (обычно 0; продолжение — через cq_fetch_result).",
                    "default": 0,
                },
                "limit_files": {
                    "type": "integer",
                    "description": "Сколько файлов обработать за один чанок (сервер ограничивает верх).",
                    "default": 50,
                },
                "query": {"type": "string", "description": "Строка или regex (если is_regex=true)."},
                "mode": {"type": "string", "description": "Набор файлов: code | logs | docs | all (default: code).", "default": "code"},
                "profile": {"type": "string", "description": "Профиль путей: all | backend | frontend | docs | infra | tests | logs (default: all).", "default": "all"},
                "time_strict": {"type": "string", "description": "Фильтр по времени (только API-режимы), напр. mtime>2026-03-25."},
                "is_regex": {"type": "boolean", "description": "Интерпретировать query как regex.", "default": False},
                "case_sensitive": {"type": "boolean", "description": "Учёт регистра.", "default": False},
                "max_results": {
                    "type": "integer",
                    "description": "max_hits в одном чанке / лимит host_fs; не больше scan_hit_cap (1..10000).",
                    "default": 100,
                },
                "scan_hit_cap": {
                    "type": "integer",
                    "description": "Жёсткий потолок совпадений для одного запроса (1..10000, по умолчанию 10000).",
                    "default": 10000,
                },
                "max_returned_items": {
                    "type": "integer",
                    "description": "Сколько попаданий отдать в ответе MCP; при превышении — кэш и cq_fetch_result (1..500).",
                    "default": 100,
                },
                "context_lines": {"type": "integer", "description": "Строк контекста до/после (0..3).", "default": 0},
                "include_glob": {"type": "array", "items": {"type": "string"}, "description": "Доп. glob путей, напр. ['src/**/*.py']."},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="cq_grep_logs",
        description=(
            "Scan one or more log files inside the selected project container context using regex filtering. "
            "Accepts file masks (glob array) and/or docker service pseudo-masks like 'docker:colloquium-core', "
            "returns JSON map: {source: [matched lines]}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "integer", "description": "Project ID (from cq_list_projects)."},
                "query": {"type": "string", "description": "Regex pattern for line filtering."},
                "log_masks": {"type": "array", "items": {"type": "string"}, "description": "Sources to scan: file globs (e.g. ['logs/*.log']) and/or docker targets (e.g. ['docker:colloquium-core'])."},
                "tail_lines": {"type": "integer", "description": "Max matched lines per file to return from tail (default 100).", "default": 100},
                "since_seconds": {"type": "integer", "description": "Optional time window in seconds; when > 0, only lines from the last N seconds are considered.", "default": 0},
                "case_sensitive": {"type": "boolean", "description": "Case-sensitive regex matching (default false).", "default": False},
            },
            "required": ["project_id", "query"],
        },
    ),
    Tool(
        name="cq_replace",
        description=(
            "Replace text in one file directly by file_id, with optional regex mode. "
            "No chat/LLM round-trip (contrast: cq_edit_file/cq_patch_file post via chat messages). "
            "Use cq_list_files or index filelist to resolve file_id."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "integer", "description": "Project ID."},
                "file_id": {"type": "integer", "description": "DB file_id in the selected project."},
                "old": {"type": "string", "description": "Old text or regex pattern."},
                "new": {"type": "string", "description": "Replacement text."},
                "is_regex": {"type": "boolean", "description": "Interpret old as regex.", "default": False},
                "case_sensitive": {"type": "boolean", "description": "Case-sensitive matching.", "default": True},
                "max_replacements": {"type": "integer", "description": "Limit number of replacements (0 = all).", "default": 0},
            },
            "required": ["project_id", "file_id", "old", "new"],
        },
    ),
]


async def handle(name: str, arguments: dict[str, Any], ctx: RunContext) -> CallToolResult | None:
    client = ctx.client

    if name == "cq_edit_file":
        chat_id = int(arguments["chat_id"])
        path = str(arguments["path"])
        content = str(arguments["content"])
        xml = _xml_code_file(path, content)
        await client.post_message(chat_id, xml)
        return _text(f"<code_file> sent for '{path}' to chat_id={chat_id}")

    if name == "cq_patch_file":
        chat_id = int(arguments["chat_id"])
        path = str(arguments["path"])
        diff = str(arguments["diff"])
        xml = _xml_patch(path, diff)
        await client.post_message(chat_id, xml)
        return _text(f"<patch> sent for '{path}' to chat_id={chat_id}")

    if name == "cq_undo_file":
        chat_id = int(arguments["chat_id"])
        file_id = int(arguments["file_id"])
        time_back = int(arguments.get("time_back", 3600))
        xml = _xml_undo(file_id, time_back)
        await client.post_message(chat_id, xml)
        return _text(f"<undo> sent for file_id={file_id} to chat_id={chat_id}")

    if name == "cq_list_files":
        project_id = int(arguments["project_id"])
        modified_since = arguments.get("modified_since")
        file_ids_raw = arguments.get("file_ids")
        include_size = bool(arguments.get("include_size", False))
        as_tree = bool(arguments.get("as_tree", False))
        include_flat = bool(arguments.get("include_flat", False))
        modified_since = int(modified_since) if modified_since is not None else None
        file_ids = [int(x.strip()) for x in file_ids_raw.split(",")] if file_ids_raw else None
        files = await client.list_files(project_id, modified_since, file_ids, include_size)
        if not as_tree:
            return _json_text(files)
        tree = _build_file_tree_from_index(files)
        payload: dict[str, Any] = {"tree": tree, "file_count": len(files)}
        if include_flat:
            payload["files"] = files
        return _json_text(payload)

    if name == "cq_get_index":
        chat_id_raw = arguments.get("chat_id")
        project_id_raw = arguments.get("project_id")
        if chat_id_raw is None and project_id_raw is None:
            raise ValueError("cq_get_index requires chat_id or project_id")
        chat_id = int(chat_id_raw) if chat_id_raw is not None else None
        project_id = int(project_id_raw) if project_id_raw is not None else None
        index = await client.get_index(chat_id=chat_id, project_id=project_id)
        return _json_text(index)

    if name == "cq_index_job_status":
        """Только снимок очереди code_index в процессе MCP — без постановки в очередь и без лишнего HTTP."""
        if ctx.queue_status is None:
            raise RuntimeError("index job tracking is not configured in this MCP process")
        qsz = int(ctx.index_queue.qsize()) if ctx.index_queue is not None else 0
        raw_pid = arguments.get("project_id")
        if raw_pid is not None:
            project_id = int(raw_pid)
            return _json_text(
                {
                    "job_kind": "mcp_code_index",
                    "note": "Фоновый GET /api/project/code_index в этом процессе cqds_mcp_mini (не maint_pool ядра).",
                    "queue_size": qsz,
                    "projects": [ctx.queue_status(project_id)],
                }
            )
        rows: list[dict[str, Any]] = []
        for pid in sorted(ctx.index_jobs.keys()):
            rows.append(ctx.queue_status(int(pid)))
        return _json_text(
            {
                "job_kind": "mcp_code_index",
                "note": "Снимок всех известных проектов с состоянием code_index в этом процессе MCP.",
                "queue_size": qsz,
                "projects": rows,
            }
        )

    if name in {"cq_rebuild_index", "cq_get_code_index"}:
        project_id = int(arguments["project_id"])
        cache_only = bool(arguments.get("cache_only", False))
        background = bool(arguments.get("background", False))
        timeout = int(arguments.get("timeout", 300))

        if cache_only:
            if background:
                LOGGER.info(
                    "cq_rebuild_index: cache_only=true ignores background (project_id=%s)",
                    project_id,
                )
            cap_web = max(15.0, min(45.0, float(_mcp_sync_code_index_http_max_sec())))
            try:
                index = await client.get_code_index(
                    project_id,
                    timeout=min(int(timeout), 120),
                    client_http_max_sec=cap_web,
                    cache_only=True,
                )
            except httpx.HTTPStatusError as exc:
                if exc.response is not None and exc.response.status_code == 404:
                    detail = ""
                    try:
                        detail = (exc.response.text or "")[:1200]
                    except Exception:
                        pass
                    return _json_text(
                        {
                            "ok": False,
                            "code": "cache_only_miss",
                            "project_id": project_id,
                            "http_status": 404,
                            "error": str(exc),
                            "detail": detail,
                            "hint": (
                                "No cached index for cache_only path; run full rebuild_index or "
                                "background maint_enqueue first."
                            ),
                        }
                    )
                raise
            except httpx.TimeoutException as exc:
                return _json_text(
                    {
                        "ok": False,
                        "code": "cache_only_timeout",
                        "project_id": project_id,
                        "client_http_max_sec": cap_web,
                        "error": str(exc),
                    }
                )
            entities_count, files_count = _index_counts(index)
            ctx.index_jobs[project_id] = {
                "project_id": project_id,
                "status": "ready",
                "running": False,
                "queued": False,
                "started_at": None,
                "finished_at": int(time.time()),
                "error": None,
                "files": files_count,
                "entities": entities_count,
                "source": "cache_only",
            }
            return _json_text(index)

        if background:
            if _mcp_index_background_via_maint_pool():
                try:
                    out = await client.maint_enqueue(project_id, "code_index")
                    return _json_text(
                        {
                            **out,
                            "via": "maint_pool",
                            "poll": (
                                "cq_help#core_status → core.maint_pool.active_jobs "
                                "(kind=code_index, busy_sec, progress); локальная очередь MCP не используется."
                            ),
                        }
                    )
                except Exception as exc:
                    LOGGER.warning(
                        "maint_enqueue fallback to mcp local index queue project_id=%s: %s",
                        project_id,
                        str(exc),
                    )
            if ctx.ensure_index_worker is None or ctx.queue_status is None:
                raise RuntimeError("index worker callbacks are not configured")
            await ctx.ensure_index_worker()
            current = ctx.index_jobs.get(project_id)
            if current and current.get("status") in {"queued", "running"}:
                return _json_text(ctx.queue_status(project_id))

            try:
                cached = await client.get_index(project_id=project_id)
                entities_count, files_count = _index_counts(cached)
                ctx.index_jobs[project_id] = {
                    "project_id": project_id,
                    "status": "ready",
                    "running": False,
                    "queued": False,
                    "started_at": None,
                    "finished_at": int(time.time()),
                    "error": None,
                    "files": files_count,
                    "entities": entities_count,
                    "source": "cache",
                }
                return _json_text(ctx.queue_status(project_id))
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise

            ctx.index_jobs[project_id] = {
                "project_id": project_id,
                "status": "queued",
                "running": False,
                "queued": True,
                "started_at": None,
                "finished_at": None,
                "error": None,
                "files": None,
                "entities": None,
                "source": "mcp-queue",
            }
            await ctx.index_queue.put(project_id)
            return _json_text(ctx.queue_status(project_id))

        cap = _mcp_sync_code_index_http_max_sec()
        try:
            index = await client.get_code_index(
                project_id,
                timeout=min(int(timeout), int(cap)),
                client_http_max_sec=cap,
            )
        except httpx.TimeoutException as exc:
            return _json_text(
                {
                    "ok": False,
                    "code": "sync_code_index_client_timeout",
                    "task_in_progress": None,
                    "note": (
                        "Ядро могло продолжить работу после обрыва HTTP; стабильного id пока нет "
                        "(заготовка task_in_progress:${id} — при появлении async API ядра)."
                    ),
                    "client_http_max_sec": cap,
                    "project_id": project_id,
                    "error": str(exc),
                    "protocol": (
                        "Норма: не держать долгий синхронный MCP→ядро. "
                        "cq_files_ctl#rebuild_index с background:true (maint_enqueue), "
                        "затем cq_help#core_status → maint_pool.active_jobs."
                    ),
                }
            )
        entities_count, files_count = _index_counts(index)
        ctx.index_jobs[project_id] = {
            "project_id": project_id,
            "status": "ready",
            "running": False,
            "queued": False,
            "started_at": None,
            "finished_at": int(time.time()),
            "error": None,
            "files": files_count,
            "entities": entities_count,
            "source": "sync",
        }
        return _json_text(index)

    if name == "cq_grep_entity":
        project_id = int(arguments["project_id"])
        patterns_raw = arguments.get("patterns")
        if patterns_raw is None or patterns_raw == []:
            single = arguments.get("pattern")
            patterns_raw = [single] if single else []
        if isinstance(patterns_raw, str):
            patterns_list = [patterns_raw] if patterns_raw.strip() else []
        elif isinstance(patterns_raw, list):
            patterns_list = [str(p) for p in patterns_raw if str(p).strip()]
        else:
            raise ValueError("cq_grep_entity: patterns must be an array of strings or use pattern (string)")
        if not patterns_list:
            raise ValueError("cq_grep_entity: provide patterns (non-empty array) or pattern (string)")

        match_field = str(arguments.get("match_field", "name")).lower()
        if match_field not in {"name", "parent", "qualified"}:
            raise ValueError("match_field must be one of: name, parent, qualified")

        type_filter = arguments.get("entity_types")
        type_allow: set[str] | None = None
        if type_filter is not None:
            if not isinstance(type_filter, list):
                raise ValueError("entity_types must be an array of strings or omitted")
            type_allow = {str(t) for t in type_filter if str(t).strip()} or None

        is_regex = bool(arguments.get("is_regex", True))
        case_sensitive = bool(arguments.get("case_sensitive", False))
        max_results = max(1, min(int(arguments.get("max_results", 100)), 500))
        ensure_index = bool(arguments.get("ensure_index", False))
        ensure_timeout = max(30, min(int(arguments.get("ensure_index_timeout", 120)), 300))

        index_payload = await client.get_index(project_id=project_id)
        entities = index_payload.get("entities") if isinstance(index_payload, dict) else None
        if (not isinstance(entities, list) or len(entities) == 0) and ensure_index:
            index_payload = await client.get_code_index(project_id, timeout=ensure_timeout)
            entities = index_payload.get("entities") if isinstance(index_payload, dict) else None

        if not isinstance(entities, list) or len(entities) == 0:
            return _json_text(
                {
                    "matches": [],
                    "count": 0,
                    "truncated": False,
                    "hint": "No entities in index. Run cq_rebuild_index, or call again with ensure_index=true.",
                    "project_id": project_id,
                }
            )

        flags = 0 if case_sensitive else re.IGNORECASE
        compiled: list[re.Pattern[str]] = []
        for pat in patterns_list:
            try:
                compiled.append(re.compile(pat if is_regex else re.escape(pat), flags))
            except re.error as exc:
                raise ValueError(f"Invalid pattern {pat!r}: {exc}") from exc

        def qualified_name(row: dict[str, Any]) -> str:
            parent, name_val = row.get("parent") or "", row.get("name") or ""
            return f"{parent}::{name_val}" if parent else name_val

        def text_for_match(row: dict[str, Any]) -> str:
            if match_field == "parent":
                return row.get("parent") or ""
            if match_field == "qualified":
                return qualified_name(row)
            return row.get("name") or ""

        file_rows = _index_file_rows(index_payload)
        fid_to_name = _file_id_to_name_map(file_rows)

        matches: list[dict[str, Any]] = []
        truncated = False
        for line in entities:
            if not isinstance(line, str):
                continue
            row = _parse_entity_csv_row(line)
            if row is None:
                continue
            if type_allow is not None and row["type"] not in type_allow:
                continue
            if not any(c.search(text_for_match(row)) for c in compiled):
                continue
            matches.append({**row, "file_name": fid_to_name.get(row["file_id"])})
            if len(matches) >= max_results:
                truncated = True
                break

        matches.sort(key=lambda r: (r.get("file_id", 0), r.get("start_line", 0), r.get("name", "")))
        return _json_text(
            {
                "matches": matches,
                "count": len(matches),
                "truncated": truncated,
                "max_results": max_results,
                "project_id": project_id,
                "note": "Matches are definition rows from the sandwiches index only (not call-site grep).",
            }
        )

    if name == "cq_read_file":
        file_id = int(arguments["file_id"])
        content = await client.read_file(file_id)
        return _text(content)

    if name == "cq_start_grep":
        query = str(arguments.get("query") or "").strip()
        if not query:
            raise ValueError("cq_start_grep: query must be non-empty")
        search_mode = str(arguments.get("search_mode", "project_registered") or "project_registered").strip().lower()
        mode = str(arguments.get("mode", "code"))
        profile = str(arguments.get("profile", "all"))
        is_regex = bool(arguments.get("is_regex", False))
        case_sensitive = bool(arguments.get("case_sensitive", False))
        scan_hit_cap = max(1, min(int(arguments.get("scan_hit_cap", DEFAULT_SCAN_HIT_CAP)), DEFAULT_SCAN_HIT_CAP))
        max_returned = max(1, min(int(arguments.get("max_returned_items", 100)), 500))
        max_hits = max(1, min(int(arguments.get("max_results", 100)), scan_hit_cap))
        context_lines = min(max(int(arguments.get("context_lines", 0)), 0), 3)
        include_glob = arguments.get("include_glob")
        if include_glob is not None and not isinstance(include_glob, list):
            raise ValueError("include_glob must be an array of strings or omitted")

        if search_mode == "host_fs":
            host_path = str(arguments.get("host_path") or "").strip()
            if not host_path:
                raise ValueError("cq_start_grep host_fs requires host_path")
            timeout_sec = max(5, min(int(arguments.get("host_timeout_sec", 120)), 600))
            workers = max(1, min(int(arguments.get("host_workers", 8)), 32))
            if bool(arguments.get("host_async", False)):
                job_id = await start_host_grep_job(
                    host_path,
                    query,
                    mode=mode,
                    profile=profile,
                    include_glob=include_glob,
                    is_regex=is_regex,
                    case_sensitive=case_sensitive,
                    max_results=max_hits,
                    context_lines=context_lines,
                    timeout_sec=timeout_sec,
                    workers=workers,
                    page_size=max_returned,
                )
                return _json_text(
                    {
                        "status": "ok",
                        "search_mode": "host_fs",
                        "host_async": True,
                        "host_grep_job_id": job_id,
                        "host_grep_poll_hint_sec": host_grep_poll_hint_sec(),
                        "scan_complete": False,
                        "hits": [],
                        "total": 0,
                        "truncated": False,
                        "query": query,
                        "mode": mode,
                        "profile": profile,
                        "is_regex": is_regex,
                        "case_sensitive": case_sensitive,
                        "hint": "cq_fetch_result с полем host_grep_job_id; snapshot_seq в ответе меняется при тике poll и при завершении.",
                    }
                )
            result = await smart_grep_host_fs(
                host_path,
                query,
                mode=mode,
                profile=profile,
                include_glob=include_glob,
                is_regex=is_regex,
                case_sensitive=case_sensitive,
                max_results=max_hits,
                context_lines=context_lines,
                timeout_sec=timeout_sec,
                workers=workers,
            )
            result = await finalize_smart_grep_response(
                result,
                page_size=max_returned,
                store=get_page_store(),
                source_tool="cq_start_grep",
                scan_complete=True,
            )
            return _json_text(result)

        if search_mode not in ("project_registered", "project_refresh"):
            raise ValueError(
                "search_mode must be host_fs, project_registered, or project_refresh"
            )
        project_id = arguments.get("project_id")
        if project_id is None:
            raise ValueError("project_id is required for project_registered / project_refresh")
        project_id = int(project_id)
        time_strict = arguments.get("time_strict")
        ts_val = str(time_strict).strip() if time_strict is not None else ""
        path_prefix = str(arguments.get("path_prefix") or "").strip()
        offset = max(0, int(arguments.get("offset", 0)))
        limit_files = max(1, int(arguments.get("limit_files", 50)))

        sm = search_mode
        if offset != 0 and sm == "project_refresh":
            sm = "project_registered"

        payload: dict[str, Any] = {
            "project_id": project_id,
            "path_prefix": path_prefix,
            "offset": offset,
            "limit_files": limit_files,
            "max_hits": max_hits,
            "query": query,
            "mode": mode,
            "profile": profile,
            "is_regex": is_regex,
            "case_sensitive": case_sensitive,
            "context_lines": context_lines,
            "search_mode": sm,
        }
        if ts_val:
            payload["time_strict"] = ts_val
        if include_glob:
            payload["include_glob"] = list(include_glob)

        meta = await client.get_project_index_meta(project_id)
        payload["index_epoch"] = int(meta.get("index_epoch", 0))
        chunk = await client.smart_grep_chunk_stable(payload)

        need_more = not bool(chunk.get("scan_complete"))
        cont: dict[str, Any] | None = None
        if need_more:
            cont = {
                "project_id": project_id,
                "index_epoch": int(chunk.get("index_epoch", payload["index_epoch"])),
                "path_prefix": str(chunk.get("path_prefix", path_prefix)),
                "offset": int(chunk.get("next_offset", 0)),
                "limit_files": limit_files,
                "max_hits": max_hits,
                "query": query,
                "mode": mode,
                "profile": profile,
                "is_regex": is_regex,
                "case_sensitive": case_sensitive,
                "context_lines": context_lines,
                "search_mode": "project_registered",
            }
            if ts_val:
                cont["time_strict"] = ts_val
            if include_glob:
                cont["include_glob"] = list(include_glob)

        for_finalize = {**chunk, "search_mode": search_mode}
        out = await finalize_smart_grep_response(
            for_finalize,
            page_size=max_returned,
            store=get_page_store(),
            source_tool="cq_start_grep",
            scan_complete=bool(chunk.get("scan_complete")),
        )
        if cont is not None:
            out["chunk_continuation"] = cont
        return _json_text(out)

    if name == "cq_grep_logs":
        project_id = int(arguments["project_id"])
        query = str(arguments["query"] or "").strip()
        log_masks_raw = arguments.get("log_masks")
        if not query:
            raise ValueError("query must be non-empty")
        if log_masks_raw is None:
            log_masks_raw = []
        if not isinstance(log_masks_raw, list):
            raise ValueError("log_masks must be an array when provided")

        raw_sources = [str(mask).strip() for mask in log_masks_raw if str(mask).strip()]
        docker_services: list[str] = []
        file_masks: list[str] = []
        for source in raw_sources:
            if source.lower().startswith("docker:"):
                service = source.split(":", 1)[1].strip()
                if service:
                    docker_services.append(service)
            else:
                file_masks.append(source)

        if not docker_services and not file_masks:
            raise ValueError("Provide at least one source in log_masks: file glob or docker:<service>")

        tail_lines = max(1, min(int(arguments.get("tail_lines", 100)), 5000))
        since_seconds = max(0, min(int(arguments.get("since_seconds", 0)), 7 * 24 * 3600))
        case_sensitive = bool(arguments.get("case_sensitive", False))

        result_payload: dict[str, Any] = {}
        flags = re.MULTILINE if case_sensitive else (re.MULTILINE | re.IGNORECASE)
        pattern = re.compile(query, flags)

        if docker_services:
            compose_dir = str(Path(__file__).resolve().parent)
            docker_errors: dict[str, str] = {}
            for service in docker_services:
                cmd = [
                    "docker",
                    "compose",
                    "logs",
                    service,
                    "--no-color",
                    "--tail",
                    str(tail_lines),
                ]
                if since_seconds > 0:
                    cmd.extend(["--since", f"{since_seconds}s"])

                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=compose_dir,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    stdin=asyncio.subprocess.DEVNULL,
                )
                stdout_bytes, stderr_bytes = await proc.communicate()
                stdout_text = stdout_bytes.decode("utf-8", errors="replace")
                stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
                key = f"docker:{service}"
                if proc.returncode != 0:
                    result_payload[key] = []
                    docker_errors[key] = stderr_text or f"docker compose logs failed with exit code {proc.returncode}"
                    continue
                matched = [line for line in stdout_text.splitlines() if pattern.search(line)]
                result_payload[key] = matched[-tail_lines:] if tail_lines > 0 else matched

            if docker_errors:
                result_payload["_docker_errors"] = docker_errors

        encoded_query = base64.b64encode(query.encode("utf-8")).decode("ascii")
        encoded_masks = base64.b64encode(
            json.dumps(file_masks, ensure_ascii=False).encode("utf-8")
        ).decode("ascii")

        if file_masks:
            command = (
                "python3 - <<'PY'\n"
                "import base64, glob, json, os, re, time\n"
                "from datetime import datetime\n"
                f"query = base64.b64decode('{encoded_query}').decode('utf-8')\n"
                f"masks = json.loads(base64.b64decode('{encoded_masks}').decode('utf-8'))\n"
                f"tail_lines = {tail_lines}\n"
                f"since_seconds = {since_seconds}\n"
                f"case_sensitive = {str(case_sensitive)}\n"
                "cutoff_ts = (time.time() - since_seconds) if since_seconds > 0 else None\n"
                "flags = re.MULTILINE if case_sensitive else (re.MULTILINE | re.IGNORECASE)\n"
                "pattern = re.compile(query, flags)\n"
                "ts_patterns = [\n"
                "    re.compile(r'^\\[(\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}:\\d{2})(?:[\\.,]\\d+)?\\]'),\n"
                "    re.compile(r'^(\\d{4}-\\d{2}-\\d{2}[ T]\\d{2}:\\d{2}:\\d{2})(?:[\\.,]\\d+)?'),\n"
                "]\n"
                "ts_formats = ['%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S']\n"
                "def parse_line_ts(line):\n"
                "    for rx in ts_patterns:\n"
                "        m = rx.search(line)\n"
                "        if not m:\n"
                "            continue\n"
                "        raw = m.group(1).replace('T', ' ')\n"
                "        for fmt in ts_formats:\n"
                "            try:\n"
                "                dt = datetime.strptime(raw, fmt.replace('T', ' '))\n"
                "                return dt.timestamp()\n"
                "            except ValueError:\n"
                "                pass\n"
                "    return None\n"
                "paths = []\n"
                "seen = set()\n"
                "for mask in masks:\n"
                "    for path in glob.glob(mask, recursive=True):\n"
                "        norm = os.path.normpath(path)\n"
                "        if not os.path.isfile(norm):\n"
                "            continue\n"
                "        if norm in seen:\n"
                "            continue\n"
                "        seen.add(norm)\n"
                "        paths.append(norm)\n"
                "paths.sort()\n"
                "result = {}\n"
                "for path in paths:\n"
                "    try:\n"
                "        with open(path, 'r', encoding='utf-8', errors='replace') as fh:\n"
                "            lines = fh.read().splitlines()\n"
                "    except OSError:\n"
                "        result[path] = []\n"
                "        continue\n"
                "    matched = []\n"
                "    current_ts = None\n"
                "    for line in lines:\n"
                "        parsed_ts = parse_line_ts(line)\n"
                "        if parsed_ts is not None:\n"
                "            current_ts = parsed_ts\n"
                "        effective_ts = parsed_ts if parsed_ts is not None else current_ts\n"
                "        if cutoff_ts is not None and (effective_ts is None or effective_ts < cutoff_ts):\n"
                "            continue\n"
                "        if pattern.search(line):\n"
                "            matched.append(line)\n"
                "    if tail_lines > 0:\n"
                "        matched = matched[-tail_lines:]\n"
                "    result[path] = matched\n"
                "print(json.dumps(result, ensure_ascii=False))\n"
                "PY"
            )

            result = await client.exec_command(project_id, command, 120)
            output = result.get("output", "") if isinstance(result, dict) else ""
            parsed_output = output.strip()
            if parsed_output.startswith("<stdout>") and "</stdout>" in parsed_output:
                parsed_output = parsed_output[len("<stdout>") : parsed_output.rfind("</stdout>")].strip()
            try:
                parsed = json.loads(parsed_output)
            except Exception as exc:
                return _json_text(
                    {
                        "status": "error",
                        "error": f"Failed to parse cq_grep_logs output as JSON: {exc}",
                        "raw_output": output,
                        "exec": result,
                    }
                )
            result_payload.update(parsed)

        return _json_text(result_payload)

    if name == "cq_replace":
        project_id = int(arguments["project_id"])
        file_id = int(arguments["file_id"])
        old = str(arguments["old"])
        new = str(arguments["new"])
        is_regex = bool(arguments.get("is_regex", False))
        case_sensitive = bool(arguments.get("case_sensitive", True))
        max_replacements = int(arguments.get("max_replacements", 0))
        result = await client.replace_file(
            project_id=project_id,
            file_id=file_id,
            old=old,
            new=new,
            is_regex=is_regex,
            case_sensitive=case_sensitive,
            max_replacements=max_replacements,
        )
        return _json_text(result)

    return None