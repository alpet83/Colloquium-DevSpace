# mcp_gitbash_stdio.py — minimal MCP server: run commands via Git Bash on Windows
#
# Stdio MCP is request/response: there is no real TTY for full-screen tools (vim, top, ssh -t).
# Use this for bash pipelines, git, rg, etc. Optional one-shot stdin suits non-TUI prompts.
#
# Bash resolution (Windows): GIT_BASH_EXE if set → registry GitForWindows\InstallPath →
#   PATH (which bash) → common install dirs (Program Files\Git, Scoop, etc.) → "bash".
# Optional override: env GIT_BASH_EXE = full path to bash.exe
# Requires: pip install mcp
#
# mcp.json example (no env needed if Git for Windows is installed normally):
#   "gitbash": {
#     "type": "stdio",
#     "command": "C:\\\\Apps\\\\Python3\\\\python.exe",
#     "args": ["P:\\\\opt\\\\docker\\\\cqds\\\\mcp-tools\\\\mcp_gitbash_stdio.py"]
#   }

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from mcp.server import Server  # type: ignore[import]
from mcp.server.stdio import stdio_server  # type: ignore[import]
from mcp.types import CallToolResult, TextContent, Tool  # type: ignore[import]

server = Server("cqds-gitbash")


def _bash_under_git_root(install_path: str) -> str | None:
    root = Path(install_path.strip().strip('"'))
    if not root.parts:
        return None
    if root.name.lower() == "bash.exe" and root.is_file():
        return str(root.resolve())
    for rel in ("bin/bash.exe", "usr/bin/bash.exe"):
        candidate = root / rel
        if candidate.is_file():
            return str(candidate.resolve())
    return None


def _bash_from_git_for_windows_registry() -> str | None:
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:
        return None

    for hkey, subkey in (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\GitForWindows"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\GitForWindows"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\GitForWindows"),
    ):
        try:
            with winreg.OpenKey(hkey, subkey) as key:
                install_path, _ = winreg.QueryValueEx(key, "InstallPath")
        except OSError:
            continue
        if isinstance(install_path, str):
            hit = _bash_under_git_root(install_path)
            if hit:
                return hit
    return None


def _windows_git_bash_candidates() -> list[Path]:
    paths: list[Path] = []
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pfx86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    for base in (pf, pfx86):
        paths.append(Path(base) / "Git" / "bin" / "bash.exe")
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        paths.append(Path(local) / "Programs" / "Git" / "bin" / "bash.exe")
    profile = os.environ.get("USERPROFILE", "")
    if profile:
        paths.append(
            Path(profile) / "scoop" / "apps" / "git" / "current" / "bin" / "bash.exe"
        )
        paths.append(
            Path(profile)
            / "AppData"
            / "Local"
            / "Programs"
            / "Git"
            / "bin"
            / "bash.exe"
        )
    return paths


def _resolve_bash() -> str:
    env = (os.environ.get("GIT_BASH_EXE") or "").strip()
    if env:
        p = Path(env)
        if p.is_file():
            return str(p.resolve())

    if sys.platform == "win32":
        reg = _bash_from_git_for_windows_registry()
        if reg:
            return reg
        w = shutil.which("bash")
        if w:
            wp = Path(w)
            if wp.is_file():
                return str(wp.resolve())
        for cand in _windows_git_bash_candidates():
            if cand.is_file():
                return str(cand.resolve())
        return "bash"

    w = shutil.which("bash")
    if w:
        return w
    return "/bin/bash"


