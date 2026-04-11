#!/usr/bin/env python3
"""Compact Phase-1 report from context_cache_metrics."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

_THIS = Path(__file__).resolve()
_AGENT_ROOT = _THIS.parents[1]
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))

from managers.db import Database


def _epoch_now() -> int:
    return int(dt.datetime.now(dt.timezone.utc).timestamp())


def _row_get(row, idx: int, default=0):
    try:
        return row[idx]
    except Exception:
        return default


def build_report(hours: int) -> dict:
    db = Database.get_database()
    since_ts = _epoch_now() - max(1, hours) * 3600

    total = db.fetch_one(
        "SELECT COUNT(*) FROM context_cache_metrics WHERE ts >= :since_ts",
        {"since_ts": since_ts},
    )
    total_rows = int(_row_get(total, 0, 0) or 0)

    mode_rows = db.fetch_all(
        """
        SELECT mode, COUNT(*)
        FROM context_cache_metrics
        WHERE ts >= :since_ts
        GROUP BY mode
        ORDER BY COUNT(*) DESC
        """,
        {"since_ts": since_ts},
    )
    reason_rows = db.fetch_all(
        """
        SELECT reason, COUNT(*)
        FROM context_cache_metrics
        WHERE ts >= :since_ts
        GROUP BY reason
        ORDER BY COUNT(*) DESC
        """,
        {"since_ts": since_ts},
    )
    model_rows = db.fetch_all(
        """
        SELECT model,
               COUNT(*) AS total,
               AVG(sent_tokens) AS avg_sent,
               AVG(build_context_ms) AS avg_build_ms,
               SUM(CASE WHEN provider_error = 1 THEN 1 ELSE 0 END) AS provider_errors
        FROM context_cache_metrics
        WHERE ts >= :since_ts
        GROUP BY model
        ORDER BY total DESC
        """,
        {"since_ts": since_ts},
    )
    err_mode_rows = db.fetch_all(
        """
        SELECT mode,
               SUM(CASE WHEN provider_error = 1 THEN 1 ELSE 0 END) AS errors,
               COUNT(*) AS total
        FROM context_cache_metrics
        WHERE ts >= :since_ts
        GROUP BY mode
        ORDER BY total DESC
        """,
        {"since_ts": since_ts},
    )
    p95_rows = db.fetch_all(
        """
        SELECT build_context_ms, sent_tokens
        FROM context_cache_metrics
        WHERE ts >= :since_ts
        ORDER BY ts
        """,
        {"since_ts": since_ts},
    )

    def pctl(values: list[int], p: float) -> int:
        if not values:
            return 0
        values = sorted(values)
        idx = int((len(values) - 1) * p)
        return int(values[idx])

    build_ms = [int(_row_get(r, 0, 0) or 0) for r in p95_rows]
    sent_tok = [int(_row_get(r, 1, 0) or 0) for r in p95_rows]

    mode = {}
    for r in mode_rows:
        k = str(_row_get(r, 0, "") or "")
        c = int(_row_get(r, 1, 0) or 0)
        mode[k] = {
            "count": c,
            "pct": round((100.0 * c / total_rows), 3) if total_rows else 0.0,
        }

    reasons = [{"reason": str(_row_get(r, 0, "")), "count": int(_row_get(r, 1, 0) or 0)} for r in reason_rows]

    models = []
    for r in model_rows:
        total_m = int(_row_get(r, 1, 0) or 0)
        err_m = int(_row_get(r, 4, 0) or 0)
        models.append(
            {
                "model": str(_row_get(r, 0, "")),
                "count": total_m,
                "avg_sent_tokens": float(_row_get(r, 2, 0) or 0),
                "avg_build_context_ms": float(_row_get(r, 3, 0) or 0),
                "provider_errors": err_m,
                "provider_error_pct": round((100.0 * err_m / total_m), 3) if total_m else 0.0,
            }
        )

    error_by_mode = {}
    for r in err_mode_rows:
        m = str(_row_get(r, 0, ""))
        e = int(_row_get(r, 1, 0) or 0)
        t = int(_row_get(r, 2, 0) or 0)
        error_by_mode[m] = {
            "errors": e,
            "total": t,
            "pct": round((100.0 * e / t), 3) if t else 0.0,
        }

    report = {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "window_hours": hours,
        "since_ts": since_ts,
        "total_rows": total_rows,
        "mode": mode,
        "top_reasons": reasons[:12],
        "models": models,
        "provider_error_by_mode": error_by_mode,
        "p50_build_context_ms": pctl(build_ms, 0.50),
        "p95_build_context_ms": pctl(build_ms, 0.95),
        "p50_sent_tokens": pctl(sent_tok, 0.50),
        "p95_sent_tokens": pctl(sent_tok, 0.95),
    }
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase-1 cache telemetry report")
    ap.add_argument("--hours", type=int, default=24, help="Lookback window in hours")
    ap.add_argument("--out", default="", help="Optional output JSON path")
    args = ap.parse_args()

    report = build_report(args.hours)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
