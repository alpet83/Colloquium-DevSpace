#!/usr/bin/env python3
"""Запуск pytest для code_index_incremental в отдельном процессе с жёстким timeout (Windows / без Git Bash).

Предпочтительно в среде с Git Bash / Linux: ``timeout`` из coreutils и MCP ``git_bash_exec`` —
см. ``run_code_index_incremental_tests.sh`` (одна команда, обрыв по SIGTERM по истечении секунд).

Использование из каталога agent:
  python scripts/run_code_index_incremental_tests.py

Пример для git_bash_exec (cwd = agent, POSIX path):
  timeout 90s python -m pytest tests/test_code_index_incremental.py -v --tb=short

Переменная окружения CODE_INDEX_TEST_TIMEOUT_SEC (по умолчанию 90) — только для этого .py wrapper.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    agent = Path(__file__).resolve().parents[1]
    tests = agent / "tests" / "test_code_index_incremental.py"
    try:
        sec = max(15, int(os.environ.get("CODE_INDEX_TEST_TIMEOUT_SEC", "90")))
    except ValueError:
        sec = 90
    env = os.environ.copy()
    env["PYTHONPATH"] = str(agent)
    cmd = [sys.executable, "-m", "pytest", str(tests), "-v", "--tb=short"]
    print(f"RUN (timeout {sec}s): {' '.join(cmd)}", flush=True)
    print(f"cwd={agent}", flush=True)
    try:
        r = subprocess.run(cmd, cwd=str(agent), env=env, timeout=sec)
        return int(r.returncode)
    except subprocess.TimeoutExpired:
        print(f"ERROR: процесс превысил {sec}s — завершён.", flush=True)
        return 124


if __name__ == "__main__":
    raise SystemExit(main())
