from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any

from mcp.types import CallToolResult, Tool  # type: ignore[import]

from cqds_client import _MCP_AUTH_TOKEN
from cqds_helpers import LOGGER, _json_text, _text
from cqds_run_ctx import RunContext


_DOCKER_CTL_ALLOWED = frozenset({"status", "restart", "rebuild", "clear-logs"})


def _docker_cli_exe() -> str:
    return shutil.which("docker") or "docker"


def docker_project_root() -> Path:
    """Корень репозитория cqds (родитель каталога mcp-tools); здесь docker-compose.yml."""
    return Path(__file__).resolve().parent.parent


def _docker_exec_argv(
    container: str,
    command: str | list[Any],
    *,
    workdir: str | None,
    user: str | None,
    env: dict[str, Any] | None,
    interactive: bool,
) -> list[str]:
    exe = _docker_cli_exe()
    argv: list[str] = [exe, "exec"]
    if interactive:
        argv.append("-i")
    if workdir:
        argv.extend(["-w", str(workdir)])
    if user:
        argv.extend(["-u", str(user)])
    if env:
        for key, value in env.items():
            argv.extend(["-e", f"{str(key)}={str(value)}"])
    argv.append(container)
    if isinstance(command, str):
        argv.extend(["sh", "-c", command])
    elif isinstance(command, list):
        if not command:
            raise ValueError("command list must be non-empty")
        argv.extend(str(part) for part in command)
    else:
        raise TypeError("command must be str or list")
    return argv


async def _invoke_cqds_ctl(command: str, services: list[str], timeout: int, wait: bool) -> dict[str, Any]:
    if command not in _DOCKER_CTL_ALLOWED:
        return {
            "ok": False,
            "error": f"Unknown command '{command}'. Allowed: {', '.join(sorted(_DOCKER_CTL_ALLOWED))}",
            "stdout": "",
            "stderr": "",
        }

    ctl_script = docker_project_root() / "scripts" / "cqds_ctl.py"
    if not ctl_script.is_file():
        return {
            "ok": False,
            "error": f"cqds_ctl.py not found at {ctl_script}",
            "stdout": "",
            "stderr": "",
        }

    proc_env = dict(os.environ)
    if not proc_env.get("MCP_AUTH_TOKEN"):
        proc_env["MCP_AUTH_TOKEN"] = _MCP_AUTH_TOKEN
    if not proc_env.get("DB_ROOT_PASSWD"):
        db_passwd_file = docker_project_root() / "secrets" / "cqds_db_password"
        if db_passwd_file.is_file():
            proc_env["DB_ROOT_PASSWD"] = db_passwd_file.read_text(encoding="utf-8").strip()

    cmd_args = [sys.executable, str(ctl_script), command]
    if command in {"status", "restart", "rebuild"}:
        cmd_args.append(f"--timeout={timeout}")
    if command == "status" and wait:
        cmd_args.append("--wait")
    cmd_args.extend(services)

    LOGGER.info("cqds_ctl: %s", " ".join(cmd_args))
    proc = await asyncio.create_subprocess_exec(
        *cmd_args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
        env=proc_env,
    )
    proc_timeout = timeout + 60
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=proc_timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return {
            "ok": False,
            "error": f"cqds_ctl timed out after {proc_timeout}s",
            "stdout": "",
            "stderr": "",
        }

    stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
    stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
    try:
        payload = json.loads(stdout_text)
    except Exception:
        return {
            "ok": False,
            "error": "non-JSON output from cqds_ctl.py",
            "stdout": stdout_text[:2000],
            "stderr": stderr_text[:1000],
        }
    return {"ok": True, "data": payload}


