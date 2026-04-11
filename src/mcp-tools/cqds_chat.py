# cqds_chat.py — Tools and handlers for chat operations (list, create, send, wait, history, stats)
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from mcp.types import CallToolResult, Tool  # type: ignore[import]

from cqds_helpers import _extract_latest_message, _is_progress_stub, _json_text, _text
from cqds_run_ctx import RunContext


def _default_copilot_storage_root() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "Code" / "User" / "workspaceStorage"

    home = Path.home()
    candidates = [
        home / "AppData" / "Roaming" / "Code" / "User" / "workspaceStorage",
        home / ".config" / "Code" / "User" / "workspaceStorage",
        home / "Library" / "Application Support" / "Code" / "User" / "workspaceStorage",
    ]
    for path in candidates:
        if path.exists():
            return path
    return candidates[0]


def _autodetect_copilot_storage_roots() -> list[Path]:
    """Find likely workspaceStorage roots in standard user-home locations.

    Preference:
      1) Existing paths that contain chat/session files
      2) Existing paths even if empty
      3) Default fallback path
    """
    roots: list[Path] = []
    seen: set[str] = set()

    appdata = os.environ.get("APPDATA")
    home = Path.home()

    candidates = [
        # Windows (stable + insiders + cursor)
        Path(appdata) / "Code" / "User" / "workspaceStorage" if appdata else None,
        Path(appdata) / "Code - Insiders" / "User" / "workspaceStorage" if appdata else None,
        Path(appdata) / "Cursor" / "User" / "workspaceStorage" if appdata else None,
        # Linux
        home / ".config" / "Code" / "User" / "workspaceStorage",
        home / ".config" / "Code - Insiders" / "User" / "workspaceStorage",
        home / ".config" / "Cursor" / "User" / "workspaceStorage",
        # macOS
        home / "Library" / "Application Support" / "Code" / "User" / "workspaceStorage",
        home / "Library" / "Application Support" / "Code - Insiders" / "User" / "workspaceStorage",
        home / "Library" / "Application Support" / "Cursor" / "User" / "workspaceStorage",
    ]

    # Keep existing paths first.
    for cand in candidates:
        if cand is None or not cand.exists():
            continue
        key = str(cand)
        if key in seen:
            continue
        seen.add(key)
        roots.append(cand)

    if not roots:
        roots.append(_default_copilot_storage_root())

    with_data: list[Path] = []
    without_data: list[Path] = []
    for root in roots:
        files = _iter_copilot_storage_files(root)
        if files:
            with_data.append(root)
        else:
            without_data.append(root)

    if with_data:
        return with_data + without_data
    return roots


def _iter_copilot_storage_files(root: Path) -> list[Path]:
    files = set(root.glob("*/chatSessions/*.jsonl"))
    files.update(root.glob("*/chatEditingSessions/*/state.json"))
    return sorted(files)


def _is_invalid_text(value: str) -> bool:
    return value == "" or value.strip() == ""


def _render_vscode_node(node: Any) -> str:
    """Recursively concatenate all text leaves from a VS Code internal tree node."""
    if isinstance(node, dict):
        t = node.get("text")
        result = t if isinstance(t, str) else ""
        for child in (node.get("children") or []):
            result += _render_vscode_node(child)
        return result
    if isinstance(node, list):
        return "".join(_render_vscode_node(n) for n in node)
    return ""


def _placeholder_vscode_node() -> dict[str, Any]:
    return {
        "type": 1, "ctor": 2, "ctorName": "ZRe",
        "children": [{"type": 2, "priority": 0, "text": "[empty tool result]",
                       "references": [], "lineBreakBefore": False}],
    }


def _scan_obj(obj: Any, path_stack: list[Any], out: list[dict[str, Any]]) -> None:
    if isinstance(obj, dict):
        cur_type = obj.get("type")
        cur_text = obj.get("text")
        if cur_type == "text" and isinstance(cur_text, str) and _is_invalid_text(cur_text):
            out.append({"json_path": _as_json_path(path_stack), "text_repr": repr(cur_text)})
        for key, val in obj.items():
            _scan_obj(val, path_stack + [key], out)
    elif isinstance(obj, list):
        for idx, val in enumerate(obj):
            _scan_obj(val, path_stack + [idx], out)


