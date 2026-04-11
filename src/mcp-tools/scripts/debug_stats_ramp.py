#!/usr/bin/env python3
"""
Лесенка прогонов filewalk_review_runner в режиме --debug-bypass (без провайдера).

Цель: полезная статистика context_cache_metrics — смесь сценариев
(1 файл = 1 чат vs несколько файлов в одном чате → хвост/DELTA_SAFE),
разные срезы индекса (offset), нарастающий объём.

Пример:
  python debug_stats_ramp.py --project-id 2
  (пароль: COLLOQUIUM_PASSWORD* / COLLOQUIUM_PASSWORD_FILE / mcp-tools/cqds_mcp_auth.secret)
  python debug_stats_ramp.py --step-begin 1 --step-end 2 --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_THIS = Path(__file__).resolve()
_SCRIPTS = _THIS.parent
_MCP_TOOLS = _SCRIPTS.parent
_FILEWALK = _SCRIPTS / "filewalk_review_runner.py"
_PROGRESS = _SCRIPTS / "cache_phase1_progress.py"
_DEFAULT_OUT_DIR = _MCP_TOOLS / "logs" / "cache_phase1_runner" / "debug_ramp"


# Имя, max_files, files_per_chat, offset — смещения не пересекаются сильно, объём растёт.
RAMP_STEPS: list[dict[str, int | str]] = [
    {"name": "xs", "max_files": 5, "files_per_chat": 1, "offset": 0},
    {"name": "s", "max_files": 12, "files_per_chat": 3, "offset": 8},
    {"name": "m", "max_files": 28, "files_per_chat": 5, "offset": 22},
    {"name": "l", "max_files": 55, "files_per_chat": 8, "offset": 55},
    {"name": "xl", "max_files": 100, "files_per_chat": 12, "offset": 115},
]


def _auth_argv(ns: argparse.Namespace) -> list[str]:
    pf = (getattr(ns, "password_file", None) or "").strip()
    pw = (getattr(ns, "password", None) or "").strip()
    if pf:
        return ["--password-file", pf]
    if pw:
        return ["--password", pw]
    return []


def _run_one(
    python: str,
    *,
    url: str,
    username: str,
    auth: list[str],
    project_id: int,
    actor_mention: str,
    step: dict[str, int | str],
    out_path: Path,
    wait_timeout: int,
    per_file_sleep: float,
    list_timeout: float,
    dry_run: bool,
) -> int:
    tag = f"dbgramp-{step['name']}"
    cmd = [
        python,
        str(_FILEWALK),
        "--url",
        url,
        "--username",
        username,
        *auth,
        "--project-id",
        str(project_id),
        "--max-files",
        str(step["max_files"]),
        "--files-per-chat",
        str(step["files_per_chat"]),
        "--offset",
        str(step["offset"]),
        "--actor-mention",
        actor_mention,
        "--debug-bypass",
        "--wait-timeout",
        str(wait_timeout),
        "--per-file-sleep",
        str(per_file_sleep),
        "--list-timeout",
        str(list_timeout),
        "--flush-every",
        "0",
        "--chat-name-prefix",
        f"{tag}-",
        "--out",
        str(out_path),
    ]
    print(f"\n=== Step {step['name']}: {' '.join(cmd[2:])} ===\n", flush=True)
    if dry_run:
        return 0
    r = subprocess.run(cmd, cwd=str(_MCP_TOOLS))
    return int(r.returncode)


def _run_progress(
    python: str,
    *,
    url: str,
    username: str,
    auth: list[str],
    project_id: int,
    dry_run: bool,
) -> None:
    cmd = [
        python,
        str(_PROGRESS),
        "--url",
        url,
        "--username",
        username,
        *auth,
        "--project-id",
        str(project_id),
        "--json",
    ]
    print("\n=== cache_phase1_progress (после лесенки) ===\n", flush=True)
    if dry_run:
        print(" ".join(cmd))
        return
    subprocess.run(cmd, cwd=str(_MCP_TOOLS), check=False)


def main() -> int:
    ap = argparse.ArgumentParser(description="Лесенка debug-bypass прогонов для context_cache_metrics.")
    ap.add_argument("--url", default=os.environ.get("COLLOQUIUM_URL", "http://localhost:8008"))
    ap.add_argument("--username", default=os.environ.get("COLLOQUIUM_USERNAME", "copilot"))
    ap.add_argument("--password", default=os.environ.get("COLLOQUIUM_PASSWORD", ""))
    ap.add_argument("--password-file", default=cq_cred.default_password_file_for_cli())
    ap.add_argument("--project-id", type=int, default=2)
    ap.add_argument("--actor-mention", default="@grok4f")
    ap.add_argument("--python", default=sys.executable, help="Интерпретатор для дочерних скриптов")
    ap.add_argument("--out-dir", default="", help="Каталог JSON отчётов (по умолчанию logs/.../debug_ramp)")
    ap.add_argument("--step-begin", type=int, default=1, help="Первый шаг, 1-based")
    ap.add_argument("--step-end", type=int, default=0, help="Последний шаг включительно (0 = все)")
    ap.add_argument("--wait-timeout", type=int, default=28, help="Ожидание «ответа» при bypass (с)")
    ap.add_argument("--per-file-sleep", type=float, default=0.12)
    ap.add_argument("--list-timeout", type=float, default=120.0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-progress", action="store_true", help="Не вызывать cache_phase1_progress в конце")
    args = ap.parse_args()

    if not _FILEWALK.is_file():
        print(f"Не найден {_FILEWALK}", file=sys.stderr)
        return 2

    auth = _auth_argv(args)

    out_dir = Path(args.out_dir).resolve() if args.out_dir.strip() else _DEFAULT_OUT_DIR
    if not args.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)

    n = len(RAMP_STEPS)
    hi = int(args.step_end) if int(args.step_end) > 0 else n
    lo = max(1, int(args.step_begin))
    hi = min(n, hi)
    if lo > hi:
        print("step-begin > step-end", file=sys.stderr)
        return 2

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    manifest: dict = {
        "started_at_utc": stamp,
        "steps_planned": [RAMP_STEPS[i - 1]["name"] for i in range(lo, hi + 1)],
        "project_id": args.project_id,
        "debug_bypass": True,
        "reports": [],
    }

    for i in range(lo, hi + 1):
        step = RAMP_STEPS[i - 1]
        out_path = out_dir / f"ramp_{stamp}_step{i}_{step['name']}.json"
        code = _run_one(
            args.python,
            url=args.url,
            username=args.username,
            auth=auth,
            project_id=args.project_id,
            actor_mention=args.actor_mention,
            step=step,
            out_path=out_path,
            wait_timeout=args.wait_timeout,
            per_file_sleep=args.per_file_sleep,
            list_timeout=args.list_timeout,
            dry_run=args.dry_run,
        )
        manifest["reports"].append({"step": i, "name": step["name"], "out": str(out_path), "exit_code": code})
        if code != 0:
            man_path = out_dir / f"ramp_{stamp}_manifest.json"
            if not args.dry_run:
                man_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"Шаг {i} завершился с кодом {code}, манифест: {man_path}", file=sys.stderr)
            return code

    man_path = out_dir / f"ramp_{stamp}_manifest.json"
    if not args.dry_run:
        man_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nМанифест: {man_path}\n", flush=True)

    if not args.no_progress:
        _run_progress(
            args.python,
            url=args.url,
            username=args.username,
            auth=auth,
            project_id=args.project_id,
            dry_run=args.dry_run,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
