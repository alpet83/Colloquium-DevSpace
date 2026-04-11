# cqds_helpers.py — Logging, text/XML/exec/index utilities shared across cqds_* modules
from __future__ import annotations

import base64
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from mcp.types import CallToolResult, TextContent, Tool  # type: ignore[import]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOGGER = logging.getLogger("cqds_mcp_full")
CURRENT_TOOL: ContextVar[str] = ContextVar("cqds_mcp_current_tool", default="-")


def _default_runtime_log_path() -> Path:
    """Имя журнала с меткой времени до минуты (без секунд), чтобы параллельные MCP не делили один файл.

    Префикс: COLLOQUIUM_MCP_LOG_STEM (по умолчанию cqds_mcp_full.runtime для полного MCP).
    CQDS MCP mini (``cqds_mcp_mini.py``) задаёт stem=cqds_mcp_mini до _setup_logging().
    """
    logs_dir = Path(__file__).resolve().parent / "logs"
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    stem = (os.environ.get("COLLOQUIUM_MCP_LOG_STEM") or "cqds_mcp_full.runtime").strip() or "cqds_mcp_full.runtime"
    candidate = logs_dir / f"{stem}.{stamp}.log"
    if candidate.exists():
        candidate = logs_dir / f"{stem}.{stamp}.{os.getpid()}.log"
    return candidate


def _setup_logging() -> Path:
    default_log = _default_runtime_log_path()
    log_file = Path(os.environ.get("COLLOQUIUM_MCP_LOG_FILE", str(default_log))).resolve()
    log_level_name = os.environ.get("COLLOQUIUM_MCP_LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_name, logging.INFO)

    log_file.parent.mkdir(parents=True, exist_ok=True)

    LOGGER.handlers.clear()
    LOGGER.setLevel(log_level)
    LOGGER.propagate = False

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)
    LOGGER.addHandler(file_handler)

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(log_level)
    stderr_handler.setFormatter(formatter)
    LOGGER.addHandler(stderr_handler)

    return log_file


# ---------------------------------------------------------------------------
# MCP tool visibility (CQ_HIDE_TOOLS)
# ---------------------------------------------------------------------------
# Перечень имён инструментов через запятую — не попадают в list_tools и не вызываются
# (текущая конфигурация процесса MCP).


def cq_hide_tool_names() -> frozenset[str]:
    raw = (os.environ.get("CQ_HIDE_TOOLS") or "").strip()
    if not raw:
        return frozenset()
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


def cq_tool_is_hidden(tool_name: str) -> bool:
    return tool_name in cq_hide_tool_names()


def cq_filter_tools_for_list(tools: list[Tool]) -> list[Tool]:
    hide = cq_hide_tool_names()
    if not hide:
        return list(tools)
    return [t for t in tools if t.name not in hide]


# ---------------------------------------------------------------------------
# Text utilities
# ---------------------------------------------------------------------------

def _preview_text(text: str, limit: int = 200) -> str:
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[:limit]}..."


def _summarize_arguments(arguments: dict[str, Any]) -> str:
    if not isinstance(arguments, dict):
        return str(type(arguments).__name__)
    parts: list[str] = []
    for key in sorted(arguments.keys()):
        value = arguments[key]
        if key.lower() in {"password", "token", "authorization"}:
            parts.append(f"{key}=<redacted>")
            continue
        if isinstance(value, str):
            parts.append(f"{key}=str(len={len(value)}, preview='{_preview_text(value, 64)}')")
            continue
        if isinstance(value, list):
            parts.append(f"{key}=list(len={len(value)})")
            continue
        if isinstance(value, dict):
            parts.append(f"{key}=dict(keys={len(value)})")
            continue
        parts.append(f"{key}={value!r}")
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# MCP result constructors
# ---------------------------------------------------------------------------

def _text(content: str) -> CallToolResult:
    LOGGER.info(
        "TOOL result name=%s content=%s",
        CURRENT_TOOL.get(),
        _preview_text(content, 220),
    )
    return CallToolResult(content=[TextContent(type="text", text=content)])