async def _kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return

    if sys.platform == "win32":
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(proc.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        except Exception:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
    else:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()

    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(proc.wait(), timeout=5)


_COMMIT_PROTOCOL_TEMPLATE = """\
=== COMMIT PROTOCOL WARNING ===
This tool enforces the developer commit workflow.  All commits MUST follow
the protocol described in the project documentation.

Before proceeding, read the full protocol file:
  {protocol_path}

Use read_file on that path now if you have not already done so.
Forbidden shortcuts: raw git add/commit outside this tool, ACL changes
on the git mirror, editing the git mirror directly.
================================
"""

_PROTOCOL_DOC_RELATIVE = "docs/COMMIT_PROTOCOL.md"

_COMMIT_PREPARE_LOCATIONS: list[str] = [
    "scripts/commit_prepare.py",
    "commit_prepare.py",
]


def _find_commit_prepare(repo_path: str) -> str | None:
    for rel in _COMMIT_PREPARE_LOCATIONS:
        candidate = Path(repo_path) / rel
        if candidate.is_file():
            return str(candidate.resolve())
    return None


def _build_protocol_warning(repo_path: str) -> str:
    protocol_path = str(Path(repo_path) / _PROTOCOL_DOC_RELATIVE)
    return _COMMIT_PROTOCOL_TEMPLATE.format(protocol_path=protocol_path)


TOOLS: list[Tool] = [
    Tool(
        name="git_write_file",
        description=(
            "Write text content to a file via Git Bash, respecting .gitattributes EOL rules "
            "from the target file's directory and any parent directories. "
            "Detects encoding issues (non-UTF-8, BOM, etc.) and validates gitattributes rules. "
            "If the file existed before writing, computes a diff against the original content. "
            "Use insert_at to modify a portion of an existing file: "
            "  - null (default): replace entire file content "
            "  - {line_start: N, line_end: null}: insert at line N (pushes existing lines down) "
            "  - {line_start: N, line_end: M} where N <= M: replace lines N..M inclusive with content "
            "Lines are 1-indexed. "
            "Returns JSON: {result_code, warnings[], diff_changes[], original_encoding, applied_eol, written_bytes}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "file_name": {
                    "type": "string",
                    "description": "Absolute path to the file to write.",
                },
                "content": {
                    "type": "string",
                    "description": "UTF-8 text content to write.",
                },
                "insert_at": {
                    "type": ["object", "null"],
                    "description": "Insert/replace at specific line range. null = replace entire file.",
                    "properties": {
                        "line_start": {
                            "type": "integer",
                            "description": "1-indexed start line. 1 = before first line.",
                        },
                        "line_end": {
                            "type": ["integer", "null"],
                            "description": "1-indexed end line (inclusive). null = insert only (no deletion).",
                        },
                    },
                    "required": ["line_start"],
                },
            },
            "required": ["file_name", "content"],
        },
    ),
    Tool(
        name="git_safe_commit",
        description=(
            "Enforces the standard two-repo commit workflow: runtime source tree → git mirror. "
            "Always use this instead of raw git commands when committing changes. "
            "Each call prepends a protocol warning with the path to the full protocol doc — "
            "use read_file on that path before proceeding if you have not already. "
            "Modes: 'dry_run' (default) — shows what would be synced via commit_prepare.py; "
            "'apply' — copies files to the git mirror via commit_prepare.py --apply; "
            "'commit' — runs git add -A + git commit inside the mirror (requires commit_message). "
            "Always start with dry_run, review with the user, then apply, then commit."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Absolute path to the RUNTIME source tree (contains scripts/commit_prepare.py).",
                },
                "mode": {
                    "type": "string",
                    "enum": ["dry_run", "apply", "commit"],
                    "description": "dry_run: check only (default). apply: sync to git mirror. commit: git add -A + commit.",
                    "default": "dry_run",
                },
                "commit_message": {
                    "type": "string",
                    "description": "Required for mode='commit'. Git commit message.",
                },
                "extra_args": {
                    "type": "string",
                    "description": "Additional flags forwarded to commit_prepare.py (e.g. '--strict-hash').",
                },
            },
            "required": ["repo_path"],
        },
    ),
    Tool(
        name="git_bash_exec",
        description=(
            "Run a shell command through Git Bash (bash -lc). On Windows, bash.exe is resolved via "
            "GIT_BASH_EXE, then Git For Windows registry InstallPath, then PATH, then common install paths. "
            "Not suitable for full-screen interactive TUIs (vim, ssh with pty): MCP has no persistent TTY. "
            "For prompts that only read stdin once, pass stdin_text. "
            "Returns JSON: exit_code, stdout, stderr, timed_out, bash_path."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Command string passed to bash -lc (as a single argument).",
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory (optional). Must exist.",
                },
                "stdin_text": {
                    "type": "string",
                    "description": "Optional UTF-8 text written to the process stdin (one shot).",
                },
                "timeout_sec": {
                    "type": "integer",
                    "description": "Seconds before SIGKILL (1–600, default 120).",
                    "default": 120,
                },
            },
            "required": ["command"],
        },
    ),
]


