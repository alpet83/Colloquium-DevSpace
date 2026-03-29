from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
import re
import time
from datetime import datetime
from typing import Any

import httpx  # type: ignore[import]
from mcp.types import CallToolResult, Tool  # type: ignore[import]

from cqds_helpers import (
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
                "entity_types": {"type": "array", "items": {"type": "string"}, "description": "Optional whitelist of entity type strings (e.g. function, class, method). Omit for all types."},
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
        name="cq_smart_grep",
        description=(
            "Search text or regex in predefined project file sets (code/logs/docs/all) on the CQDS project tree in one call. "
            "Prefer this over running grep/find in the IDE terminal on Windows when the task targets Colloquium-attached sources. "
            "Direct call — no LLM chat loop."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "integer", "description": "Project ID."},
                "query": {"type": "string", "description": "Text or regex pattern to find."},
                "mode": {"type": "string", "description": "File set preset: code | logs | docs | all (default: code).", "default": "code"},
                "profile": {"type": "string", "description": "Focus profile: all | backend | frontend | docs | infra | tests | logs (default: all).", "default": "all"},
                "time_strict": {"type": "string", "description": "Optional time filter, e.g. 'mtime>2026-03-25', 'mtime>=2026-03-25 21:00', 'ctime>1711390800'."},
                "is_regex": {"type": "boolean", "description": "Interpret query as regex.", "default": False},
                "case_sensitive": {"type": "boolean", "description": "Case-sensitive search.", "default": False},
                "max_results": {"type": "integer", "description": "Maximum returned matches (1..500).", "default": 100},
                "context_lines": {"type": "integer", "description": "Context lines before/after match (0..3).", "default": 0},
                "include_glob": {"type": "array", "items": {"type": "string"}, "description": "Optional extra path globs to narrow search, e.g. ['src/**/*.py']."},
            },
            "required": ["project_id", "query"],
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

    if name in {"cq_rebuild_index", "cq_get_code_index"}:
        project_id = int(arguments["project_id"])
        background = bool(arguments.get("background", False))
        timeout = int(arguments.get("timeout", 300))
        if background:
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

        index = await client.get_code_index(project_id, timeout=timeout)
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

    if name == "cq_smart_grep":
        project_id = int(arguments["project_id"])
        query = str(arguments["query"])
        mode = str(arguments.get("mode", "code"))
        profile = str(arguments.get("profile", "all"))
        time_strict = arguments.get("time_strict")
        is_regex = bool(arguments.get("is_regex", False))
        case_sensitive = bool(arguments.get("case_sensitive", False))
        max_results = int(arguments.get("max_results", 100))
        context_lines = int(arguments.get("context_lines", 0))
        include_glob = arguments.get("include_glob")
        result = await client.smart_grep(
            project_id=project_id,
            query=query,
            mode=mode,
            profile=profile,
            time_strict=str(time_strict) if time_strict is not None else None,
            is_regex=is_regex,
            case_sensitive=case_sensitive,
            max_results=max_results,
            context_lines=context_lines,
            include_glob=include_glob,
        )
        return _json_text(result)

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