def _fix_obj(obj: Any) -> int:
    fixed = 0
    if isinstance(obj, dict):
        cur_type = obj.get("type")
        cur_text = obj.get("text")
        if cur_type == "text" and isinstance(cur_text, str) and _is_invalid_text(cur_text):
            obj["text"] = "[interrupted]"
            fixed += 1
        for key in list(obj.keys()):
            fixed += _fix_obj(obj[key])
    elif isinstance(obj, list):
        for item in obj:
            fixed += _fix_obj(item)
    return fixed


def _as_json_path(parts: list[Any]) -> str:
    if not parts:
        return "$"
    out = "$"
    for part in parts:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}"
    return out


def _backup_path(path: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return Path(f"{path}.bak-{stamp}-copilot-chat-check")


def _scan_tool_results(v: Any, line_no: int, out: list[dict[str, Any]]) -> None:
    """Detect tool call results whose rendered node text is empty/whitespace.

    These produce empty Anthropic API content blocks and cause 400 errors.
    Location: v.requests[].result.metadata.toolCallResults[id].content[i].value.node
    """
    if not isinstance(v, dict):
        return
    for req in (v.get("requests") or []):
        if not isinstance(req, dict):
            continue
        result_obj = req.get("result")
        if not isinstance(result_obj, dict):
            continue
        meta = result_obj.get("metadata")
        if not isinstance(meta, dict):
            continue
        tcr = meta.get("toolCallResults")
        if not isinstance(tcr, dict):
            continue
        for tool_id, tval in tcr.items():
            if not isinstance(tval, dict):
                continue
            for ci, content_item in enumerate(tval.get("content") or []):
                if not isinstance(content_item, dict):
                    continue
                val = content_item.get("value")
                if not isinstance(val, dict):
                    continue
                node = val.get("node")
                if node is None:
                    continue
                rendered = _render_vscode_node(node).strip()
                if not rendered:
                    out.append({
                        "line": line_no,
                        "json_path": f"$.v.requests[?].result.metadata"
                                     f".toolCallResults.{tool_id[:40]}.content[{ci}]",
                        "text_repr": "(empty node tree)",
                        "kind": "empty_tool_result",
                    })


def _fix_tool_results(v: Any) -> int:
    """Replace empty-rendered tool result nodes with a placeholder. Returns fix count."""
    fixed = 0
    if not isinstance(v, dict):
        return 0
    for req in (v.get("requests") or []):
        if not isinstance(req, dict):
            continue
        result_obj = req.get("result")
        if not isinstance(result_obj, dict):
            continue
        meta = result_obj.get("metadata")
        if not isinstance(meta, dict):
            continue
        tcr = meta.get("toolCallResults")
        if not isinstance(tcr, dict):
            continue
        for tval in tcr.values():
            if not isinstance(tval, dict):
                continue
            for content_item in (tval.get("content") or []):
                if not isinstance(content_item, dict):
                    continue
                val = content_item.get("value")
                if not isinstance(val, dict):
                    continue
                node = val.get("node")
                if node is None:
                    continue
                if not _render_vscode_node(node).strip():
                    val["node"] = _placeholder_vscode_node()
                    fixed += 1
    return fixed


def _scan_and_fix_jsonl(path: Path, auto_recovery: bool) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    parse_errors = 0
    fixed_count = 0
    changed = False

    tmp_path = Path(f"{path}.tmp-copilot-chat-check") if auto_recovery else None
    writer = tmp_path.open("w", encoding="utf-8") if tmp_path else None

    try:
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            for line_no, line in enumerate(fh, start=1):
                raw = line.rstrip("\n")
                if not raw.strip():
                    if writer:
                        writer.write(line)
                    continue

                try:
                    obj = json.loads(raw)
                except Exception:
                    parse_errors += 1
                    if writer:
                        writer.write(line)
                    continue

                line_issues: list[dict[str, Any]] = []
                _scan_obj(obj, [], line_issues)
                v_val = obj.get("v") if isinstance(obj, dict) else None
                _scan_tool_results(v_val, line_no, line_issues)
                for issue in line_issues:
                    issues.append({"line": line_no, **issue} if "line" not in issue else issue)

                if auto_recovery and line_issues:
                    fixed_now = _fix_obj(obj)
                    fixed_now += _fix_tool_results(v_val)
                    if fixed_now > 0:
                        changed = True
                        fixed_count += fixed_now
                    writer.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
                elif writer:
                    writer.write(line)

        if writer:
            writer.close()
            writer = None

        backup_file = None
        if auto_recovery and changed and tmp_path is not None:
            backup_file = _backup_path(path)
            shutil.copy2(path, backup_file)
            os.replace(tmp_path, path)
        elif tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)

        return {
            "file": str(path),
            "issues": len(issues),
            "parse_errors": parse_errors,
            "fixed": fixed_count,
            "backup": str(backup_file) if backup_file else None,
            "sample": issues[:10],
        }
    finally:
        if writer:
            writer.close()


