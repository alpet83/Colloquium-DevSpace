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
import datetime
import json
import os
import secrets
import shlex
import shutil
import sys
import time
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
_DEFAULT_PROJECTS_ROOT_FALLBACK = "/opt/projects"


def _runtime_projects_root() -> str:
    return (
        (os.environ.get("RUNTIME_PROJECTS_ROOT", "") or "").strip()
        or _DEFAULT_PROJECTS_ROOT_FALLBACK
    )


_DEFAULT_PUBLIC_MIRROR_ROOT = "P:/GitHub"


def _public_mirror_root() -> str:
    return (
        (os.environ.get("PUBLIC_MIRROR_ROOT", "") or "").strip()
        or _DEFAULT_PUBLIC_MIRROR_ROOT
    )


def _repo_is_under_public_mirror_root(repo_path: str) -> bool:
    """True if repo_path is under the public git mirror root (e.g. P:/GitHub/...)."""
    try:
        root = Path(_public_mirror_root()).resolve()
        rp = Path(repo_path).resolve()
        rp.relative_to(root)
        return True
    except (ValueError, OSError):
        return False


def _repo_is_under_runtime_projects_root(repo_path: str) -> bool:
    """True if repo_path is the runtime/deploy tree under RUNTIME_PROJECTS_ROOT (not the public mirror)."""
    try:
        rt = Path(_runtime_projects_root()).resolve()
        rp = Path(repo_path).resolve()
        rp.relative_to(rt)
        return True
    except (ValueError, OSError):
        return False


def _report_target_hint(repo_path: str) -> str | None:
    """Best-effort mirror target hint from commit_prepare report (legacy field)."""
    for rel in ("scripts/commit_prepare_report.json", "commit_prepare_report.json"):
        report = Path(repo_path) / rel
        if not report.is_file():
            continue
        try:
            data = json.loads(
                report.read_text(encoding="utf-8", errors="replace")
            )
        except (OSError, json.JSONDecodeError):
            continue
        target = data.get("target")
        if isinstance(target, str) and target.strip():
            return target.strip()
    return None