@server.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


async def _handle_git_safe_commit(arguments: dict[str, Any]) -> CallToolResult:
    repo_path = str(arguments.get("repo_path", "")).strip()
    warning = (
        _build_protocol_warning(repo_path)
        if repo_path
        else _COMMIT_PROTOCOL_TEMPLATE.format(protocol_path="<unknown>")
    )
    if not repo_path or not Path(repo_path).is_dir():
        return CallToolResult(
            isError=True,
            content=[
                TextContent(
                    type="text",
                    text=warning
                    + f"\nERROR: repo_path is not a directory: {repo_path!r}",
                )
            ],
        )

    mode = str(arguments.get("mode", "dry_run")).strip()
    if mode not in ("dry_run", "apply", "commit"):
        mode = "dry_run"

    commit_message = str(arguments.get("commit_message", "")).strip()
    extra_args = str(arguments.get("extra_args", "")).strip()

    prepare_script = _find_commit_prepare(repo_path)
    if prepare_script is None and mode in ("dry_run", "apply"):
        return CallToolResult(
            isError=True,
            content=[
                TextContent(
                    type="text",
                    text=warning
                    + f"\nERROR: commit_prepare.py not found under {repo_path!r}. Looked in: {_COMMIT_PREPARE_LOCATIONS}",
                )
            ],
        )

    bash_path = _resolve_bash()

    if mode == "commit":
        if not commit_message:
            return CallToolResult(
                isError=True,
                content=[
                    TextContent(
                        type="text",
                        text=warning
                        + "\nERROR: commit_message is required for mode='commit'.",
                    )
                ],
            )
        # Escape single quotes in message for bash
        safe_msg = commit_message.replace("'", "'\\''")
        command = (
            f"cd {shutil.quote(repo_path)} && git add -A && git commit -m '{safe_msg}'"
        )
    else:
        flags = "--apply" if mode == "apply" else ""
        if extra_args:
            flags = (flags + " " + extra_args).strip()
        python_exe = shutil.quote(sys.executable)
        script = shutil.quote(prepare_script)
        command = f"cd {shutil.quote(repo_path)} && {python_exe} {script} {flags}"

    proc = await asyncio.create_subprocess_exec(
        bash_path,
        "-lc",
        command,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=repo_path,
        env=os.environ.copy(),
    )

    timeout_event = asyncio.Event()

    async def _watchdog() -> None:
        await asyncio.sleep(120)
        if proc.returncode is None:
            timeout_event.set()
            await _kill_process_tree(proc)

    wdtask = asyncio.create_task(_watchdog(), name="git-safe-commit-watchdog")
    try:
        stdout_b, stderr_b = await proc.communicate()
    finally:
        wdtask.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await wdtask

    exit_code = proc.returncode if proc.returncode is not None else -1
    timed_out = timeout_event.is_set()

    result: dict[str, Any] = {
        "mode": mode,
        "repo_path": repo_path,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "stdout": (stdout_b or b"").decode("utf-8", errors="replace"),
        "stderr": (stderr_b or b"").decode("utf-8", errors="replace"),
    }
    text = warning + "\n" + json.dumps(result, ensure_ascii=False, indent=2)
    return CallToolResult(
        isError=exit_code != 0,
        content=[TextContent(type="text", text=text)],
    )


