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
        paths.append(Path(profile) / "scoop" / "apps" / "git" / "current" / "bin" / "bash.exe")
        paths.append(Path(profile) / "AppData" / "Local" / "Programs" / "Git" / "bin" / "bash.exe")
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


TOOLS: list[Tool] = [
    Tool(
        name="gitbash_exec",
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


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
    if name != "gitbash_exec":
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
        stdin=asyncio.subprocess.PIPE if stdin_bytes is not None else asyncio.subprocess.DEVNULL,
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

    watchdog_task = asyncio.create_task(timeout_watchdog(), name="gitbash-timeout-watchdog")

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
