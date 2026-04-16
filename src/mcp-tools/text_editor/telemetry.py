from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


def estimate_tokens_from_bytes(size_bytes: int) -> int:
    # Rough heuristic for mixed JSON/text payloads.
    return max(1, size_bytes // 4)


class TelemetryStore:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "telemetry.jsonl"

    def append(
        self,
        *,
        tool: str,
        op: str,
        request_obj: dict[str, Any],
        response_obj: dict[str, Any],
        used_help: bool,
        used_capabilities_guide: bool,
    ) -> dict[str, int | bool]:
        request_text = json.dumps(request_obj, ensure_ascii=False)
        response_text = json.dumps(response_obj, ensure_ascii=False)
        req_bytes = len(request_text.encode("utf-8"))
        resp_bytes = len(response_text.encode("utf-8"))
        req_tokens = estimate_tokens_from_bytes(req_bytes)
        resp_tokens = estimate_tokens_from_bytes(resp_bytes)
        row = {
            "ts": int(time.time()),
            "tool": tool,
            "op": op,
            "request_bytes": req_bytes,
            "response_bytes": resp_bytes,
            "request_tokens_est": req_tokens,
            "response_tokens_est": resp_tokens,
            "used_help": bool(used_help),
            "used_capabilities_guide": bool(used_capabilities_guide),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return {
            "request_bytes": req_bytes,
            "response_bytes": resp_bytes,
            "request_tokens_est": req_tokens,
            "response_tokens_est": resp_tokens,
            "used_help": bool(used_help),
            "used_capabilities_guide": bool(used_capabilities_guide),
        }

    def report(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "entries": 0,
                "totals": {
                    "request_bytes": 0,
                    "response_bytes": 0,
                    "request_tokens_est": 0,
                    "response_tokens_est": 0,
                },
                "top_ops_by_tokens": [],
            }
        lines = self.path.read_text(encoding="utf-8").splitlines()
        rows: list[dict[str, Any]] = []
        for line in lines:
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
        totals = {
            "request_bytes": 0,
            "response_bytes": 0,
            "request_tokens_est": 0,
            "response_tokens_est": 0,
        }
        op_totals: dict[str, int] = {}
        for row in rows:
            totals["request_bytes"] += int(row.get("request_bytes", 0))
            totals["response_bytes"] += int(row.get("response_bytes", 0))
            totals["request_tokens_est"] += int(row.get("request_tokens_est", 0))
            totals["response_tokens_est"] += int(row.get("response_tokens_est", 0))
            op = str(row.get("op") or "unknown")
            op_totals[op] = op_totals.get(op, 0) + int(row.get("request_tokens_est", 0)) + int(
                row.get("response_tokens_est", 0)
            )
        top_ops = sorted(op_totals.items(), key=lambda kv: kv[1], reverse=True)[:5]
        return {
            "entries": len(rows),
            "totals": totals,
            "top_ops_by_tokens": [{"op": op, "tokens_est_total": val} for op, val in top_ops],
        }
