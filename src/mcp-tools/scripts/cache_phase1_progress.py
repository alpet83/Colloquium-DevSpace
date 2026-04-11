#!/usr/bin/env python3
"""Краткая картина Phase 1: % к цели по строкам метрик и оценка оставшихся прогонов."""
from __future__ import annotations

import argparse
import asyncio
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
    data = json.loads(inner)
    if data.get("status") != "success":
        raise RuntimeError(f"query_db inner: {data!r}")
    rows = data.get("rows")
    return rows if isinstance(rows, list) else []


async def _fetch_counts(client: ColloquiumClient, project_id: int) -> tuple[int, int]:
    q = (
        "SELECT COUNT(*)::int AS c, "
        "COALESCE(SUM(sent_tokens), 0)::bigint AS s "
        "FROM context_cache_metrics"
    )
    r = await client.query_db(project_id, q, timeout=90)
    rows = _parse_query_db_rows(r)
    if not rows or len(rows[0]) < 2:
        return 0, 0
    row = rows[0]
    return int(row[0] or 0), int(row[1] or 0)


async def _fetch_mode_breakdown(client: ColloquiumClient, project_id: int) -> dict[str, int]:
    q = (
        "SELECT mode, COUNT(*)::int AS c "
        "FROM context_cache_metrics "
        "GROUP BY mode "
        "ORDER BY mode ASC"
    )
    r = await client.query_db(project_id, q, timeout=90)
    rows = _parse_query_db_rows(r)
    out: dict[str, int] = {}
    for row in rows:
        if len(row) < 2:
            continue
        label = str(row[0] if row[0] is not None else "").strip() or "(empty)"
        out[label] = int(row[1] or 0)
    return out


async def run(args: argparse.Namespace) -> dict[str, Any]:
    password, _ = cq_cred.resolve_password(args.password, args.password_file)
    if not password:
        raise RuntimeError("Нужен пароль (как у других mcp-tools скриптов).")

    client = ColloquiumClient(base_url=args.url, username=args.username, password=password)
    try:
        await client.select_project(args.project_id)
        n_rows, sum_sent = await _fetch_counts(client, args.project_id)
        mode_counts = await _fetch_mode_breakdown(client, args.project_id)
    finally:
        await client.aclose()

    target = max(1, int(args.target_rows))
    pct = min(100.0, 100.0 * n_rows / target)
    need = max(0, target - n_rows)
    per_run = max(1, int(args.assume_new_rows_per_run))
    runs_left = math.ceil(need / per_run) if need else 0

    delta_safe = int(mode_counts.get("DELTA_SAFE", 0))
    delta_pct = round(100.0 * delta_safe / n_rows, 3) if n_rows else 0.0

    out: dict[str, Any] = {
        "metric_rows_now": n_rows,
        "metric_rows_target": target,
        "progress_pct": round(pct, 2),
        "metric_rows_remaining": need,
        "assumed_new_rows_per_run": per_run,
        "estimated_runs_remaining": runs_left,
        "sum_sent_tokens": sum_sent,
        "mode_counts": mode_counts,
        "delta_safe_rows": delta_safe,
        "delta_safe_pct": delta_pct,
    }

    if args.target_prompt_tokens > 0:
        ttok = int(args.target_prompt_tokens)
        tok_pct = min(100.0, 100.0 * sum_sent / ttok) if ttok else 0.0
        need_tok = max(0, ttok - sum_sent)
        out["target_prompt_tokens"] = ttok
        out["prompt_tokens_progress_pct"] = round(tok_pct, 2)
        out["prompt_tokens_remaining"] = need_tok

    return out


def _print_human(d: dict[str, Any]) -> None:
    n = d["metric_rows_now"]
    t = d["metric_rows_target"]
    pct = d["progress_pct"]
    need = d["metric_rows_remaining"]
    per = d["assumed_new_rows_per_run"]
    runs = d["estimated_runs_remaining"]

    print("Phase 1 / context_cache_metrics (строки ~ число зафиксированных интеракций)")
    print(f"  Собрано: {n} / {t}  ->  {pct}% цели по строкам")
    modes = d.get("mode_counts") or {}
    if modes:
        parts = [f"{k}={v}" for k, v in sorted(modes.items(), key=lambda x: (-x[1], x[0]))]
        print(f"  Режимы (mode): {', '.join(parts)}")
    ds = int(d.get("delta_safe_rows", 0) or 0)
    dsp = float(d.get("delta_safe_pct", 0.0) or 0.0)
    print(f"  DELTA_SAFE: {ds} строк ({dsp}% от всех метрик)")
    if need <= 0:
        print("  По строкам цель достигнута (можно поднять --target-rows для запаса).")
    else:
        print(f"  Осталось строк (оценка): {need}")
        print(
            f"  При ~{per} новых строк на один типовой прогон (filewalk/scenario): "
            f"ещё порядка {runs} таких прогонов"
        )

    if "target_prompt_tokens" in d:
        print(
            f"  Prompt-токены (sent_tokens сумма): {d['sum_sent_tokens']:,} / "
            f"{d['target_prompt_tokens']:,}  ->  {d['prompt_tokens_progress_pct']}% "
            f"(грубый ориентир по «объёму»)"
        )
        if d["prompt_tokens_remaining"] > 0:
            print(f"  До цели по токенам (оценка): ~{d['prompt_tokens_remaining']:,}")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser(
        description="Процент сбора статистики Phase 1 и оценка числа похожих прогонов."
    )
    ap.add_argument("--url", default=os.environ.get("COLLOQUIUM_URL", "http://localhost:8008"))
    ap.add_argument("--username", default=os.environ.get("COLLOQUIUM_USERNAME", "copilot"))
    ap.add_argument("--password", default=os.environ.get("COLLOQUIUM_PASSWORD", ""))
    ap.add_argument("--password-file", default=cq_cred.default_password_file_for_cli())
    ap.add_argument("--project-id", type=int, default=2)
    ap.add_argument(
        "--target-rows",
        type=int,
        default=800,
        help="Целевое число строк context_cache_metrics (Phase 1: ориентир 500–1500, по умолчанию 800).",
    )
    ap.add_argument(
        "--assume-new-rows-per-run",
        type=int,
        default=24,
        help=(
            "Сколько новых строк метрик в среднем даёт один прогон. "
            "При filewalk с --files-per-chat 1 обычно близко к числу обработанных файлов (или чуть меньше при сбоях)."
        ),
    )
    ap.add_argument(
        "--target-prompt-tokens",
        type=int,
        default=0,
        help="Если >0: второй прогресс по SUM(sent_tokens) к этому ориентиру (копейки не считает).",
    )
    ap.add_argument("--json", action="store_true", help="Только JSON в stdout")
    args = ap.parse_args()

    payload = asyncio.run(run(args))
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_human(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