async def _docker_exec_batch_item(raw: dict[str, Any]) -> dict[str, Any]:
    try:
        container = str(raw.get("container", "")).strip()
        if not container:
            return {"ok": False, "error": "missing container", "request": raw}
        command = raw.get("command")
        if command is None:
            return {"ok": False, "error": "missing command", "request": raw}
        workdir = raw.get("workdir")
        workdir_s = str(workdir) if workdir is not None else None
        user = raw.get("user")
        user_s = str(user) if user is not None else None
        env = raw.get("env")
        env_d: dict[str, Any] | None = env if isinstance(env, dict) else None
        stdin_raw = raw.get("stdin")
        stdin_b = str(stdin_raw).encode("utf-8") if stdin_raw is not None else None
        interactive_flag = bool(raw.get("interactive", False)) or (stdin_b is not None)
        argv = _docker_exec_argv(
            container,
            command,
            workdir=workdir_s,
            user=user_s,
            env=env_d,
            interactive=interactive_flag,
        )
        timeout_sec = max(1, min(int(raw.get("timeout_sec", 120)), 600))
        LOGGER.info("cq_docker_exec: %s", argv)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(docker_project_root()),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE if stdin_b is not None else asyncio.subprocess.DEVNULL,
            env=os.environ.copy(),
        )
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(stdin_b), timeout=float(timeout_sec) + 15.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"ok": False, "error": f"docker exec timed out after {timeout_sec}s", "request": raw}
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": out_b.decode("utf-8", errors="replace") if out_b else "",
            "stderr": err_b.decode("utf-8", errors="replace") if err_b else "",
            "request": raw,
        }
    except Exception as exc:
        LOGGER.exception("docker exec batch item")
        return {"ok": False, "error": str(exc), "request": raw}


_COMPOSE_SUB_ALLOWED = frozenset({"up", "down", "stop", "start", "restart", "pull", "ps", "build"})


async def docker_compose_run(raw: dict[str, Any]) -> dict[str, Any]:
    """docker compose <sub> в каталоге репозитория cqds."""
    try:
        sub = str(raw.get("compose_command") or raw.get("subcommand") or "").strip().lower()
        if not sub:
            return {"ok": False, "error": "compose_command or subcommand required", "request": raw}
        if sub not in _COMPOSE_SUB_ALLOWED:
            return {
                "ok": False,
                "error": f"Unknown compose subcommand '{sub}'. Allowed: {', '.join(sorted(_COMPOSE_SUB_ALLOWED))}",
                "request": raw,
            }
        services = [str(s) for s in (raw.get("services") or [])]
        root = docker_project_root()
        compose_yml = root / "docker-compose.yml"
        exe = _docker_cli_exe()
        argv: list[str] = [exe, "compose"]
        if compose_yml.is_file():
            argv.extend(["-f", str(compose_yml)])
        extra_files = raw.get("compose_files")
        if isinstance(extra_files, list):
            for cf in extra_files:
                pth = Path(str(cf))
                if not pth.is_absolute():
                    pth = root / pth
                if pth.is_file():
                    argv.extend(["-f", str(pth)])
        for prof in raw.get("profiles") or []:
            argv.extend(["--profile", str(prof)])
        argv.append(sub)
        if sub == "up":
            if bool(raw.get("detach", True)):
                argv.append("-d")
            if bool(raw.get("build", False)):
                argv.append("--build")
            argv.extend(services)
        elif sub in {"stop", "start", "restart", "build", "pull"}:
            argv.extend(services)
        elif sub == "down":
            if bool(raw.get("remove_orphans", False)):
                argv.append("--remove-orphans")
            if bool(raw.get("volumes", False)):
                argv.append("-v")
            argv.extend(services)
        elif sub == "ps":
            if bool(raw.get("all", False)):
                argv.append("-a")
            argv.extend(services)
        timeout_sec = max(10, min(int(raw.get("timeout_sec", 600)), 7200))
        LOGGER.info("docker compose: cwd=%s argv=%s", root, argv)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            env=os.environ.copy(),
        )
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=float(timeout_sec))
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"ok": False, "error": f"docker compose {sub} timed out after {timeout_sec}s", "request": raw}
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": out_b.decode("utf-8", errors="replace") if out_b else "",
            "stderr": err_b.decode("utf-8", errors="replace") if err_b else "",
            "request": raw,
        }
    except Exception as exc:
        LOGGER.exception("docker_compose_run")
        return {"ok": False, "error": str(exc), "request": raw}


async def docker_inspect_run(raw: dict[str, Any]) -> dict[str, Any]:
    try:
        target = str(raw.get("target") or "").strip()
        if not target:
            return {"ok": False, "error": "missing target (container or image id/name)", "request": raw}
        exe = _docker_cli_exe()
        argv: list[str] = [exe, "inspect", target]
        fmt = raw.get("format")
        if fmt is not None and str(fmt).strip():
            argv.extend(["--format", str(fmt)])
        timeout_sec = max(5, min(int(raw.get("timeout_sec", 120)), 600))
        LOGGER.info("docker inspect: %s", argv)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(docker_project_root()),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            env=os.environ.copy(),
        )
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=float(timeout_sec))
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"ok": False, "error": f"docker inspect timed out after {timeout_sec}s", "request": raw}
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": out_b.decode("utf-8", errors="replace") if out_b else "",
            "stderr": err_b.decode("utf-8", errors="replace") if err_b else "",
            "request": raw,
        }
    except Exception as exc:
        LOGGER.exception("docker_inspect_run")
        return {"ok": False, "error": str(exc), "request": raw}


