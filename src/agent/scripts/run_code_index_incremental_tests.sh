#!/usr/bin/env bash
# Запуск pytest с лимитом времени через coreutils `timeout` (Git Bash / Linux).
# Из каталога agent:
#   bash scripts/run_code_index_incremental_tests.sh
# Через MCP git_bash_exec: выполнить ту же команду с cwd = agent (или полный путь к скрипту).
set -euo pipefail
AGENT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="${AGENT}"
cd "${AGENT}"
exec timeout 90s python -m pytest tests/test_code_index_incremental.py -v --tb=short