async def _parse_gitattributes_for_path(file_path: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    path_obj = Path(file_path).resolve()
    if path_obj.is_file():
        path_obj = path_obj.parent
    if not path_obj.is_dir():
        return attrs
    for parent in [path_obj] + list(path_obj.parents):
        gitattributes = parent / ".gitattributes"
        if gitattributes.is_file():
            try:
                for line in gitattributes.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split(maxsplit=1)
                    if len(parts) == 2:
                        patterns, rule_str = parts
                        pattern = patterns.split()[0] if patterns.split() else ""
                        for rule in rule_str.split():
                            if "=" in rule:
                                key, val = rule.split("=", 1)
                                attrs[key.strip()] = val.strip()
            except OSError:
                pass
    return attrs


async def _run_git_diff(
    file_path: str, new_content: str, old_content: str
) -> list[str]:
    bash_path = _resolve_bash()
    tmp_new = Path(file_path).resolve().parent / f".git_write_tmp_{os.getpid()}.new"
    tmp_orig = Path(file_path).resolve().parent / f".git_write_tmp_{os.getpid()}.orig"
    try:
        tmp_new.write_bytes(new_content.encode("utf-8", errors="replace"))
        tmp_orig.write_bytes(old_content.encode("utf-8", errors="replace"))
        command = f"cd {str(Path(file_path).resolve().parent)!r} && git diff --no-color --no-ext-diff .git_write_tmp_{os.getpid()}.orig .git_write_tmp_{os.getpid()}.new 2>/dev/null || diff -u .git_write_tmp_{os.getpid()}.orig .git_write_tmp_{os.getpid()}.new 2>/dev/null || true"
        proc = await asyncio.create_subprocess_exec(
            bash_path,
            "-lc",
            command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, _ = await proc.communicate()
        diff_output = (stdout_b or b"").decode("utf-8", errors="replace")
        lines = diff_output.splitlines()
        filtered = [ln for ln in lines if ".git_write_tmp_" not in ln]
        return filtered
    finally:
        for tmp in (tmp_new, tmp_orig):
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


async def _handle_git_write_file(arguments: dict[str, Any]) -> CallToolResult:
    file_name = str(arguments.get("file_name", "")).strip()
    content = str(arguments.get("content", ""))
    insert_at_raw = arguments.get("insert_at")

    if not file_name:
        result: dict[str, Any] = {
            "result_code": "error",
            "warnings": [],
            "diff_changes": [],
            "error": "file_name is required",
        }
        return CallToolResult(
            isError=True,
            content=[
                TextContent(
                    type="text", text=json.dumps(result, ensure_ascii=False, indent=2)
                )
            ],
        )

    file_path = Path(file_name).resolve()
    parent_dir = file_path.parent
    warnings: list[str] = []
    applied_eol = "lf"
    original_encoding = "utf-8"
    diff_changes: list[str] = []
    original_content: str | None = None
    file_existed = file_path.is_file()

    if file_existed:
        try:
            raw_bytes = file_path.read_bytes()
            if raw_bytes.startswith(b"\xef\xbb\xbf"):
                original_encoding = "utf-8-bom"
                warnings.append("File has UTF-8 BOM; will be stripped on write")
            elif raw_bytes.startswith(b"\xff\xfe"):
                original_encoding = "utf-16-le-bom"
                warnings.append("File has UTF-16-LE BOM; converting to UTF-8")
            elif raw_bytes.startswith(b"\xfe\xff"):
                original_encoding = "utf-16-be-bom"
                warnings.append("File has UTF-16-BE BOM; converting to UTF-8")
            elif raw_bytes.startswith(b"\x00\x00"):
                original_encoding = "utf-32"
                warnings.append(
                    "File appears to be UTF-32; treating as binary and skipping"
                )
            else:
                for enc in ("utf-8", "cp1251", "latin-1", "cp866"):
                    try:
                        raw_bytes.decode(enc)
                        original_encoding = enc
                        break
                    except UnicodeDecodeError:
                        continue
            original_content = raw_bytes.decode(original_encoding, errors="replace")
        except OSError as e:
            warnings.append(f"Could not read original file: {e}")

    git_attrs = await _parse_gitattributes_for_path(str(file_path))
    eol_rule = git_attrs.get("eol", "")
    if eol_rule == "crlf":
        applied_eol = "crlf"
    elif eol_rule == "lf":
        applied_eol = "lf"
    elif file_path.suffix in {
        ".py",
        ".js",
        ".ts",
        ".jsx",
        ".tsx",
        ".sh",
        ".bash",
        ".zsh",
        ".yml",
        ".yaml",
        ".json",
        ".md",
        ".txt",
        ".gitignore",
        ".gitattributes",
        ".editorconfig",
    }:
        applied_eol = "lf"
    elif file_path.suffix in {".bat", ".cmd"}:
        applied_eol = "crlf"
    if eol_rule:
        warnings.append(
            f"Applied .gitattributes eol={eol_rule} (resolved: {applied_eol})"
        )

    if applied_eol == "crlf":
        final_content = content.replace("\r\n", "\n").replace("\n", "\r\n")
    else:
        final_content = content.replace("\r\n", "\n")

    has_invalid_chars = False
    for ch in final_content:
        code = ord(ch)
        if code > 0x10FFFF:
            has_invalid_chars = True
            warnings.append(f"Invalid Unicode character U+{code:04X} will be replaced")
    if has_invalid_chars:
        final_content = final_content.encode("utf-8", errors="replace").decode(
            "utf-8", errors="replace"
        )

    if "\t" in final_content:
        warnings.append(
            "Content contains tabs; consider using spaces for consistent indentation"
        )
    if "\r" in final_content and "\n" not in final_content.replace("\r", ""):
        warnings.append("Content contains bare CR characters; LF or CRLF expected")
    trailing = final_content.rstrip("\r\n")
    if len(final_content) != len(trailing) + (len(final_content) - len(trailing)):
        pass
    if final_content.endswith(" ") or final_content.endswith("\t"):
        warnings.append("File ends with trailing whitespace")

    insert_at = insert_at_raw
    if insert_at is not None:
        if not isinstance(insert_at, dict):
            warnings.append("insert_at must be an object with line_start; ignoring")
            insert_at = None
        else:
            line_start = insert_at.get("line_start")
            line_end = insert_at.get("line_end")
            if not isinstance(line_start, int) or line_start < 1:
                warnings.append(
                    "insert_at.line_start must be a positive integer; ignoring insert_at"
                )
                insert_at = None
            elif line_end is not None and not isinstance(line_end, int):
                warnings.append(
                    "insert_at.line_end must be integer or null; ignoring line_end"
                )
                insert_at = {"line_start": line_start, "line_end": None}
            elif line_end is not None and line_end < line_start:
                warnings.append(
                    f"insert_at.line_end ({line_end}) < line_start ({line_start}); treating as insert-only"
                )
                insert_at = {"line_start": line_start, "line_end": None}

    if insert_at is not None and original_content is not None:
        original_lines = original_content.splitlines(keepends=True)
        ls = insert_at["line_start"]
        le = insert_at.get("line_end")
        max_line = len(original_lines)
        ls = max(1, min(ls, max_line + 1))
        idx_start = ls - 1
        if le is not None:
            le = max(ls, min(le, max_line))
            delete_count = le - ls + 1
            if delete_count > 0:
                warnings.append(f"Replacing {delete_count} line(s) at lines {ls}..{le}")
            idx_end = le
            before = original_lines[:idx_start]
            after = original_lines[idx_end:]
        else:
            before = original_lines[:idx_start]
            after = original_lines[idx_start:]
        new_piece = (
            final_content
            if final_content.endswith(("\n", "\r"))
            else (final_content + ("\n" if applied_eol == "lf" else "\r\n"))
        )
        combined = "".join(before) + new_piece + "".join(after)
        final_content = combined
    elif insert_at is not None:
        warnings.append("insert_at specified but file did not exist; creating new file")

    try:
        parent_dir.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(final_content.encode("utf-8", errors="replace"))
    except OSError as e:
        result = {
            "result_code": "error",
            "warnings": warnings,
            "diff_changes": [],
            "error": f"Failed to write file: {e}",
        }
        return CallToolResult(
            isError=True,
            content=[
                TextContent(
                    type="text", text=json.dumps(result, ensure_ascii=False, indent=2)
                )
            ],
        )

    if original_content is not None and original_content != final_content:
        diff_lines = await _run_git_diff(
            str(file_path), final_content, original_content
        )
        diff_changes = diff_lines

    written_bytes = len(final_content.encode("utf-8", errors="replace"))
    result = {
        "result_code": "success",
        "warnings": warnings,
        "diff_changes": diff_changes,
        "original_encoding": original_encoding,
        "applied_eol": applied_eol,
        "written_bytes": written_bytes,
        "file_existed": file_existed,
        "insert_at": {
            "line_start": insert_at["line_start"] if insert_at else None,
            "line_end": insert_at.get("line_end") if insert_at else None,
        }
        if insert_at
        else None,
    }
    return CallToolResult(
        isError=False,
        content=[
            TextContent(
                type="text", text=json.dumps(result, ensure_ascii=False, indent=2)
            )
        ],
    )


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
    if name == "git_safe_commit":
        return await _handle_git_safe_commit(arguments)
    if name == "git_write_file":
        return await _handle_git_write_file(arguments)
    if name != "git_bash_exec":
        return CallToolResult(
            isError=True,
            content=[TextContent(type="text", text=f"Unknown tool: {name}")],
        )

    command = str(arguments.get("command", "")).strip()
    if not command:
        return CallToolResult(
            isError=True,
            content=[TextContent(type="text", text="command must be non-empty")],
        )

    cwd_raw = arguments.get("cwd")
    cwd = str(cwd_raw).strip() if cwd_raw else None
    if cwd and not Path(cwd).is_dir():
        return CallToolResult(
            isError=True,
            content=[TextContent(type="text", text=f"cwd is not a directory: {cwd}")],
        )

    timeout_sec = int(arguments.get("timeout_sec", 120))
    timeout_sec = max(1, min(timeout_sec, 600))

    stdin_text = arguments.get("stdin_text")
    stdin_bytes = None
    if stdin_text is not None:
        stdin_bytes = str(stdin_text).encode("utf-8", errors="replace")

    bash_path = _resolve_bash()
    proc = await asyncio.create_subprocess_exec(
        bash_path,
        "-lc",
        command,
        stdin=asyncio.subprocess.PIPE
        if stdin_bytes is not None
        else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd or None,
        env=os.environ.copy(),
    )

    timeout_event = asyncio.Event()

    async def timeout_watchdog() -> None:
        await asyncio.sleep(timeout_sec)
        if proc.returncode is None:
            timeout_event.set()
            await _kill_process_tree(proc)

    watchdog_task = asyncio.create_task(
        timeout_watchdog(), name="gitbash-timeout-watchdog"
    )

    timed_out = False
    try:
        stdout_b, stderr_b = await proc.communicate(input=stdin_bytes)
        timed_out = timeout_event.is_set()
    finally:
        watchdog_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watchdog_task

    exit_code = proc.returncode
    if exit_code is None:
        exit_code = -1

    out: dict[str, Any] = {
        "exit_code": exit_code,
        "stdout": (stdout_b or b"").decode("utf-8", errors="replace"),
        "stderr": (stderr_b or b"").decode("utf-8", errors="replace"),
        "timed_out": timed_out,
        "bash_path": bash_path,
    }
    text = json.dumps(out, ensure_ascii=False, indent=2)
    return CallToolResult(content=[TextContent(type="text", text=text)])


async def _run() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