def _report_public_repo(runtime_repo_path: str) -> str | None:
    """Resolve bound public repo from commit_prepare report near runtime path."""
    for rel in ("scripts/commit_prepare_report.json", "commit_prepare_report.json"):
        report = Path(runtime_repo_path) / rel
        if not report.is_file():
            continue
        try:
            data = json.loads(report.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue

        explicit = data.get("public_commit_repo")
        if isinstance(explicit, str) and explicit.strip():
            return str(Path(explicit.strip()).resolve())

        # Backward compatibility fallback: infer from mapping like ".../src" -> repo root.
        mappings = data.get("mappings")
        if isinstance(mappings, list):
            for item in mappings:
                if not isinstance(item, dict):
                    continue
                if str(item.get("name", "")).strip() != "main_src":
                    continue
                dst = str(item.get("dst", "")).strip()
                if not dst:
                    continue
                dst_path = Path(dst).resolve()
                if dst_path.name.lower() == "src":
                    return str(dst_path.parent)
    return None


_COMMIT_PREPARE_LOCATIONS: list[str] = [
    "scripts/commit_prepare.py",
    "project/scripts/commit_prepare.py",
    "projects/scripts/commit_prepare.py",
    "commit_prepare.py",
]
_LAST_DRY_RUN_TS_BY_REPO: dict[str, float] = {}
_LAST_DRY_RUN_TOKEN_BY_REPO: dict[str, str] = {}
# Runtime repo key -> bound public repo key, learned from commit_prepare report.
_REPO_MAP: dict[str, str] = {}
# Set of allowed public repo keys discovered from recent dry_run/apply reports.
_PUB_REPOS: set[str] = set()
_APPLY_DRY_RUN_TTL_SEC = 300


def _repo_key(path: str) -> str:
    return str(Path(path).resolve()).lower()


async def _git_is_clean(repo_path: str, bash_path: str) -> bool | None:
    command = f"cd {shlex.quote(repo_path)} && git status --porcelain"
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
    stdout_b, _ = await proc.communicate()
    if proc.returncode != 0:
        return None
    return not bool((stdout_b or b"").decode("utf-8", errors="replace").strip())


def _drop_binding_for_public_repo(pub_key: str) -> None:
    _PUB_REPOS.discard(pub_key)
    runtime_keys = [rk for rk, pk in _REPO_MAP.items() if pk == pub_key]
    for rk in runtime_keys:
        _REPO_MAP.pop(rk, None)
        _LAST_DRY_RUN_TS_BY_REPO.pop(rk, None)
        _LAST_DRY_RUN_TOKEN_BY_REPO.pop(rk, None)


def _find_commit_prepare(repo_path: str) -> str | None:
    for rel in _COMMIT_PREPARE_LOCATIONS:
        candidate = Path(repo_path) / rel
        if candidate.is_file():
            return str(candidate.resolve())
    return None


async def _find_commit_prepare_via_bash_find(
    repo_path: str, bash_path: str
) -> str | None:
    # Fallback scan for projects that keep commit_prepare.py in non-standard layout.
    command = (
        f"cd {shlex.quote(repo_path)} && "
        "find . -maxdepth 6 -type f -name commit_prepare.py "
        "! -path '*/.git/*' ! -path '*/node_modules/*' | head -n 1"
    )
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
    stdout_b, _ = await proc.communicate()
    if proc.returncode != 0:
        return None
    hit = (stdout_b or b"").decode("utf-8", errors="replace").strip()
    if not hit:
        return None
    if hit.startswith("./"):
        hit = hit[2:]
    candidate = (Path(repo_path) / hit).resolve()
    if candidate.is_file():
        return str(candidate)
    return None


def _build_protocol_warning(repo_path: str) -> str:
    protocol_path = str(Path(repo_path) / _PROTOCOL_DOC_RELATIVE)
    return _COMMIT_PROTOCOL_TEMPLATE.format(protocol_path=protocol_path)


TOOLS: list[Tool] = [
    Tool(
        name="git_read_file",
        description=(
            "Read text file with optional line slices and metadata. "
            "Default pagination: if line_slices is omitted, reads up to 50 lines. "
            "For log-like files it automatically reads the tail (last lines), otherwise head (first lines). "
            "If the resulting text still exceeds max_content_chars (~20k tokens heuristic), "
            "the line window is shrunk automatically (default mode only). "
            "Optional wrap_lines splits very long physical lines at wrap_width with a rare marker + newline. "
            "Set output_mode='text' for plain text only (no JSON wrapper)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "file_name": {
                    "type": "string",
                    "description": "Absolute path to the file to read.",
                },
                "line_slices": {
                    "type": "array",
                    "description": (
                        "Optional list of 1-indexed inclusive ranges in format 'start:end'. "
                        "If omitted, a single default slice is used (default page_lines=50). "
                        "For log-like files the default slice is tail, otherwise head. "
                        "Examples: ['1:20', '200:260']."
                    ),
                    "items": {"type": "string"},
                },
                "page_lines": {
                    "type": "integer",
                    "default": 50,
                    "description": (
                        "When line_slices is omitted: number of lines in default page (50 by default). "
                        "Clamped to file length. Ignored when line_slices is provided."
                    ),
                },
                "max_content_chars": {
                    "type": "integer",
                    "default": 72000,
                    "description": (
                        "Soft cap on total characters across all returned slice texts after wrapping. "
                        "Default ~72k chars (~18k-20k tokens rough heuristic). "
                        "When line_slices was omitted (default pagination), the tool shrinks the "
                        "line end until under this cap; then may hard-truncate a single huge line."
                    ),
                },
                "wrap_lines": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "If true, split each physical line longer than wrap_width into chunks, "
                        "joined with wrap_marker and newlines (helps minified one-line JS)."
                    ),
                },
                "wrap_width": {
                    "type": "integer",
                    "default": 160,
                    "description": "Max characters per chunk when wrap_lines is true (minimum 40).",
                },
                "wrap_marker": {
                    "type": "string",
                    "default": "⦚",
                    "description": "Separator inserted between wrapped chunks of one logical line.",
                },
                "output_mode": {
                    "type": "string",
                    "enum": ["json", "text"],
                    "default": "json",
                    "description": (
                        "json: metadata + slices in JSON. "
                        "text: only selected text, no JSON wrapper."
                    ),
                },
                "encoding": {
                    "type": "string",
                    "default": "utf-8",
                    "description": "Text encoding for read (default utf-8).",
                },
            },
            "required": ["file_name"],
        },
    ),
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
            "Two-tree workflow (runtime → public mirror) via MCP only. "
            "Each response prepends COMMIT_PROTOCOL — read that file first. "
            "Modes: dry_run (default) and apply use repo_path = RUNTIME/deploy tree only "
            "(never paths under the public mirror root, e.g. P:/GitHub). "
            "mode=commit with repo_path under the public mirror requires public_repo=true; "
            "local commits on runtime use public_repo=false (default). "
            "public_repo=true is invalid for dry_run/apply. "
            "apply requires a recent successful dry_run and apply_token. "
            "For status, use git_repo_status — avoid ad-hoc terminal git. "
            "Confirmation: confirm token or use_ui_prompt + ui_confirm_command for apply/commit."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Absolute path to the RUNTIME source tree (contains scripts/commit_prepare.py). Defaults to RUNTIME_PROJECTS_ROOT or /opt/projects if omitted.",
                },
                "public_repo": {
                    "type": "boolean",
                    "description": "Only for mode=commit on repo_path under PUBLIC_MIRROR_ROOT (e.g. P:/GitHub). Must be false for dry_run, apply, and for local runtime commits.",
                    "default": False,
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
                "commit_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of file paths to include in mode='commit' (relative to repo_path). If omitted or empty, all changes are included.",
                },
                "extra_args": {
                    "type": "string",
                    "description": "Additional flags forwarded to the prepare step (dry_run/apply only).",
                },
                "apply_token": {
                    "type": "string",
                    "description": "Required for mode='apply'. Must match dry_run_token returned by the latest successful dry_run for this repo (TTL 5 min).",
                },
                "require_confirmation": {
                    "type": "boolean",
                    "description": "If true (default), apply/commit require explicit approval (chat token or UI command).",
                    "default": True,
                },
                "confirm": {
                    "type": "string",
                    "description": "Chat confirmation token for apply/commit. Must be exactly 'I_UNDERSTAND_AND_APPROVE'.",
                },
                "use_ui_prompt": {
                    "type": "boolean",
                    "description": "If true, run `ui_confirm_command` before apply/commit. Exit code 0 means approved.",
                    "default": False,
                },
                "ui_confirm_command": {
                    "type": "string",
                    "description": "Command passed to bash -lc for local confirmation UI/script. Non-zero exit aborts operation.",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="git_repo_status",
        description=(
            "Run `git status` in a repository via Git Bash. "
            "Use instead of opening a shell for git status. "
            "repo_path must be a directory containing .git (file or dir)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Absolute path to the git working tree.",
                },
                "short_format": {
                    "type": "boolean",
                    "description": "If true, pass --short.",
                    "default": False,
                },
                "show_branch": {
                    "type": "boolean",
                    "description": "If true, pass -b (meaningful with --short).",
                    "default": False,
                },
                "porcelain": {
                    "type": "string",
                    "enum": ["", "v1", "v2"],
                    "description": "v1: --porcelain; v2: --porcelain=v2; empty: default human-readable.",
                    "default": "",
                },
                "untracked_files": {
                    "type": "string",
                    "enum": ["all", "normal", "no"],
                    "description": "Maps to --untracked-files=…",
                    "default": "all",
                },
                "ignored": {
                    "type": "boolean",
                    "description": "If true, pass --ignored.",
                    "default": False,
                },
            },
            "required": ["repo_path"],
        },
    ),
    Tool(
        name="git_list_runtime_deploys",
        description=(
            "List runtime deploy directories under a root path. "
            "A runtime deploy is detected as a directory containing a '.git' subdirectory. "
            "Defaults root_dir to RUNTIME_PROJECTS_ROOT or /opt/projects."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "root_dir": {
                    "type": "string",
                    "description": "Root directory to scan recursively (default RUNTIME_PROJECTS_ROOT or /opt/projects).",
                    "default": _DEFAULT_PROJECTS_ROOT_FALLBACK,
                },
                "max_depth": {
                    "type": "integer",
                    "description": "Maximum recursion depth from root_dir (default 4, max 12).",
                    "default": 4,
                },
            },
            "required": [],
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
    def _as_bool(value: Any, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in ("1", "true", "yes", "on"):
                return True
            if lowered in ("0", "false", "no", "off"):
                return False
        return default

    repo_path = str(arguments.get("repo_path", "")).strip() or _runtime_projects_root()
    public_repo = _as_bool(arguments.get("public_repo"), False)
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

    mirror_root = str(Path(_public_mirror_root()).resolve())
    if public_repo and mode in ("dry_run", "apply"):
        return CallToolResult(
            isError=True,
            content=[
                TextContent(
                    type="text",
                    text=warning
                    + "\nERROR: public_repo=true is only valid for mode=commit on a repo_path under "
                    + f"PUBLIC_MIRROR_ROOT ({mirror_root}).\n"
                    + "For dry_run and apply use public_repo=false (default) and repo_path = runtime/deploy tree.",
                )
            ],
        )
    if mode in ("dry_run", "apply") and _repo_is_under_public_mirror_root(repo_path):
        return CallToolResult(
            isError=True,
            content=[
                TextContent(
                    type="text",
                    text=warning
                    + "\nERROR: dry_run and apply must use the runtime/deploy repo_path, not a path under "
                    + f"PUBLIC_MIRROR_ROOT ({mirror_root}).\n"
                    + "Use git_repo_status for mirror working tree state.",
                )
            ],
        )

    commit_message = str(arguments.get("commit_message", "")).strip()
    commit_files_raw = arguments.get("commit_files")
    extra_args = str(arguments.get("extra_args", "")).strip()
    apply_token = str(arguments.get("apply_token", "")).strip()
    require_confirmation = _as_bool(arguments.get("require_confirmation"), True)
    confirm_token = str(arguments.get("confirm", "")).strip()
    use_ui_prompt = _as_bool(arguments.get("use_ui_prompt"), False)
    ui_confirm_command = str(arguments.get("ui_confirm_command", "")).strip()
    required_confirm_token = "I_UNDERSTAND_AND_APPROVE"

    bash_path = _resolve_bash()

    prepare_script = _find_commit_prepare(repo_path)
    if prepare_script is None and mode in ("dry_run", "apply"):
        prepare_script = await _find_commit_prepare_via_bash_find(repo_path, bash_path)
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

    confirmation_info: dict[str, Any] = {
        "required": require_confirmation and mode in ("apply", "commit"),
        "granted": mode == "dry_run",
        "method": "not-required" if mode == "dry_run" else "pending",
    }

    if mode in ("apply", "commit") and require_confirmation:
        if use_ui_prompt:
            if not ui_confirm_command:
                return CallToolResult(
                    isError=True,
                    content=[
                        TextContent(
                            type="text",
                            text=warning
                            + "\nERROR: ui_confirm_command is required when use_ui_prompt=true.\n"
                            + "No changes were applied. Ask the user for explicit confirmation and call again.",
                        )
                    ],
                )
            confirm_proc = await asyncio.create_subprocess_exec(
                bash_path,
                "-lc",
                ui_confirm_command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=repo_path,
                env=os.environ.copy(),
            )
            confirm_stdout_b, confirm_stderr_b = await confirm_proc.communicate()
            confirm_exit_code = (
                confirm_proc.returncode if confirm_proc.returncode is not None else -1
            )
            confirmation_info = {
                "required": True,
                "granted": confirm_exit_code == 0,
                "method": "ui_prompt",
                "ui_exit_code": confirm_exit_code,
                "ui_stdout": (confirm_stdout_b or b"").decode("utf-8", errors="replace"),
                "ui_stderr": (confirm_stderr_b or b"").decode("utf-8", errors="replace"),
            }
            if confirm_exit_code != 0:
                result = {
                    "mode": mode,
                    "repo_path": repo_path,
                    "aborted_before_exec": True,
                    "reason": "ui_confirmation_rejected_or_failed",
                    "confirmation": confirmation_info,
                }
                text = warning + "\n" + json.dumps(result, ensure_ascii=False, indent=2)
                return CallToolResult(
                    isError=True,
                    content=[TextContent(type="text", text=text)],
                )
        else:
            if confirm_token != required_confirm_token:
                return CallToolResult(
                    isError=True,
                    content=[
                        TextContent(
                            type="text",
                            text=warning
                            + "\nERROR: non-dry-run mode requires explicit confirmation.\n"
                            + f"Pass confirm='{required_confirm_token}' after user approval,\n"
                            + "or use use_ui_prompt=true with ui_confirm_command.\n"
                            + "No changes were applied.",
                        )
                    ],
                )
            confirmation_info = {
                "required": True,
                "granted": True,
                "method": "chat_token",
                "token_ok": True,
            }

    repo_key = _repo_key(repo_path)
    repo_resolved = str(Path(repo_path).resolve())
    now_ts = time.monotonic()
    dry_run_guard: dict[str, Any] = {
        "ttl_sec": _APPLY_DRY_RUN_TTL_SEC,
    }
    if mode == "apply":
        last_dry_run_ts = _LAST_DRY_RUN_TS_BY_REPO.get(repo_key)
        expected_apply_token = _LAST_DRY_RUN_TOKEN_BY_REPO.get(repo_key, "")
        if last_dry_run_ts is None:
            dry_run_guard.update(
                {
                    "has_recent_dry_run": False,
                    "age_sec": None,
                    "blocked": True,
                    "reason": "missing_dry_run",
                    "apply_token_required": True,
                }
            )
            result = {
                "mode": mode,
                "repo_path": repo_path,
                "aborted_before_exec": True,
                "reason": "apply_requires_recent_dry_run",
                "dry_run_guard": dry_run_guard,
                "confirmation": confirmation_info,
            }
            text = warning + "\n" + json.dumps(result, ensure_ascii=False, indent=2)
            return CallToolResult(
                isError=True,
                content=[TextContent(type="text", text=text)],
            )
        age_sec = int(now_ts - last_dry_run_ts)
        if age_sec > _APPLY_DRY_RUN_TTL_SEC:
            _LAST_DRY_RUN_TOKEN_BY_REPO.pop(repo_key, None)
            dry_run_guard.update(
                {
                    "has_recent_dry_run": False,
                    "age_sec": age_sec,
                    "blocked": True,
                    "reason": "dry_run_expired",
                    "apply_token_required": True,
                }
            )
            result = {
                "mode": mode,
                "repo_path": repo_path,
                "aborted_before_exec": True,
                "reason": "apply_requires_recent_dry_run",
                "dry_run_guard": dry_run_guard,
                "confirmation": confirmation_info,
            }
            text = warning + "\n" + json.dumps(result, ensure_ascii=False, indent=2)
            return CallToolResult(
                isError=True,
                content=[TextContent(type="text", text=text)],
            )
        if not expected_apply_token or apply_token != expected_apply_token:
            dry_run_guard.update(
                {
                    "has_recent_dry_run": True,
                    "age_sec": age_sec,
                    "blocked": True,
                    "reason": "invalid_apply_token",
                    "apply_token_required": True,
                    "token_provided": bool(apply_token),
                }
            )
            result = {
                "mode": mode,
                "repo_path": repo_path,
                "aborted_before_exec": True,
                "reason": "apply_requires_valid_dry_run_token",
                "dry_run_guard": dry_run_guard,
                "confirmation": confirmation_info,
            }
            text = warning + "\n" + json.dumps(result, ensure_ascii=False, indent=2)
            return CallToolResult(
                isError=True,
                content=[TextContent(type="text", text=text)],
            )
        dry_run_guard.update(
            {
                "has_recent_dry_run": True,
                "age_sec": age_sec,
                "blocked": False,
                "reason": "ok",
                "apply_token_required": True,
            }
        )

    commit_warnings: list[str] = []
    if mode == "commit":
        commit_files: list[str] = []
        if commit_files_raw is None:
            commit_files = []
        elif isinstance(commit_files_raw, list):
            for item in commit_files_raw:
                if not isinstance(item, str):
                    return CallToolResult(
                        isError=True,
                        content=[
                            TextContent(
                                type="text",
                                text=warning
                                + "\nERROR: commit_files must be an array of strings.",
                            )
                        ],
                    )
                path_item = item.strip().replace("\\", "/")
                if not path_item:
                    continue
                if path_item.startswith("/") or ":" in path_item:
                    return CallToolResult(
                        isError=True,
                        content=[
                            TextContent(
                                type="text",
                                text=warning
                                + f"\nERROR: commit_files entry must be a relative path: {item!r}",
                            )
                        ],
                    )
                if path_item == ".." or path_item.startswith("../") or "/../" in path_item:
                    return CallToolResult(
                        isError=True,
                        content=[
                            TextContent(
                                type="text",
                                text=warning
                                + f"\nERROR: commit_files entry escapes repo root: {item!r}",
                            )
                        ],
                    )
                commit_files.append(path_item)
        else:
            return CallToolResult(
                isError=True,
                content=[
                    TextContent(
                        type="text",
                        text=warning + "\nERROR: commit_files must be an array of strings.",
                    )
                ],
            )

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
        under_mirror = _repo_is_under_public_mirror_root(repo_path)
        if under_mirror and not public_repo:
            return CallToolResult(
                isError=True,
                content=[
                    TextContent(
                        type="text",
                        text=warning
                        + "\nERROR: mode=commit on a path under PUBLIC_MIRROR_ROOT requires "
                        + "public_repo=true (explicit opt-in for public mirror checkout).\n"
                        + f"PUBLIC_MIRROR_ROOT is {mirror_root}.",
                    )
                ],
            )
        if public_repo and not under_mirror:
            mirror_hint = _report_target_hint(repo_path)
            err_body = (
                "\nERROR: public_repo=true is only allowed when repo_path is under "
                f"PUBLIC_MIRROR_ROOT ({mirror_root}).\n"
                "For a local commit on the runtime/deploy tree use public_repo=false (default).\n"
                "For a public mirror commit set repo_path to the mirror directory under that root."
            )
            if mirror_hint:
                err_body += f"\nHint (from report): mirror path may be: {mirror_hint}"
            return CallToolResult(
                isError=True,
                content=[TextContent(type="text", text=warning + err_body)],
            )
        if public_repo:
            expected_candidates = sorted(_PUB_REPOS)
            if not expected_candidates:
                repo_clean = await _git_is_clean(repo_path, bash_path)
                if repo_clean is False:
                    commit_warnings.append(
                        "WARN: commit allowed without active dry_run/apply token; change origin is unknown."
                    )
                else:
                    return CallToolResult(
                        isError=True,
                        content=[
                            TextContent(
                                type="text",
                                text=warning
                                + "\nERROR: expected public commit repository is unknown.\n"
                                + "Run dry_run/apply on the runtime repo first so commit_prepare report provides "
                                + "the bound public_commit_repo path, or ensure repo has pending changes.",
                            )
                        ],
                    )
            elif repo_resolved.lower() not in _PUB_REPOS:
                return CallToolResult(
                    isError=True,
                    content=[
                        TextContent(
                            type="text",
                            text=warning
                            + "\nERROR: repo_path for public commit does not match runtime binding.\n"
                            + f"Allowed public repo(s): {expected_candidates}\n"
                            + f"Provided repo_path: {repo_path}",
                        )
                    ],
                )
        # Escape single quotes in message for bash
        safe_msg = commit_message.replace("'", "'\\''")
        if commit_files:
            files_args = " ".join(shlex.quote(p) for p in commit_files)
            add_cmd = f"git add -- {files_args}"
            commit_scope: dict[str, Any] = {
                "mode": "partial",
                "files": commit_files,
            }
        else:
            add_cmd = "git add -A"
            commit_scope = {
                "mode": "all",
                "files": [],
            }
        command = f"cd {shlex.quote(repo_path)} && {add_cmd} && git commit -m '{safe_msg}'"
    else:
        flags = "--apply" if mode == "apply" else ""
        if extra_args:
            flags = (flags + " " + extra_args).strip()
        python_exe = shlex.quote(sys.executable)
        script = shlex.quote(prepare_script)
        command = f"cd {shlex.quote(repo_path)} && {python_exe} {script} {flags}"

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
        "public_repo": public_repo,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "stdout": (stdout_b or b"").decode("utf-8", errors="replace"),
        "stderr": (stderr_b or b"").decode("utf-8", errors="replace"),
        "confirmation": confirmation_info,
    }
    if mode == "commit":
        result["commit_scope"] = commit_scope
        if commit_warnings:
            result["warnings"] = commit_warnings
    if mode == "dry_run" and exit_code == 0:
        _LAST_DRY_RUN_TS_BY_REPO[repo_key] = now_ts
        dry_run_token = secrets.token_urlsafe(24)
        _LAST_DRY_RUN_TOKEN_BY_REPO[repo_key] = dry_run_token
        expected_public = _report_public_repo(repo_path)
        if expected_public:
            expected_key = _repo_key(expected_public)
            _REPO_MAP[repo_key] = expected_key
            _PUB_REPOS.add(expected_key)
        result["dry_run_guard"] = {
            "ttl_sec": _APPLY_DRY_RUN_TTL_SEC,
            "stored_at_monotonic": now_ts,
            "dry_run_token": dry_run_token,
            "apply_token_required": True,
            "hint": "Не забывай COMMIT_PROTOCOL, подтверждение пользователя перед коммитом обязательно!",
        }
        if expected_public:
            result["expected_public_commit_repo"] = expected_public
    elif mode == "apply":
        result["dry_run_guard"] = dry_run_guard
        expected_public = _report_public_repo(repo_path)
        if expected_public:
            expected_key = _repo_key(expected_public)
            _REPO_MAP[repo_key] = expected_key
            _PUB_REPOS.add(expected_key)
            result["expected_public_commit_repo"] = expected_public
        if exit_code == 0:
            # Keep apply token/binding for iterative fragment commits.
            # Cleanup happens when public repo becomes clean (git status --porcelain).
            bound_pub = _REPO_MAP.get(repo_key)
            if bound_pub:
                is_clean = await _git_is_clean(str(Path(bound_pub).resolve()), bash_path)
                if is_clean:
                    _drop_binding_for_public_repo(bound_pub)
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


def _parse_line_slice(value: str) -> tuple[int, int] | None:
    raw = value.strip()
    if ":" not in raw:
        return None
    left, right = raw.split(":", 1)
    left = left.strip()
    right = right.strip()
    if not left.isdigit() or not right.isdigit():
        return None
    start = int(left)
    end = int(right)
    if start < 1 or end < start:
        return None
    return (start, end)


def _estimate_tokens_chars(text: str) -> int:
    """Rough token estimate for budgeting (Latin-heavy text ~4 chars/token)."""
    if not text:
        return 0
    return (len(text) + 3) // 4


def _wrap_segment_lines(
    segment_lines: list[str], wrap: bool, width: int, marker: str
) -> str:
    if not wrap or width < 1:
        return "\n".join(segment_lines)
    width = max(40, width)
    sep = f"\n{marker}\n"
    out_chunks: list[str] = []
    for ln in segment_lines:
        if len(ln) <= width:
            out_chunks.append(ln)
            continue
        parts: list[str] = []
        for i in range(0, len(ln), width):
            parts.append(ln[i : i + width])
        out_chunks.append(sep.join(parts))
    return "\n".join(out_chunks)


def _segment_text_for_range(
    lines: list[str],
    total_lines: int,
    start: int,
    end: int,
    wrap: bool,
    wrap_width: int,
    wrap_marker: str,
) -> tuple[list[str], int, int, str]:
    bounded_start = min(start, total_lines) if total_lines > 0 else 1
    bounded_end = min(end, total_lines) if total_lines > 0 else 0
    if total_lines == 0 or bounded_end < bounded_start:
        return [], bounded_start, bounded_end, ""
    segment_lines = lines[bounded_start - 1 : bounded_end]
    text = _wrap_segment_lines(segment_lines, wrap, wrap_width, wrap_marker)
    return segment_lines, bounded_start, bounded_end, text


def _truncate_to_budget(text: str, budget: int, marker: str) -> tuple[str, bool]:
    if len(text) <= budget:
        return text, False
    note = f"\n{marker}\n[truncated: N chars omitted]"
    keep = max(0, budget - len(note))
    omitted = max(0, len(text) - keep)
    note = f"\n{marker}\n[truncated: {omitted} chars omitted]"
    keep = max(0, budget - len(note))
    return text[:keep] + note, True


def _is_log_like_file(file_path: Path) -> bool:
    suffix = file_path.suffix.lower()
    if suffix in {".log", ".logs", ".out", ".err"}:
        return True
    return "log" in file_path.name.lower()


async def _handle_git_read_file(arguments: dict[str, Any]) -> CallToolResult:
    file_name = str(arguments.get("file_name", "")).strip()
    if not file_name:
        return CallToolResult(
            isError=True,
            content=[TextContent(type="text", text="file_name is required")],
        )

    file_path = Path(file_name).resolve()
    if not file_path.is_file():
        return CallToolResult(
            isError=True,
            content=[
                TextContent(type="text", text=f"file is not found: {str(file_path)!r}")
            ],
        )

    output_mode = str(arguments.get("output_mode", "json")).strip().lower()
    if output_mode not in ("json", "text"):
        output_mode = "json"

    encoding = str(arguments.get("encoding", "utf-8")).strip() or "utf-8"
    line_slices_raw = arguments.get("line_slices")

    page_lines = int(arguments.get("page_lines", 50))
    page_lines = max(1, min(page_lines, 1_000_000))

    max_content_chars = int(arguments.get("max_content_chars", 72_000))
    max_content_chars = max(2_000, min(max_content_chars, 2_000_000))

    wrap_lines = bool(arguments.get("wrap_lines", False))
    wrap_width = int(arguments.get("wrap_width", 160))
    wrap_marker = str(arguments.get("wrap_marker", "⦚"))
    if not wrap_marker.strip():
        wrap_marker = "⦚"

    invalid_slices: list[str] = []
    slices_input: list[str] = []
    if line_slices_raw is None:
        slices_input = []
    elif isinstance(line_slices_raw, list):
        for item in line_slices_raw:
            if isinstance(item, str):
                slices_input.append(item)
            else:
                invalid_slices.append(str(item))
    else:
        return CallToolResult(
            isError=True,
            content=[
                TextContent(
                    type="text",
                    text="line_slices must be an array of strings like ['1:20', '40:60']",
                )
            ],
        )

    used_default_pagination = line_slices_raw is None

    try:
        text = file_path.read_text(encoding=encoding, errors="replace")
    except OSError as e:
        return CallToolResult(
            isError=True,
            content=[
                TextContent(type="text", text=f"failed to read file {str(file_path)!r}: {e}")
            ],
        )
    except LookupError as e:
        return CallToolResult(
            isError=True,
            content=[TextContent(type="text", text=f"unknown encoding {encoding!r}: {e}")],
        )

    lines = text.splitlines()
    total_lines = len(lines)
    stat = file_path.stat()

    parsed_ranges: list[tuple[str, int, int]] = []
    for raw_slice in slices_input:
        parsed = _parse_line_slice(raw_slice)
        if parsed is None:
            invalid_slices.append(raw_slice)
            continue
        start, end = parsed
        parsed_ranges.append((raw_slice, start, end))

    is_log_like = _is_log_like_file(file_path)

    pagination_note: dict[str, Any] = {
        "used_default_pagination": used_default_pagination,
        "page_lines": page_lines,
        "default_slice_mode": "tail" if is_log_like else "head",
        "max_content_chars": max_content_chars,
        "estimated_token_budget_approx": (max_content_chars + 3) // 4,
    }

    if not parsed_ranges:
        if total_lines <= 0:
            parsed_ranges = [("1:1", 1, 1)]
        elif is_log_like:
            end_line = total_lines
            start_line = max(1, end_line - page_lines + 1)
            parsed_ranges = [(f"{start_line}:{end_line}", start_line, end_line)]
        else:
            end_line = min(page_lines, total_lines)
            parsed_ranges = [(f"1:{end_line}", 1, end_line)]

    auto_shrank_lines = False
    hard_truncated = False

    if used_default_pagination and len(parsed_ranges) == 1:
        _label, start, end = parsed_ranges[0]
        if start <= end and total_lines > 0:
            # Keep orientation: head for regular files, tail for log-like files.
            if start == 1:
                max_end = min(end, total_lines)

                def _chars_for_end(e: int) -> int:
                    _, _, _, t = _segment_text_for_range(
                        lines, total_lines, 1, e, wrap_lines, wrap_width, wrap_marker
                    )
                    return len(t)

                lo, hi = 1, max(1, max_end)
                best = 1
                while lo <= hi:
                    mid = (lo + hi) // 2
                    if _chars_for_end(mid) <= max_content_chars:
                        best = mid
                        lo = mid + 1
                    else:
                        hi = mid - 1
                if best < max_end:
                    auto_shrank_lines = True
                parsed_ranges = [(f"1:{best}", 1, best)]
            else:
                fixed_end = min(end, total_lines)
                min_start = max(1, start)

                def _chars_for_start(s: int) -> int:
                    _, _, _, t = _segment_text_for_range(
                        lines,
                        total_lines,
                        s,
                        fixed_end,
                        wrap_lines,
                        wrap_width,
                        wrap_marker,
                    )
                    return len(t)

                lo, hi = min_start, fixed_end
                best = fixed_end
                while lo <= hi:
                    mid = (lo + hi) // 2
                    if _chars_for_start(mid) <= max_content_chars:
                        best = mid
                        hi = mid - 1
                    else:
                        lo = mid + 1
                if best > min_start:
                    auto_shrank_lines = True
                parsed_ranges = [(f"{best}:{fixed_end}", best, fixed_end)]

    selected_segments: list[dict[str, Any]] = []
    plain_text_chunks: list[str] = []

    for original, start, end in parsed_ranges:
        segment_lines, bounded_start, bounded_end, segment_text = _segment_text_for_range(
            lines, total_lines, start, end, wrap_lines, wrap_width, wrap_marker
        )
        selected_segments.append(
            {
                "requested_range": original,
                "resolved_start_line": bounded_start,
                "resolved_end_line": bounded_end,
                "line_count": len(segment_lines),
                "text": segment_text,
            }
        )
        plain_text_chunks.append(segment_text)

    total_chars = sum(len(s["text"]) for s in selected_segments)
    if total_chars > max_content_chars:
        if used_default_pagination and len(selected_segments) == 1:
            seg = selected_segments[0]
            t2, did = _truncate_to_budget(seg["text"], max_content_chars, wrap_marker)
            seg["text"] = t2
            hard_truncated = did
            plain_text_chunks = [t2]
        else:
            remaining = max_content_chars
            new_plain: list[str] = []
            for seg in selected_segments:
                cap = max(0, remaining)
                orig = seg["text"]
                if cap == 0:
                    if orig:
                        hard_truncated = True
                    seg["text"] = ""
                    new_plain.append("")
                    continue
                t2, did = _truncate_to_budget(orig, cap, wrap_marker)
                seg["text"] = t2
                if did:
                    hard_truncated = True
                new_plain.append(t2)
                remaining -= len(t2)
            plain_text_chunks = new_plain

    total_out_chars = sum(len(s["text"]) for s in selected_segments)
    pagination_note.update(
        {
            "auto_shrank_lines": auto_shrank_lines,
            "hard_truncated": hard_truncated,
            "output_chars": total_out_chars,
            "estimated_output_tokens": _estimate_tokens_chars(
                "\n".join(s["text"] for s in selected_segments)
            ),
            "wrap_lines": wrap_lines,
            "wrap_width": wrap_width if wrap_lines else None,
        }
    )

    if output_mode == "text":
        plain_text = "\n\n".join(plain_text_chunks)
        return CallToolResult(
            isError=False,
            content=[TextContent(type="text", text=plain_text)],
        )

    result: dict[str, Any] = {
        "result_code": "success",
        "file_name": str(file_path),
        "pagination": pagination_note,
        "metadata": {
            "created_at_unix": stat.st_ctime,
            "modified_at_unix": stat.st_mtime,
            "created_at_iso": datetime.datetime.fromtimestamp(stat.st_ctime).isoformat(
                timespec="seconds"
            ),
            "modified_at_iso": datetime.datetime.fromtimestamp(stat.st_mtime).isoformat(
                timespec="seconds"
            ),
            "size_bytes": stat.st_size,
            "total_lines": total_lines,
            "encoding": encoding,
        },
        "line_slices": selected_segments,
        "invalid_line_slices": invalid_slices,
    }
    return CallToolResult(
        isError=False,
        content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))],
    )


