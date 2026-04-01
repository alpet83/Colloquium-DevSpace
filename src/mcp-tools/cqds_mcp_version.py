# Версия MCP-обвязки Colloquium/CQDS (stdio-серверы). Менять при релизах.
from __future__ import annotations

import os
import sys

VERSION = "1.002a"


def print_ident_stderr(server_label: str) -> None:
    """Идентификация процесса в консоли VS Code/Cursor (stderr, не ломает MCP stdio)."""
    print(
        f"[cqds-mcp] server={server_label} version={VERSION} pid={os.getpid()}",
        file=sys.stderr,
        flush=True,
    )