def _scan_and_fix_json(path: Path, auto_recovery: bool) -> dict[str, Any]:
    parse_errors = 0
    try:
        obj = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        parse_errors = 1
        return {
            "file": str(path),
            "issues": 0,
            "parse_errors": parse_errors,
            "fixed": 0,
            "backup": None,
            "sample": [],
        }

    issues: list[dict[str, Any]] = []
    _scan_obj(obj, [], issues)

    fixed = 0
    backup_file = None
    if auto_recovery and issues:
        fixed = _fix_obj(obj)
        if fixed > 0:
            backup_file = _backup_path(path)
            shutil.copy2(path, backup_file)
            path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "file": str(path),
        "issues": len(issues),
        "parse_errors": parse_errors,
        "fixed": fixed,
        "backup": str(backup_file) if backup_file else None,
        "sample": issues[:10],
    }

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="cq_list_chats",
        description=(
            "List all chats available in Colloquium-DevSpace. "
            "Use when you need chat_id for cq_send_message or history tools; "
            "not a substitute for cq_list_projects (projects vs chats)."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    Tool(
        name="cq_create_chat",
        description=(
            "Create a new chat in Colloquium-DevSpace. Returns the new chat_id. "
            "Use before cq_edit_file/cq_patch_file/cq_undo_file when those must post via chat messages."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "Short description / title for the new chat.",
                }
            },
            "required": [],
        },
    ),
    Tool(
        name="cq_send_message",
        description=(
            "Send a plain text message to a Colloquium chat and return immediately. "
            "Use cq_wait_reply (or cq_get_history) to read the AI response; "
            "or cq_set_sync_mode with timeout>0 so cq_send_message waits for the reply."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "chat_id": {"type": "integer", "description": "Target chat ID."},
                "message": {"type": "string", "description": "Message text to send."},
            },
            "required": ["chat_id", "message"],
        },
    ),
    Tool(
        name="cq_wait_reply",
        description=(
            "Long-poll a Colloquium chat for new AI messages (up to 15 s). "
            "Returns the latest posts or 'no changes' if nothing arrived. "
            "Use cq_get_history instead if you need to read existing messages."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "chat_id": {"type": "integer", "description": "Chat ID to poll."},
            },
            "required": ["chat_id"],
        },
    ),
    Tool(
        name="cq_get_history",
        description=(
            "Fetch the current chat history snapshot immediately (no waiting). "
            "Use this to read messages that already arrived, e.g. after cq_send_message "
            "when cq_wait_reply returned 'no changes'."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "chat_id": {"type": "integer", "description": "Chat ID to read."},
            },
            "required": ["chat_id"],
        },
    ),
    Tool(
        name="cq_chat_stats",
        description=(
            "Get aggregated chat usage stats (calls, tokens, model breakdown, costs). "
            "Optional since_seconds limits stats to the last N seconds."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "chat_id": {
                    "type": "integer",
                    "description": "Chat ID to aggregate stats for.",
                },
                "since_seconds": {
                    "type": "integer",
                    "description": "Optional lookback window in seconds (0 = full history).",
                    "default": 0,
                },
            },
            "required": ["chat_id"],
        },
    ),
    Tool(
        name="cq_copilot_chat_check",
        description=(
            "Scan VS Code Copilot workspace storage for invalid text blocks in chat payloads "
            "(empty or whitespace-only text for type='text'). "
            "With auto_recovery=true, creates backups and replaces invalid text with '[interrupted]'. "
            "If root is omitted, tool auto-detects standard user-home workspaceStorage paths."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "root": {
                    "type": "string",
                    "description": "Optional workspaceStorage root path. Default resolves from APPDATA/home.",
                },
                "auto_recovery": {
                    "type": "boolean",
                    "description": "If true, backup and patch invalid blocks in-place.",
                    "default": False,
                },
            },
            "required": [],
        },
    ),
]

# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

async def handle(
    name: str, arguments: dict[str, Any], ctx: RunContext
) -> CallToolResult | None:
    client = ctx.client

    if name == "cq_list_chats":
        chats = await client.list_chats()
        return _json_text(chats)

    if name == "cq_create_chat":
        description = arguments.get("description", "MCP Session")
        chat_id = await client.create_chat(description)
        return _text(f"Created chat with chat_id={chat_id}")

    if name == "cq_send_message":
        chat_id = int(arguments["chat_id"])
        message = str(arguments["message"])
        await client.post_message(chat_id, message)
        if client._sync_timeout > 0:
            deadline = time.monotonic() + client._sync_timeout
            saw_progress_stub = False
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                resp = await client.get_reply(chat_id, wait=True, timeout=min(remaining, 15.0))
                hist = resp.get("chat_history", "") if isinstance(resp, dict) else ""
                if hist not in ("no changes", "chat switch"):
                    latest_message = _extract_latest_message(resp)
                    if latest_message and _is_progress_stub(latest_message):
                        saw_progress_stub = True
                        continue
                    return _json_text(resp)
            if saw_progress_stub:
                return _text(
                    f"Message sent to chat_id={chat_id} "
                    f"(sync: only progress stub seen within {client._sync_timeout}s)"
                )
            return _text(
                f"Message sent to chat_id={chat_id} (sync: no reply in {client._sync_timeout}s)"
            )
        return _text(f"Message sent to chat_id={chat_id}")

    if name == "cq_wait_reply":
        chat_id = int(arguments["chat_id"])
        resp = await client.get_reply(chat_id)
        return _json_text(resp)

    if name == "cq_get_history":
        chat_id = int(arguments["chat_id"])
        resp = await client.get_history(chat_id)
        return _json_text(resp)

    if name == "cq_chat_stats":
        chat_id = int(arguments["chat_id"])
        since_seconds_raw = arguments.get("since_seconds", 0)
        since_seconds = int(since_seconds_raw) if since_seconds_raw is not None else 0
        since_seconds = max(0, min(since_seconds, 30 * 24 * 3600))
        resp = await client.get_chat_stats(
            chat_id=chat_id,
            since_seconds=since_seconds if since_seconds > 0 else None,
        )
        return _json_text(resp)

    if name == "cq_copilot_chat_check":
        root_raw = arguments.get("root")
        roots = [Path(str(root_raw)).expanduser()] if root_raw else _autodetect_copilot_storage_roots()
        auto_recovery = bool(arguments.get("auto_recovery", False))

        missing_roots = [str(root) for root in roots if not root.exists()]
        existing_roots = [root for root in roots if root.exists()]
        if not existing_roots:
            return _json_text(
                {
                    "ok": False,
                    "error": "root_not_found",
                    "roots": [str(root) for root in roots],
                }
            )

        files_set: set[Path] = set()
        for root in existing_roots:
            files_set.update(_iter_copilot_storage_files(root))
        files = sorted(files_set)
        file_results: list[dict[str, Any]] = []
        total_issues = 0
        total_parse_errors = 0
        total_fixed = 0
        backups: list[str] = []

        for fp in files:
            if fp.suffix.lower() == ".jsonl":
                row = _scan_and_fix_jsonl(fp, auto_recovery)
            elif fp.suffix.lower() == ".json":
                row = _scan_and_fix_json(fp, auto_recovery)
            else:
                continue

            if row.get("issues", 0) > 0 or row.get("fixed", 0) > 0 or row.get("parse_errors", 0) > 0:
                file_results.append(row)

            total_issues += int(row.get("issues", 0))
            total_parse_errors += int(row.get("parse_errors", 0))
            total_fixed += int(row.get("fixed", 0))
            if row.get("backup"):
                backups.append(str(row["backup"]))

        return _json_text(
            {
                "ok": True,
                "root": str(existing_roots[0]),
                "roots_scanned": [str(root) for root in existing_roots],
                "roots_missing": missing_roots,
                "auto_recovery": auto_recovery,
                "files_scanned": len(files),
                "files_with_findings": len(file_results),
                "issues_found": total_issues,
                "parse_errors": total_parse_errors,
                "fixed": total_fixed,
                "backups": backups,
                "files": file_results,
            }
        )

    return None