async def _handle_git_list_runtime_deploys(arguments: dict[str, Any]) -> CallToolResult:
    root_dir = str(arguments.get("root_dir", "")).strip() or _runtime_projects_root()
    max_depth_raw = int(arguments.get("max_depth", 4))
    max_depth = max(0, min(max_depth_raw, 12))

    root = Path(root_dir).resolve()
    if not root.is_dir():
        return CallToolResult(
            isError=True,
            content=[
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "result_code": "error",
                            "error": f"root_dir is not a directory: {str(root)}",
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
            ],
        )

    found: list[str] = []
    for dirpath, dirnames, _filenames in os.walk(root):
        current = Path(dirpath)
        rel = current.relative_to(root)
        depth = len(rel.parts)
        if depth > max_depth:
            dirnames[:] = []
            continue
        if ".git" in dirnames:
            found.append(str(current))
            dirnames[:] = []
            continue
        dirnames[:] = [
            d
            for d in dirnames
            if d not in (".git", "node_modules", ".venv", "__pycache__", ".cache")
        ]

    result: dict[str, Any] = {
        "result_code": "success",
        "root_dir": str(root),
        "max_depth": max_depth,
        "runtime_deploys": sorted(found),
        "count": len(found),
    }
    return CallToolResult(
        isError=False,
        content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))],
    )


