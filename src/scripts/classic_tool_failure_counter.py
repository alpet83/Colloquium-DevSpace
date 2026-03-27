#!/usr/bin/env python3
"""Increment daily counters in stats/classic_tool_failures.json.

Usage examples:
  python scripts/classic_tool_failure_counter.py wrong_context
  python scripts/classic_tool_failure_counter.py shell_not_found --note "grep unavailable in powershell"
  python scripts/classic_tool_failure_counter.py permission_error --date 2026-03-27 --count 2
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATS_FILE = ROOT / "stats" / "classic_tool_failures.json"

DEFAULT_CLASSES = [
    "shell_not_found",
    "path_mismatch",
    "wrong_context",
    "format_incompatibility",
    "permission_error",
]

DEFAULT_PROPOSALS = {
    "shell_not_found": "Add CQ helper wrappers for common shell queries in a cross-platform form",
    "path_mismatch": "Add CQ path-normalizer utility and path-aware search endpoint",
    "wrong_context": "Add CQ helper for guaranteed execution context routing (project/shell selector)",
    "format_incompatibility": "Add CQ output formatter (table/json/short) to avoid client-side parsing failures",
    "permission_error": "Add CQ safe-log-cleanup tool with protected-file skip reporting",
}


def utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def load_stats() -> dict:
    if not STATS_FILE.exists():
        return {
            "schema_version": 1,
            "description": "Daily tally of failed classic-tool attempts to justify CQ tool additions",
            "escalation_threshold_per_class_per_day": 3,
            "classes": list(DEFAULT_CLASSES),
            "days": {},
        }
    with STATS_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_stats(data: dict):
    STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with STATS_FILE.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=True, indent=2)
        f.write("\n")


def ensure_day(data: dict, day: str):
    classes = data.get("classes") or list(DEFAULT_CLASSES)
    days = data.setdefault("days", {})
    if day not in days:
        days[day] = {k: 0 for k in classes}
        days[day]["notes"] = []
        days[day]["proposals"] = []
        return
    for c in classes:
        days[day].setdefault(c, 0)
    days[day].setdefault("notes", [])
    days[day].setdefault("proposals", [])


def proposal_exists(day_data: dict, trigger_class: str, threshold: int) -> bool:
    for item in day_data.get("proposals", []):
        if item.get("trigger_class") == trigger_class and int(item.get("threshold", 0)) == threshold:
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Increment classic-tool failure counters")
    parser.add_argument("failure_class", choices=DEFAULT_CLASSES, help="Failure class to increment")
    parser.add_argument("--date", default=utc_day(), help="UTC day in YYYY-MM-DD")
    parser.add_argument("--count", type=int, default=1, help="Increment amount (default: 1)")
    parser.add_argument("--note", default="", help="Optional note to append")
    args = parser.parse_args()

    if args.count < 1:
        raise SystemExit("--count must be >= 1")

    stats = load_stats()
    ensure_day(stats, args.date)

    day_data = stats["days"][args.date]
    day_data[args.failure_class] = int(day_data.get(args.failure_class, 0)) + args.count

    if args.note:
        day_data["notes"].append(args.note)

    threshold = int(stats.get("escalation_threshold_per_class_per_day", 3))
    current_count = int(day_data.get(args.failure_class, 0))
    if current_count >= threshold and not proposal_exists(day_data, args.failure_class, threshold):
        day_data["proposals"].append(
            {
                "trigger_class": args.failure_class,
                "threshold": threshold,
                "idea": DEFAULT_PROPOSALS.get(args.failure_class, "Propose dedicated CQ tool for this failure class"),
            }
        )

    save_stats(stats)

    print(f"day={args.date}")
    print(f"class={args.failure_class}")
    print(f"count={day_data[args.failure_class]}")
    print(f"threshold={threshold}")
    print(f"proposals={len(day_data.get('proposals', []))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