def _json_text(obj: Any) -> CallToolResult:
    return _text(json.dumps(obj, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# XML chat message helpers
# ---------------------------------------------------------------------------

def _xml_code_file(path: str, content: str) -> str:
    return f'<code_file name="{path}">\n{content}\n</code_file>'


def _xml_patch(path: str, diff: str) -> str:
    return f'<patch name="{path}">\n{diff}\n</patch>'


def _xml_undo(file_id: int, time_back: int = 3600) -> str:
    return f'<undo file_id={file_id} time_back={time_back}>'


# ---------------------------------------------------------------------------
# Exec output helpers
# ---------------------------------------------------------------------------

def _unwrap_exec_output(raw_output: str) -> dict[str, str]:
    text = str(raw_output or "")
    stdout_match = re.search(r"<stdout>(.*?)</stdout>", text, flags=re.DOTALL)
    stderr_match = re.search(r"<stderr>(.*?)</stderr>", text, flags=re.DOTALL)

    if stdout_match or stderr_match:
        return {
            "stdout": (stdout_match.group(1) if stdout_match else "").strip(),
            "stderr": (stderr_match.group(1) if stderr_match else "").strip(),
        }

    return {
        "stdout": text.strip(),
        "stderr": "",
    }


def _normalize_exec_result(result: dict[str, Any], command: str, timeout: int) -> dict[str, Any]:
    output_raw = str(result.get("output", ""))
    streams = _unwrap_exec_output(output_raw)
    normalized: dict[str, Any] = {
        "status": result.get("status"),
        "project": result.get("project"),
        "command": command,
        "timeout": timeout,
        "stdout": streams["stdout"],
        "stderr": streams["stderr"],
        "output": streams["stdout"],
        "output_raw": output_raw,
    }
    for key in ("exit_code", "signal", "duration_ms"):
        if key in result:
            normalized[key] = result[key]
    return normalized


def _parse_exec_commands(
    command_arg: Any,
    default_timeout: int,
) -> list[tuple[str, int]]:
    if isinstance(command_arg, str):
        candidate = command_arg.strip()
        if candidate.startswith("{") or candidate.startswith("["):
            try:
                parsed = json.loads(candidate)
                return _parse_exec_commands(parsed, default_timeout)
            except Exception:
                pass
        if not candidate:
            raise ValueError("command must be non-empty")
        return [(candidate, default_timeout)]

    def normalize_item(item: Any) -> tuple[str, int]:
        if isinstance(item, str):
            cmd = item.strip()
            if not cmd:
                raise ValueError("command item must be non-empty")
            return cmd, default_timeout
        if isinstance(item, dict):
            cmd = str(item.get("command", "")).strip()
            if not cmd:
                raise ValueError("command item dict requires non-empty 'command'")
            cmd_timeout = int(item.get("timeout", default_timeout))
            cmd_timeout = max(1, min(cmd_timeout, 300))
            return cmd, cmd_timeout
        raise ValueError("command item must be string or object")

    if isinstance(command_arg, list):
        if not command_arg:
            raise ValueError("command list must be non-empty")
        return [normalize_item(item) for item in command_arg]

    if isinstance(command_arg, dict):
        if "commands" in command_arg:
            commands = command_arg["commands"]
            if not isinstance(commands, list) or not commands:
                raise ValueError("command.commands must be a non-empty array")
            return [normalize_item(item) for item in commands]
        if "command" in command_arg:
            return [normalize_item(command_arg)]
        raise ValueError("command object must contain 'command' or 'commands'")

    raise ValueError("command must be string, object, or array")


def _build_spawn_script_command(script_payload: dict[str, Any]) -> str:
    encoded = base64.b64encode(
        json.dumps(script_payload, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    return (
        "python3 - <<'PY'\n"
        "import base64, json, os, subprocess, tempfile\n"
        f"cfg = json.loads(base64.b64decode('{encoded}').decode('utf-8'))\n"
        "engine = cfg.get('engine', 'bash')\n"
        "commands = cfg.get('commands', [])\n"
        "script_name = cfg.get('script_name') or f'cq_spawn_{os.getpid()}'\n"
        "keep_file = bool(cfg.get('keep_file', False))\n"
        "if engine not in ('bash', 'python'):\n"
        "    raise SystemExit('Unsupported engine, expected bash or python')\n"
        "suffix = '.py' if engine == 'python' else '.sh'\n"
        "runner = 'python3' if engine == 'python' else '/bin/bash'\n"
        "script_text = '\n'.join(commands) + '\n'\n"
        "tmp_path = os.path.join(tempfile.gettempdir(), script_name + suffix)\n"
        "with open(tmp_path, 'w', encoding='utf-8') as handle:\n"
        "    handle.write(script_text)\n"
        "if engine == 'bash':\n"
        "    os.chmod(tmp_path, 0o755)\n"
        "proc = subprocess.run([runner, tmp_path], capture_output=True, text=True)\n"
        "if (not keep_file) and os.path.exists(tmp_path):\n"
        "    os.remove(tmp_path)\n"
        "print(json.dumps({\n"
        "    'script_path': tmp_path,\n"
        "    'engine': engine,\n"
        "    'returncode': proc.returncode,\n"
        "    'stdout': proc.stdout,\n"
        "    'stderr': proc.stderr,\n"
        "    'kept': keep_file,\n"
        "}, ensure_ascii=False))\n"
        "PY"
    )


# ---------------------------------------------------------------------------
# Index helpers
# ---------------------------------------------------------------------------

def _index_counts(index_payload: dict[str, Any]) -> tuple[int | None, int | None]:
    entities = index_payload.get("entities") if isinstance(index_payload, dict) else None
    filelist = None
    if isinstance(index_payload, dict):
        filelist = index_payload.get("files")
        if filelist is None:
            filelist = index_payload.get("filelist")
    entities_count = len(entities) if isinstance(entities, list) else None
    files_count = len(filelist) if isinstance(filelist, (list, dict)) else None
    return entities_count, files_count


def _index_file_rows(index_payload: dict[str, Any]) -> list[str]:
    files = index_payload.get("files") if isinstance(index_payload, dict) else None
    if files is None and isinstance(index_payload, dict):
        files = index_payload.get("filelist")
    if isinstance(files, list):
        return [r for r in files if isinstance(r, str)]
    return []


def _file_id_to_name_map(rows: list[str]) -> dict[int, str]:
    out: dict[int, str] = {}
    for row in rows:
        parts = row.split(",")
        if not parts:
            continue
        try:
            fid = int(parts[0].strip())
        except ValueError:
            continue
        if len(parts) > 1:
            out[fid] = parts[1]
    return out


def _parse_entity_csv_row(line: str) -> dict[str, Any] | None:
    """Parse one sandwiches_index entities CSV row: vis,type,parent,name,file_id,start-end,tokens."""
    if not isinstance(line, str) or not line.strip():
        return None
    parts = line.split(",")
    if len(parts) < 7:
        return None
    vis, e_type, parent, name = parts[0], parts[1], parts[2], parts[3]
    file_id_s, span_s, tokens_s = parts[4], parts[5], parts[6]
    try:
        file_id = int(file_id_s.strip())
    except ValueError:
        return None
    m = re.match(r"^(\d+)-(\d+)$", span_s.strip())
    if not m:
        return None
    start_line, end_line = int(m.group(1)), int(m.group(2))
    try:
        tokens = int(tokens_s.strip())
    except ValueError:
        tokens = 0
    return {
        "vis": vis,
        "type": e_type,
        "parent": parent,
        "name": name,
        "file_id": file_id,
        "start_line": start_line,
        "end_line": end_line,
        "tokens": tokens,
    }


def _build_file_tree_from_index(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Nest flat file_index rows by file_name path segments (posix-style)."""
    root: dict[str, Any] = {"kind": "dir", "name": "", "path": "", "children": []}

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        raw = entry.get("file_name")
        if raw is None:
            continue
        fn = str(raw).replace("\\", "/").strip("/")
        if not fn:
            continue
        parts = [p for p in fn.split("/") if p]
        node = root
        acc: list[str] = []
        for i, seg in enumerate(parts):
            acc.append(seg)
            is_leaf = i == len(parts) - 1
            children: list[dict[str, Any]] = node.setdefault("children", [])
            if is_leaf:
                leaf: dict[str, Any] = {
                    "kind": "file",
                    "name": seg,
                    "file_name": fn,
                    "id": entry.get("id"),
                    "ts": entry.get("ts"),
                }
                if "project_id" in entry:
                    leaf["project_id"] = entry["project_id"]
                if "size_bytes" in entry:
                    leaf["size_bytes"] = entry["size_bytes"]
                children.append(leaf)
            else:
                dir_node: dict[str, Any] | None = None
                for c in children:
                    if c.get("kind") == "dir" and c.get("name") == seg:
                        dir_node = c
                        break
                if dir_node is None:
                    dir_path = "/".join(acc)
                    dir_node = {
                        "kind": "dir",
                        "name": seg,
                        "path": dir_path,
                        "children": [],
                    }
                    children.append(dir_node)
                node = dir_node

    def sort_children(n: dict[str, Any]) -> None:
        ch = n.get("children")
        if not isinstance(ch, list):
            return
        for c in ch:
            if isinstance(c, dict) and c.get("kind") == "dir":
                sort_children(c)
        ch.sort(
            key=lambda x: (
                0 if (isinstance(x, dict) and x.get("kind") == "dir") else 1,
                str(x.get("name", "")).lower() if isinstance(x, dict) else "",
            )
        )

    sort_children(root)
    return root


# ---------------------------------------------------------------------------
# Chat sync helpers
# ---------------------------------------------------------------------------

def _is_progress_stub(message: str) -> bool:
    msg = (message or "").strip().lower()
    if not msg:
        return False
    markers = (
        "llm request accepted",
        "preparing response",
        "response in progress",
        "\u23f3",
    )
    return any(marker in msg for marker in markers)


def _extract_latest_message(payload: Any) -> str | None:
    latest_rank: int | None = None
    latest_message: str | None = None

    def walk(node: Any) -> None:
        nonlocal latest_rank, latest_message
        if isinstance(node, dict):
            msg = node.get("message")
            if isinstance(msg, str):
                rank_raw = node.get("id", node.get("post_id", node.get("timestamp", 0)))
                try:
                    rank = int(rank_raw)
                except Exception:
                    rank = 0
                if latest_rank is None or rank >= latest_rank:
                    latest_rank = rank
                    latest_message = msg
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return latest_message
