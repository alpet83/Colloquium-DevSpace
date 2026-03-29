from __future__ import annotations

import asyncio
import os
import time
from typing import Any
import uuid

from mcp.types import CallToolResult, Tool  # type: ignore[import]

from cqds_helpers import LOGGER, _json_text, _text
from cqds_run_ctx import RunContext


_MAX_HOST_PROC_BUFF = 4 * 1024 * 1024
_MAX_HOST_PROCS = 48


async def _host_pump_stream(reader: asyncio.StreamReader | None, acc: bytearray, cap: int) -> None:
    if reader is None:
        return
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            acc.extend(chunk)
            if len(acc) > cap:
                del acc[: len(acc) - cap]
    except Exception as exc:
        LOGGER.debug("host process pump ended: %s", exc)


class HostProcRecord:
    __slots__ = (
        "proc",
        "argv_desc",
        "started",
        "stdout_acc",
        "stderr_acc",
        "pump_out",
        "pump_err",
        "ttl_task",
    )

    def __init__(self, proc: asyncio.subprocess.Process, argv_desc: str) -> None:
        self.proc = proc
        self.argv_desc = argv_desc
        self.started = time.monotonic()
        self.stdout_acc = bytearray()
        self.stderr_acc = bytearray()
        self.pump_out: asyncio.Task[None] | None = None
        self.pump_err: asyncio.Task[None] | None = None
        self.ttl_task: asyncio.Task[None] | None = None


def _host_tail_text(acc: bytearray, max_bytes: int) -> str:
    if max_bytes <= 0:
        return ""
    raw = bytes(acc[-max_bytes:]) if len(acc) > max_bytes else bytes(acc)
    return raw.decode("utf-8", errors="replace")


TOOLS: list[Tool] = [
    Tool(
        name="cq_host_process_spawn",
        description=(
            "Spawn a subprocess on the machine where this MCP server runs (local host), not in "
            "Colloquium/mcp-sandbox. Same interaction model as cq_process_spawn: use cq_host_process_io "
            "/ wait / status / kill afterward. command: shell string (asyncio.create_subprocess_shell) "
            "or argv array (create_subprocess_exec). Optional cwd, env, timeout seconds (1–7200, default 3600) "
            "after which the process is killed if still running."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "command": {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}, "minItems": 1}], "description": "Shell string or argv list."},
                "cwd": {"type": "string"},
                "env": {"type": "object"},
                "timeout": {"type": "integer", "description": "TTL in seconds; process killed if still alive (default 3600).", "default": 3600},
            },
            "required": ["command"],
        },
    ),
    Tool(
        name="cq_host_process_io",
        description=(
            "Read accumulated stdout/stderr from a cq_host_process_spawn process and optionally write "
            "to stdin. Returns text fragments (UTF-8, replacement on errors), not base64."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "process_guid": {"type": "string"},
                "input": {"type": "string", "description": "Optional plain text written to stdin."},
                "read_timeout_ms": {"type": "integer", "default": 5000},
                "max_bytes": {"type": "integer", "default": 65536},
            },
            "required": ["process_guid"],
        },
    ),
    Tool(
        name="cq_host_process_kill",
        description=(
            "Send SIGTERM or SIGKILL to a host process spawned via cq_host_process_spawn and remove "
            "it from the local registry."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "process_guid": {"type": "string"},
                "signal": {"type": "string", "enum": ["SIGTERM", "SIGKILL"], "default": "SIGTERM"},
            },
            "required": ["process_guid"],
        },
    ),
    Tool(
        name="cq_host_process_status",
        description="Status for a local host process (alive, returncode, pid, runtime_ms).",
        inputSchema={"type": "object", "properties": {"process_guid": {"type": "string"}}, "required": ["process_guid"]},
    ),
    Tool(
        name="cq_host_process_list",
        description="List subprocesses spawned via cq_host_process_spawn on this MCP host.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="cq_host_process_wait",
        description=(
            "Poll a host process for new output or exit (same semantics as cq_process_wait: "
            "any_output vs finished)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "process_guid": {"type": "string"},
                "wait_timeout_ms": {"type": "integer", "default": 30000},
                "wait_condition": {"type": "string", "enum": ["any_output", "finished"], "default": "any_output"},
            },
            "required": ["process_guid"],
        },
    ),
]