async def _handle_git_repo_status(arguments: dict[str, Any]) -> CallToolResult:
    def _as_bool(value: Any, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in ("1", "true", "yes", "on"):
                return True
            if lowered in ("0", "false", "no", "off"):
                return False
        return default

    repo_path = str(arguments.get("repo_path", "")).strip()
    if not repo_path or not Path(repo_path).is_dir():
        return CallToolResult(
            isError=True,
            content=[
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "result_code": "error",
                            "error": f"repo_path is not a directory: {repo_path!r}",
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
            ],
        )
    git_meta = Path(repo_path) / ".git"
    if not git_meta.exists():
        return CallToolResult(
            isError=True,
            content=[
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "result_code": "error",
                            "error": f"not a git working tree (no .git): {repo_path!r}",
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
            ],
        )

    short = _as_bool(arguments.get("short_format"), False)
    show_branch = _as_bool(arguments.get("show_branch"), False)
    ignored = _as_bool(arguments.get("ignored"), False)
    porcelain = str(arguments.get("porcelain", "") or "").strip().lower()
    untracked = str(arguments.get("untracked_files", "all") or "all").strip().lower()
    if untracked not in ("all", "normal", "no"):
        untracked = "all"

    argv: list[str] = ["git", "status"]
    if short:
        argv.append("--short")
    if show_branch:
        argv.append("-b")
    if porcelain == "v1":
        argv.append("--porcelain")
    elif porcelain == "v2":
        argv.append("--porcelain=v2")
    argv.append(f"--untracked-files={untracked}")
    if ignored:
        argv.append("--ignored")

    inner = " ".join(shlex.quote(a) for a in argv)
    command = f"cd {shlex.quote(repo_path)} && {inner}"
    bash_path = _resolve_bash()
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
    stdout_b, stderr_b = await proc.communicate()
    exit_code = proc.returncode if proc.returncode is not None else -1
    decoded = (stdout_b or b"").decode("utf-8", errors="replace")
    nonempty_lines = [ln.strip() for ln in decoded.splitlines() if ln.strip()]
    # When show_branch=true, git status includes a leading "## <branch...>" line
    # even if there are no other changes. Treat it as header-only.
    if show_branch and nonempty_lines and nonempty_lines[0].startswith("##"):
        nonempty_lines = nonempty_lines[1:]
    clean_status = exit_code == 0 and not bool(nonempty_lines)
    repo_k = _repo_key(repo_path)
    if clean_status:
        if repo_k in _PUB_REPOS:
            _drop_binding_for_public_repo(repo_k)
        else:
            mapped_pub = _REPO_MAP.get(repo_k)
            if mapped_pub:
                maybe_clean = await _git_is_clean(str(Path(mapped_pub).resolve()), bash_path)
                if maybe_clean:
                    _drop_binding_for_public_repo(mapped_pub)

    result: dict[str, Any] = {
        "result_code": "success" if exit_code == 0 else "error",
        "repo_path": repo_path,
        "exit_code": exit_code,
        "stdout": (stdout_b or b"").decode("utf-8", errors="replace"),
        "stderr": (stderr_b or b"").decode("utf-8", errors="replace"),
        "argv": argv,
        "clean": clean_status,
    }
    return CallToolResult(
        isError=exit_code != 0,
        content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))],
    )


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
    if name == "git_safe_commit":
        return await _handle_git_safe_commit(arguments)
    if name == "git_repo_status":
        return await _handle_git_repo_status(arguments)
    if name == "git_list_runtime_deploys":
        return await _handle_git_list_runtime_deploys(arguments)
    if name == "git_read_file":
        return await _handle_git_read_file(arguments)
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
