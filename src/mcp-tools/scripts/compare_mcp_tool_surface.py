#!/usr/bin/env python3
"""Сравнение объёма презентации инструментов: полный copilot_mcp_tool vs cqds_runtime."""
from __future__ import annotations

import json
import sys
from pathlib import Path

MCP_TOOLS = Path(__file__).resolve().parent.parent
RUNTIME = MCP_TOOLS / "runtime"
for p in (MCP_TOOLS, RUNTIME):
    s = str(p)
    if s not in sys.path:
        sys.path.insert(0, s)


def _payload(tools: list) -> list[dict]:
    return [
        {"name": t.name, "description": t.description, "inputSchema": t.inputSchema}
        for t in tools
    ]


def _stats(tools: list) -> dict:
    payload = _payload(tools)
    raw = json.dumps(payload, ensure_ascii=False)
    desc_only = sum(len(t.description or "") for t in tools)
    names = sum(len(t.name) for t in tools)
    return {
        "tool_count": len(tools),
        "json_bytes_utf8": len(raw.encode("utf-8")),
        "json_chars": len(raw),
        "sum_description_chars": desc_only,
        "sum_name_chars": names,
        "approx_tokens_4chars": len(raw) // 4,
    }


def main() -> None:
    import copilot_mcp_tool as full
    import copilot_mcp_runtime as rt

    full_tools = full._registered_tools()
    rt_tools = rt._registered_tools()
    sf = _stats(full_tools)
    sr = _stats(rt_tools)
    ratio = sf["json_chars"] / max(sr["json_chars"], 1)

    print("=== Полный copilot_mcp_tool (MODULE_HANDLERS) ===")
    for k, v in sf.items():
        print(f"  {k}: {v}")
    print("=== cqds_runtime (copilot_mcp_runtime) ===")
    for k, v in sr.items():
        print(f"  {k}: {v}")
    print("=== Соотношение (полный / runtime) ===")
    print(f"  json_chars: {ratio:.2f}x")
    print(f"  tool_count: {sf['tool_count']} / {sr['tool_count']} = {sf['tool_count']/sr['tool_count']:.2f}x")
    print("\nИмена инструментов (runtime):")
    print(" ", ", ".join(sorted(t.name for t in rt_tools)))
    print("\nИмена инструментов (полный сервер), сортировка:")
    print(" ", ", ".join(sorted(t.name for t in full_tools)))


if __name__ == "__main__":
    main()