async def docker_logs_run(raw: dict[str, Any]) -> dict[str, Any]:
    try:
        container = str(raw.get("container") or "").strip()
        if not container:
            return {"ok": False, "error": "missing container", "request": raw}
        exe = _docker_cli_exe()
        argv: list[str] = [exe, "logs", container]
        tail = max(1, min(int(raw.get("tail", 200)), 10000))
        argv.extend(["--tail", str(tail)])
        since = raw.get("since")
        if since is not None and str(since).strip():
            argv.extend(["--since", str(since)])
        if bool(raw.get("timestamps", False)):
            argv.append("--timestamps")
        timeout_sec = max(5, min(int(raw.get("timeout_sec", 120)), 600))
        LOGGER.info("docker logs: %s", argv)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(docker_project_root()),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            env=os.environ.copy(),
        )
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=float(timeout_sec))
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"ok": False, "error": f"docker logs timed out after {timeout_sec}s", "request": raw}
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": out_b.decode("utf-8", errors="replace") if out_b else "",
            "stderr": err_b.decode("utf-8", errors="replace") if err_b else "",
            "request": raw,
        }
    except Exception as exc:
        LOGGER.exception("docker_logs_run")
        return {"ok": False, "error": str(exc), "request": raw}


# Публичные алиасы для runtime cq_docker_ctl
invoke_cqds_ctl = _invoke_cqds_ctl
docker_exec_one = _docker_exec_batch_item


TOOLS: list[Tool] = [
    Tool(
        name="cq_docker_control",
        description=(
            "Control CQDS Docker Compose services on the host. Wraps scripts/cqds_ctl.py.\n"
            "Commands:\n"
            "  status      — report container state, health, and recent log failures\n"
            "  restart     — docker compose restart + wait for stable/failed\n"
            "  rebuild     — docker compose up -d --build + wait for stable/failed\n"
            "  clear-logs  — truncate container json-file logs via Docker VM\n"
            "Optional 'services' list narrows the scope to specific compose services\n"
            "(e.g. ['colloquium-core', 'frontend']). Omit to target all services.\n"
            "'wait' (bool, status only): block until stable or failed rather than snapshot.\n"
            "'timeout': seconds to wait for stable state (default 90, restart/rebuild only)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "enum": ["status", "restart", "rebuild", "clear-logs"], "description": "Control action to perform."},
                "services": {"type": "array", "items": {"type": "string"}, "description": "Optional list of compose service names, e.g. ['colloquium-core']."},
                "timeout": {"type": "integer", "description": "Seconds to wait for stable/failed state (default 90). Only for status/restart/rebuild.", "default": 90},
                "wait": {"type": "boolean", "description": "For 'status': block until stable or failed before returning (default false).", "default": False},
            },
            "required": ["command"],
        },
    ),
    Tool(
        name="cq_docker_control_batch",
        description=(
            "Batch CQDS Docker Compose control via cqds_ctl.py: pass a JSON array of requests, "
            "get an array of results in order. Each request has the same fields as cq_docker_control "
            "(command, optional services, timeout, wait). Steps run sequentially on the MCP host. "
            "Use stop_on_error=true to abort after the first failure (default false: run all steps)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "requests": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "command": {"type": "string", "enum": ["status", "restart", "rebuild", "clear-logs"]},
                            "services": {"type": "array", "items": {"type": "string"}},
                            "timeout": {"type": "integer", "description": "Per-step timeout seconds (10–600, default 90).", "default": 90},
                            "wait": {"type": "boolean", "description": "For status: block until stable/failed.", "default": False},
                        },
                        "required": ["command"],
                    },
                    "description": "Ordered list of cq_docker_control-equivalent operations.",
                },
                "stop_on_error": {"type": "boolean", "description": "If true, stop after the first failed step (remaining not run).", "default": False},
            },
            "required": ["requests"],
        },
    ),
    Tool(
        name="cq_docker_exec",
        description=(
            "Run `docker exec` on the MCP host (not cqds_ctl). Pass an ordered list of exec requests; "
            "each step runs sequentially. Fields per request: container (required), command (string "
            "→ `sh -c` inside the container, or argv array), optional workdir, user, env object, "
            "stdin (string, UTF-8), interactive (bool; implied true when stdin is set), "
            "timeout_sec (1–600, default 120). Uses the docker CLI from PATH; cwd for the CLI is the "
            "cqds repo root."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "requests": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "container": {"type": "string"},
                            "command": {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}, "minItems": 1}]},
                            "workdir": {"type": "string"},
                            "user": {"type": "string"},
                            "env": {"type": "object", "description": "Extra -e KEY=value for docker exec."},
                            "stdin": {"type": "string", "description": "Optional stdin for this exec (UTF-8)."},
                            "interactive": {"type": "boolean", "description": "Pass docker -i (also set automatically if stdin is provided).", "default": False},
                            "timeout_sec": {"type": "integer", "description": "Per-step timeout seconds (1–600, default 120).", "default": 120},
                        },
                        "required": ["container", "command"],
                    },
                    "description": "Ordered docker exec operations.",
                },
                "stop_on_error": {"type": "boolean", "description": "If true, stop after the first failed step.", "default": False},
            },
            "required": ["requests"],
        },
    ),
]


