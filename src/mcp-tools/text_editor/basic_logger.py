from __future__ import annotations

import os
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any


def _configure_stdio_utf8() -> None:
    # VS Code integrated terminal on Windows may default to cp1251/cp866,
    # which garbles Unicode diagnostics. Reconfigure streams when possible.
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _diag(msg: str) -> None:
    try:
        print(f"[text_editor.logger] {msg}", file=sys.stderr)
    except Exception:
        pass


def _resolve_logs_dir(data_dir: Path) -> Path:
    # Prefer explicit override, then shared mcp-tools/logs, then local data_dir/logs.
    explicit = (os.environ.get("TEXT_EDITOR_LOG_DIR") or "").strip()
    if explicit:
        p = Path(explicit).expanduser().resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p
    mcp_tools_root = Path(__file__).resolve().parents[1]
    shared = (mcp_tools_root / "logs").resolve()
    try:
        shared.mkdir(parents=True, exist_ok=True)
        return shared
    except Exception:
        fallback = (data_dir / "logs").resolve()
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def _parent_process_name() -> str:
    # Prefer psutil when available: more robust on Windows.
    try:
        import psutil  # type: ignore

        proc = psutil.Process(os.getpid())
        parent = proc.parent()
        if parent is not None:
            name = str(parent.name() or "").strip().lower()
            if name:
                _diag(f"parent detection via psutil: '{name}'")
                return name
            _diag("psutil parent process found, but name is empty")
        else:
            _diag("psutil parent process is None")
    except Exception:
        _diag("psutil detection failed; falling back")

    ppid = os.getppid()
    if ppid <= 0:
        _diag("os.getppid() returned non-positive value")
        return ""
    try:
        if os.name == "nt":
            # Best-effort on Windows without extra deps.
            cmd = [
                "powershell",
                "-NoProfile",
                "-Command",
                f"(Get-CimInstance Win32_Process -Filter \\\"ProcessId={ppid}\\\").Name",
            ]
            out = subprocess.check_output(cmd, text=True, timeout=1.5).strip().lower()
            if out:
                _diag(f"parent detection via powershell: '{out}'")
            else:
                _diag("powershell returned empty parent process name")
            return out
        # Linux/Unix best-effort via /proc.
        comm = Path(f"/proc/{ppid}/comm")
        if comm.exists():
            out = comm.read_text(encoding="utf-8").strip().lower()
            if out:
                _diag(f"parent detection via /proc: '{out}'")
            else:
                _diag("/proc parent process name is empty")
            return out
        _diag(f"/proc path not found for ppid={ppid}")
    except Exception:
        _diag("fallback parent detection failed")
        return ""
    return ""


def _parent_tag() -> str:
    name = _parent_process_name().lower()
    if not name:
        ppid = os.getppid()
        return f"ppid{ppid}" if ppid > 0 else "ppid0"
    if "cursor" in name:
        return "cursor"
    if "code" in name:
        return "code"
    if name.endswith(".exe"):
        name = name[:-4]
    allowed = "".join(ch for ch in name if ch.isalnum() or ch in ("-", "_")).strip("-_")
    return allowed[:24]


class BasicLoggerSimplified:
    def __init__(self, data_dir: Path, name: str):
        self.name = name
        self._lock = threading.Lock()
        log_dir = _resolve_logs_dir(data_dir)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        pid = os.getpid()
        nonce = secrets.token_hex(2)
        self._path = log_dir / f"{self.name}_{stamp}_{pid}_{nonce}.log"

    @staticmethod
    def _color(level: str, text: str) -> str:
        # ANSI colors: INFO green, WARN yellow, ERROR red, DEBUG cyan.
        colors = {"INFO": "32", "WARN": "33", "ERROR": "31", "DEBUG": "36"}
        code = colors.get(level, "37")
        return f"\x1b[{code}m{text}\x1b[0m"

    @staticmethod
    def _format(fmt: str, *args: Any) -> str:
        if not args:
            return fmt
        try:
            return fmt % args
        except Exception:
            return f"{fmt} | args={args!r}"

    def _write(self, level: str, text: str) -> None:
        ts = datetime.fromtimestamp(time.time()).strftime("%Y-%m-%d %H:%M:%S")
        base = f"[{ts}] [{self.name}] [{level}] {text}"
        line = self._color(level, base)
        with self._lock:
            with self._path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

    def debug(self, fmt: str, *args: Any) -> None:
        self._write("DEBUG", self._format(fmt, *args))

    def info(self, fmt: str, *args: Any) -> None:
        self._write("INFO", self._format(fmt, *args))

    def warn(self, fmt: str, *args: Any) -> None:
        self._write("WARN", self._format(fmt, *args))

    def error(self, fmt: str, *args: Any) -> None:
        self._write("ERROR", self._format(fmt, *args))


def make_logger(data_dir: Path, name: str) -> Any:
    _configure_stdio_utf8()
    tag = _parent_tag()
    effective_name = f"{name}-{tag}" if tag else name
    try:
        repo_root = Path(__file__).resolve().parents[2]
        agent_dir = repo_root / "agent"
        if agent_dir.is_dir() and str(agent_dir) not in sys.path:
            sys.path.insert(0, str(agent_dir))
        from lib.basic_logger import BasicLogger  # type: ignore

        # keep logs directly under shared logs root (without extra text_editor subdir)
        impl = BasicLogger("", effective_name, stdout=None)
        log_dir = _resolve_logs_dir(data_dir)
        impl.log_dir = str(log_dir) + os.sep
        if os.name == "nt":
            # Symlink creation on Windows often requires elevation/developer mode.
            # Disable link attempts to avoid noisy WinError 1314 in console.
            def _no_symlink(_path: str, _syml: str) -> bool:
                return False

            impl._make_symlink = _no_symlink  # type: ignore[attr-defined]
        return impl
    except Exception:
        return BasicLoggerSimplified(data_dir, effective_name)
