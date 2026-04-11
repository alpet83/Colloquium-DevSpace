#!/usr/bin/env python3
"""Phase-1 acceptance gate report with explicit pass/fail thresholds.

Работает через Core API query_db (как остальные mcp-tools scripts), поэтому можно
применять и к synthetic/dev окнам, и к mixed chats.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any

_THIS = Path(__file__).resolve()
_MCP_TOOLS = _THIS.parents[1]
if str(_MCP_TOOLS) not in sys.path:
    sys.path.insert(0, str(_MCP_TOOLS))

from cqds_client import ColloquiumClient
import cqds_credentials as cq_cred


def _parse_query_db_rows(exec_result: dict[str, Any]) -> list[list[Any]]:
    shell_status = str(exec_result.get("status") or "")
    raw = str(exec_result.get("output") or "")
    if shell_status != "success":
        raise RuntimeError(f"query_db: {shell_status}: {raw[:600]}")
    m = re.search(r"<stdout>(.*)</stdout>", raw, re.DOTALL)
    inner = (m.group(1) if m else raw).strip()
    try:
        data = json.loads(inner)
    except json.JSONDecodeError:
        # Иногда stdout содержит неэкранированные control chars от трасс/логов.
        # Пробуем мягко очистить и повторить.
        cleaned = re.sub(r"[^\x09\x0A\x0D\x20-\x7E]", "", inner)
        data = json.loads(cleaned)
    if data.get("status") != "success":
        raise RuntimeError(f"query_db inner: {data!r}")
    rows = data.get("rows")
    return rows if isinstance(rows, list) else []


def _parse_ids(value: str) -> list[int]:
    out: list[int] = []
    for part in (value or "").split(","):
        part = part.strip()
        if not part:
            continue
        out.append(int(part))
    return sorted(set(out))


def _check(name: str, ok: bool, actual: Any, expected: str) -> dict[str, Any]:
    return {
        "check": name,
        "ok": bool(ok),
        "actual": actual,
        "expected": expected,
    }


def _where_clause(since_ts: int, chat_ids: list[int]) -> str:
    cond = f"ts >= {int(since_ts)}"
    if chat_ids:
        csv = ",".join(str(x) for x in chat_ids)
        cond += f" AND chat_id IN ({csv})"
    return cond


async def _fetch_totals(
    client: ColloquiumClient,
    project_id: int,
    since_ts: int,
    chat_ids: list[int],
) -> list[list[Any]]:
    cond = _where_clause(since_ts, chat_ids)
    q = (
        "SELECT COUNT(*)::int AS total, "
        "SUM(CASE WHEN mode='DELTA_SAFE' THEN 1 ELSE 0 END)::int AS delta_safe, "
        "SUM(CASE WHEN mode='FULL' THEN 1 ELSE 0 END)::int AS full_rows, "
        "SUM(CASE WHEN provider_error=1 THEN 1 ELSE 0 END)::int AS provider_errors "
        f"FROM context_cache_metrics WHERE {cond}"
    )
    raw = await client.query_db(project_id, q, timeout=120)
    return _parse_query_db_rows(raw)


async def _fetch_reason_counts(
    client: ColloquiumClient,
    project_id: int,
    since_ts: int,
    chat_ids: list[int],
) -> list[list[Any]]:
    cond = _where_clause(since_ts, chat_ids)
    q = (
        "SELECT reason, COUNT(*)::int "
        f"FROM context_cache_metrics WHERE {cond} "
        "GROUP BY reason ORDER BY COUNT(*) DESC"
    )
    raw = await client.query_db(project_id, q, timeout=120)
    return _parse_query_db_rows(raw)


async def _fetch_percentiles(
    client: ColloquiumClient,
    project_id: int,
    since_ts: int,
    chat_ids: list[int],
) -> list[list[Any]]:
    cond = _where_clause(since_ts, chat_ids)
    q = (
        "SELECT "
        "percentile_cont(0.50) WITHIN GROUP (ORDER BY build_context_ms)::float8 AS p50_build, "
        "percentile_cont(0.95) WITHIN GROUP (ORDER BY build_context_ms)::float8 AS p95_build, "
        "percentile_cont(0.50) WITHIN GROUP (ORDER BY sent_tokens)::float8 AS p50_sent, "
        "percentile_cont(0.95) WITHIN GROUP (ORDER BY sent_tokens)::float8 AS p95_sent, "
        "percentile_cont(0.50) WITHIN GROUP (ORDER BY used_tokens)::float8 AS p50_used, "
        "percentile_cont(0.95) WITHIN GROUP (ORDER BY used_tokens)::float8 AS p95_used, "
        "percentile_cont(0.50) WITHIN GROUP (ORDER BY output_tokens)::float8 AS p50_out, "
        "percentile_cont(0.95) WITHIN GROUP (ORDER BY output_tokens)::float8 AS p95_out "
        f"FROM context_cache_metrics WHERE {cond}"
    )
    raw = await client.query_db(project_id, q, timeout=120)
    return _parse_query_db_rows(raw)


async def _fetch_tail_totals(
    client: ColloquiumClient,
    project_id: int,
    since_ts: int,
    tail_chat_ids: list[int],
) -> list[list[Any]]:
    cond = _where_clause(since_ts, tail_chat_ids)
    q = (
        "SELECT COUNT(*)::int AS total, "
        "SUM(CASE WHEN mode='DELTA_SAFE' THEN 1 ELSE 0 END)::int AS delta_safe "
        f"FROM context_cache_metrics WHERE {cond}"
    )
    raw = await client.query_db(project_id, q, timeout=120)
    return _parse_query_db_rows(raw)


async def run(args: argparse.Namespace) -> dict[str, Any]:
    password, _ = cq_cred.resolve_password(args.password, args.password_file)
    if not password:
        raise RuntimeError("Password required.")

    chat_ids = _parse_ids(args.chat_ids)
    tail_chat_ids = _parse_ids(args.tail_chat_ids)
    since_ts = int((dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=max(1, args.hours))).timestamp())

    client = ColloquiumClient(base_url=args.url, username=args.username, password=password)
    try:
        await client.select_project(args.project_id)
        totals_rows = await _fetch_totals(client, args.project_id, since_ts, chat_ids)
        reasons = await _fetch_reason_counts(client, args.project_id, since_ts, chat_ids)
        pct_rows = await _fetch_percentiles(client, args.project_id, since_ts, chat_ids)
        tail_rows = await _fetch_tail_totals(client, args.project_id, since_ts, tail_chat_ids or chat_ids)
    finally:
        await client.aclose()

    trow = totals_rows[0] if totals_rows else [0, 0, 0, 0]
    total = int(trow[0] or 0)
    delta_safe = int(trow[1] or 0)
    full = int(trow[2] or 0)
    provider_errors = int(trow[3] or 0)
    delta_safe_pct = round(100.0 * delta_safe / total, 3) if total else 0.0
    provider_error_pct = round(100.0 * provider_errors / total, 3) if total else 0.0

    prow = pct_rows[0] if pct_rows else [0, 0, 0, 0, 0, 0, 0, 0]
    p50_build = int(round(float(prow[0] or 0)))
    p95_build = int(round(float(prow[1] or 0)))
    p50_sent = int(round(float(prow[2] or 0)))
    p95_sent = int(round(float(prow[3] or 0)))
    p50_used = int(round(float(prow[4] or 0)))
    p95_used = int(round(float(prow[5] or 0)))
    p50_out = int(round(float(prow[6] or 0)))
    p95_out = int(round(float(prow[7] or 0)))

    tr = tail_rows[0] if tail_rows else [0, 0]
    tail_total = int(tr[0] or 0)
    tail_ds = int(tr[1] or 0)
    tail_ds_pct = round(100.0 * tail_ds / tail_total, 3) if tail_total else 0.0

    checks = [
        _check(
            "min_rows",
            total >= int(args.min_rows),
            total,
            f">= {int(args.min_rows)}",
        ),
        _check(
            "provider_error_pct",
            provider_error_pct <= float(args.max_provider_error_pct),
            provider_error_pct,
            f"<= {float(args.max_provider_error_pct)}",
        ),
        _check(
            "tail_delta_safe_pct",
            tail_ds_pct >= float(args.min_tail_delta_safe_pct),
            tail_ds_pct,
            f">= {float(args.min_tail_delta_safe_pct)}",
        ),
        _check(
            "p95_build_context_ms",
            p95_build <= int(args.max_p95_build_ms),
            p95_build,
            f"<= {int(args.max_p95_build_ms)}",
        ),
        _check(
            "p95_sent_tokens",
            p95_sent <= int(args.max_p95_sent_tokens),
            p95_sent,
            f"<= {int(args.max_p95_sent_tokens)}",
        ),
    ]
    verdict = all(c["ok"] for c in checks)

    report = {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "project_id": int(args.project_id),
        "window_hours": int(args.hours),
        "since_ts": since_ts,
        "filter_chat_ids": chat_ids,
        "tail_chat_ids": tail_chat_ids,
        "totals": {
            "rows": total,
            "full_rows": full,
            "delta_safe_rows": delta_safe,
            "delta_safe_pct": delta_safe_pct,
            "provider_errors": provider_errors,
            "provider_error_pct": provider_error_pct,
        },
        "tail_window": {
            "rows": tail_total,
            "delta_safe_rows": tail_ds,
            "delta_safe_pct": tail_ds_pct,
        },
        "percentiles": {
            "build_context_ms": {"p50": p50_build, "p95": p95_build},
            "sent_tokens": {"p50": p50_sent, "p95": p95_sent},
            "used_tokens": {"p50": p50_used, "p95": p95_used},
            "output_tokens": {"p50": p50_out, "p95": p95_out},
        },
        "top_reasons": [
            {"reason": str(r[0] or ""), "count": int(r[1] or 0)}
            for r in reasons[:12]
        ],
        "checks": checks,
        "verdict": "PASS" if verdict else "FAIL",
    }
    return report


def _print_human(report: dict[str, Any]) -> None:
    print("Phase-1 Gate Report")
    print(f"  Verdict: {report['verdict']}")
    t = report["totals"]
    tw = report["tail_window"]
    p = report["percentiles"]
    print(
        f"  Rows={t['rows']} | FULL={t['full_rows']} | DELTA_SAFE={t['delta_safe_rows']} "
        f"({t['delta_safe_pct']}%) | provider_error={t['provider_errors']} ({t['provider_error_pct']}%)"
    )
    print(
        f"  Tail-window DELTA_SAFE={tw['delta_safe_rows']}/{tw['rows']} ({tw['delta_safe_pct']}%)"
    )
    print(
        f"  build_context_ms p50/p95={p['build_context_ms']['p50']}/{p['build_context_ms']['p95']} | "
        f"sent_tokens p50/p95={p['sent_tokens']['p50']}/{p['sent_tokens']['p95']}"
    )
    print("  Checks:")
    for c in report["checks"]:
        mark = "OK" if c["ok"] else "FAIL"
        print(f"    - {mark} {c['check']}: actual={c['actual']} expected {c['expected']}")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="Phase-1 acceptance gate report")
    ap.add_argument("--url", default=os.environ.get("COLLOQUIUM_URL", "http://localhost:8008"))
    ap.add_argument("--username", default=os.environ.get("COLLOQUIUM_USERNAME", "copilot"))
    ap.add_argument("--password", default=os.environ.get("COLLOQUIUM_PASSWORD", ""))
    ap.add_argument("--password-file", default=cq_cred.default_password_file_for_cli())
    ap.add_argument("--project-id", type=int, default=2)
    ap.add_argument("--hours", type=int, default=72)
    ap.add_argument(
        "--chat-ids",
        default="",
        help="Optional comma-list filter for all metrics (e.g. 774,775,776).",
    )
    ap.add_argument(
        "--tail-chat-ids",
        default="",
        help="Comma-list of tail-only chats for delta-safe threshold.",
    )
    ap.add_argument("--min-rows", type=int, default=100)
    ap.add_argument("--max-provider-error-pct", type=float, default=1.0)
    ap.add_argument("--min-tail-delta-safe-pct", type=float, default=85.0)
    ap.add_argument("--max-p95-build-ms", type=int, default=2500)
    ap.add_argument("--max-p95-sent-tokens", type=int, default=120000)
    ap.add_argument("--out", default="", help="Optional JSON report path")
    ap.add_argument("--json", action="store_true", help="Print only JSON")
    args = ap.parse_args()

    payload = asyncio.run(run(args))
    text = json.dumps(payload, ensure_ascii=False, indent=2)

    if args.json:
        print(text)
    else:
        _print_human(payload)
        print("\nJSON:\n" + text)

    if (args.out or "").strip():
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
        print(f"Saved: {out}")

    return 0 if payload.get("verdict") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