async def handle(name: str, arguments: dict[str, Any], ctx: RunContext) -> CallToolResult | None:
    if name == "cq_docker_control":
        command = str(arguments.get("command", "status"))
        services = [str(s) for s in (arguments.get("services") or [])]
        timeout = max(10, min(int(arguments.get("timeout", 90)), 600))
        wait = bool(arguments.get("wait", False))
        out = await _invoke_cqds_ctl(command, services, timeout, wait)
        if out["ok"]:
            return _json_text(out["data"])
        err = str(out.get("error", "error"))
        if out.get("stdout") or out.get("stderr"):
            return _text(
                f"cq_docker_control: {err}\nstdout: {str(out.get('stdout', ''))[:600]}\nstderr: {str(out.get('stderr', ''))[:300]}"
            )
        return _text(f"cq_docker_control: {err}")

    if name == "cq_docker_control_batch":
        raw_reqs = arguments.get("requests")
        if not isinstance(raw_reqs, list) or len(raw_reqs) == 0:
            raise ValueError("cq_docker_control_batch requires a non-empty requests array")
        stop_on_error = bool(arguments.get("stop_on_error", False))
        results: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_reqs):
            if not isinstance(raw, dict):
                row: dict[str, Any] = {
                    "index": index,
                    "ok": False,
                    "error": "request must be an object",
                    "request": raw,
                }
                results.append(row)
                if stop_on_error:
                    break
                continue
            command = str(raw.get("command", "status"))
            services = [str(s) for s in (raw.get("services") or [])]
            timeout = max(10, min(int(raw.get("timeout", 90)), 600))
            wait = bool(raw.get("wait", False))
            if command not in _DOCKER_CTL_ALLOWED:
                row = {
                    "index": index,
                    "ok": False,
                    "error": f"Unknown command '{command}'. Allowed: {', '.join(sorted(_DOCKER_CTL_ALLOWED))}",
                    "request": raw,
                }
                results.append(row)
                if stop_on_error:
                    break
                continue
            out = await _invoke_cqds_ctl(command, services, timeout, wait)
            if out["ok"]:
                results.append({"index": index, "ok": True, "request": raw, "response": out["data"]})
            else:
                row = {
                    "index": index,
                    "ok": False,
                    "request": raw,
                    "error": out.get("error"),
                    "stdout": out.get("stdout"),
                    "stderr": out.get("stderr"),
                }
                results.append(row)
                if stop_on_error:
                    break
        all_ok = all(r.get("ok") for r in results)
        return _json_text({"results": results, "all_ok": all_ok, "count": len(results)})

    if name == "cq_docker_exec":
        raw_reqs = arguments.get("requests")
        if not isinstance(raw_reqs, list) or len(raw_reqs) == 0:
            raise ValueError("cq_docker_exec requires a non-empty requests array")
        stop_on_error = bool(arguments.get("stop_on_error", False))
        results: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_reqs):
            if not isinstance(raw, dict):
                results.append({"index": index, "ok": False, "error": "request must be an object", "request": raw})
                if stop_on_error:
                    break
                continue
            row = await _docker_exec_batch_item(raw)
            row["index"] = index
            results.append(row)
            if stop_on_error and not row.get("ok"):
                break
        all_ok = all(r.get("ok") for r in results)
        return _json_text({"results": results, "all_ok": all_ok, "count": len(results)})

    return None