async def handle(name: str, arguments: dict[str, Any], ctx: RunContext) -> CallToolResult | None:
    host_proc_registry = ctx.host_proc_registry

    if name == "cq_host_process_spawn":
        if len(host_proc_registry) >= _MAX_HOST_PROCS:
            return _text(f"Too many host processes (max {_MAX_HOST_PROCS})")
        cmd = arguments.get("command")
        if cmd is None:
            return _text("Missing required argument: command")
        cwd = arguments.get("cwd")
        cwd_s = str(cwd) if cwd else None
        env_arg = arguments.get("env")
        env_merged = os.environ.copy()
        if isinstance(env_arg, dict):
            for key, value in env_arg.items():
                env_merged[str(key)] = str(value)
        timeout_sec = max(1, min(int(arguments.get("timeout", 3600)), 7200))

        try:
            if isinstance(cmd, str):
                proc = await asyncio.create_subprocess_shell(
                    cmd,
                    cwd=cwd_s,
                    env=env_merged,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    stdin=asyncio.subprocess.PIPE,
                )
                desc = cmd[:500]
            elif isinstance(cmd, list):
                if not cmd:
                    return _text("command array must be non-empty")
                argv = [str(x) for x in cmd]
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=cwd_s,
                    env=env_merged,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    stdin=asyncio.subprocess.PIPE,
                )
                desc = " ".join(argv)[:500]
            else:
                return _text("command must be string or array")
        except Exception as exc:
            return _text(f"spawn failed: {exc}")

        guid = str(uuid.uuid4())
        rec = HostProcRecord(proc, desc)
        rec.pump_out = asyncio.create_task(_host_pump_stream(proc.stdout, rec.stdout_acc, _MAX_HOST_PROC_BUFF))
        rec.pump_err = asyncio.create_task(_host_pump_stream(proc.stderr, rec.stderr_acc, _MAX_HOST_PROC_BUFF))

        async def _host_ttl_kill() -> None:
            await asyncio.sleep(float(timeout_sec))
            if proc.returncode is None:
                proc.kill()

        rec.ttl_task = asyncio.create_task(_host_ttl_kill())
        host_proc_registry[guid] = rec
        return _json_text({"process_guid": guid, "pid": proc.pid, "command": desc, "timeout": timeout_sec})

    if name == "cq_host_process_io":
        process_guid = str(arguments.get("process_guid") or "")
        if not process_guid:
            return _text("Missing required argument: process_guid")
        rec = host_proc_registry.get(process_guid)
        if not rec:
            return _text("Unknown process_guid")
        inp = arguments.get("input")
        if inp is not None and rec.proc.stdin:
            try:
                if not rec.proc.stdin.is_closing():
                    rec.proc.stdin.write(str(inp).encode("utf-8", errors="replace"))
                    await rec.proc.stdin.drain()
            except Exception as exc:
                max_bytes = int(arguments.get("max_bytes", 65536))
                return _json_text(
                    {
                        "error": f"stdin write failed: {exc}",
                        "stdout_fragment": _host_tail_text(rec.stdout_acc, max_bytes),
                        "stderr_fragment": _host_tail_text(rec.stderr_acc, max_bytes),
                        "alive": rec.proc.returncode is None,
                        "returncode": rec.proc.returncode,
                    }
                )
        max_bytes = int(arguments.get("max_bytes", 65536))
        read_timeout_ms = max(0, int(arguments.get("read_timeout_ms", 5000)))
        await asyncio.sleep(min(read_timeout_ms / 1000.0, 2.0))
        return _json_text(
            {
                "stdout_fragment": _host_tail_text(rec.stdout_acc, max_bytes),
                "stderr_fragment": _host_tail_text(rec.stderr_acc, max_bytes),
                "alive": rec.proc.returncode is None,
                "returncode": rec.proc.returncode,
            }
        )

    if name == "cq_host_process_kill":
        process_guid = str(arguments.get("process_guid") or "")
        if not process_guid:
            return _text("Missing required argument: process_guid")
        rec = host_proc_registry.get(process_guid)
        if not rec:
            return _text("Unknown process_guid")
        signal_name = str(arguments.get("signal", "SIGTERM"))
        if rec.ttl_task and not rec.ttl_task.done():
            rec.ttl_task.cancel()
        for task in (rec.pump_out, rec.pump_err):
            if task and not task.done():
                task.cancel()
        try:
            if rec.proc.returncode is None:
                if signal_name == "SIGKILL":
                    rec.proc.kill()
                else:
                    rec.proc.terminate()
                try:
                    await asyncio.wait_for(rec.proc.wait(), timeout=8.0)
                except asyncio.TimeoutError:
                    rec.proc.kill()
                    await rec.proc.wait()
        finally:
            for task in (rec.pump_out, rec.pump_err):
                if task and not task.done():
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            host_proc_registry.pop(process_guid, None)
        return _json_text({"stopped": True, "returncode": rec.proc.returncode})

    if name == "cq_host_process_status":
        process_guid = str(arguments.get("process_guid") or "")
        if not process_guid:
            return _text("Missing required argument: process_guid")
        rec = host_proc_registry.get(process_guid)
        if not rec:
            return _text("Unknown process_guid")
        alive = rec.proc.returncode is None
        runtime_ms = int((time.monotonic() - rec.started) * 1000)
        return _json_text(
            {
                "alive": alive,
                "returncode": rec.proc.returncode,
                "pid": rec.proc.pid,
                "runtime_ms": runtime_ms,
                "command": rec.argv_desc,
            }
        )

    if name == "cq_host_process_list":
        items: list[dict[str, Any]] = []
        for guid, record in host_proc_registry.items():
            items.append(
                {
                    "process_guid": guid,
                    "pid": record.proc.pid,
                    "alive": record.proc.returncode is None,
                    "returncode": record.proc.returncode,
                    "command": record.argv_desc,
                }
            )
        return _json_text({"processes": items, "count": len(items)})

    if name == "cq_host_process_wait":
        process_guid = str(arguments.get("process_guid") or "")
        if not process_guid:
            return _text("Missing required argument: process_guid")
        rec = host_proc_registry.get(process_guid)
        if not rec:
            return _text("Unknown process_guid")
        wait_ms = max(0, int(arguments.get("wait_timeout_ms", 30000)))
        condition = str(arguments.get("wait_condition", "any_output"))
        baseline = len(rec.stdout_acc) + len(rec.stderr_acc)
        deadline = time.monotonic() + wait_ms / 1000.0
        while time.monotonic() < deadline:
            if rec.proc.returncode is not None:
                return _json_text({"finished": True, "returncode": rec.proc.returncode})
            if condition == "any_output" and (len(rec.stdout_acc) + len(rec.stderr_acc) > baseline):
                return _json_text({"finished": False, "saw_output": True})
            await asyncio.sleep(0.05)
        return _json_text({"finished": False, "timed_out": True})

    return None