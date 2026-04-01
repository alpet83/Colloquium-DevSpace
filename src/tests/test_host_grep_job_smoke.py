#!/usr/bin/env python3
"""Дымовой прогон host_async: start_host_grep_job + take_host_grep_snapshot до scan_complete."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mcp-tools"))

from cqds_host_grep_jobs import start_host_grep_job, take_host_grep_snapshot  # noqa: E402


async def main() -> int:
    target = ROOT / "mcp-tools"
    jid = await start_host_grep_job(
        str(target),
        "def",
        mode="code",
        profile="all",
        include_glob=None,
        is_regex=False,
        case_sensitive=False,
        max_results=100,
        context_lines=0,
        timeout_sec=90,
        workers=4,
        page_size=20,
    )
    last_seq = -1
    for i in range(200):
        s = await take_host_grep_snapshot(jid)
        assert s is not None
        if s["snapshot_seq"] != last_seq:
            last_seq = s["snapshot_seq"]
            print("tick", i, "hits", len(s["hits"]), "seq", s["snapshot_seq"], "done", s["scan_complete"])
        if s["scan_complete"]:
            print("OK complete hits=", len(s["hits"]), "engine=", s["engine"])
            return 0
        await asyncio.sleep(0.15)
    print("FAIL timeout waiting scan_complete")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
