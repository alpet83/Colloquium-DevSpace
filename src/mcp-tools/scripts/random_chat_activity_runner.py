#!/usr/bin/env python3
"""Один чат со случайными короткими сообщениями к модели (низкий «бюджет» выходных токенов).

Сценарии перемешиваются: пинг, микро-вопрос с ответом в одно слово/число, бюджетные крестики-нолики
(ход одной клеткой a1–c3 без развёрнутого анализа; полный разбор в чате всё равно нереалистичен),
опционально редкий @attached_file# с задачей «одно число».

Все промпты явно требуют укороченный ответ, чтобы меньше раздувать completion_tokens в llm_usage
при том же числе интеракций для Phase 1 / context_cache_metrics.

Зависимости: как у context_window_growth_runner (httpx, colloquium_httpx_client, cqds_credentials).
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import random
import re
import sys
import time
import traceback
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import httpx

_THIS = Path(__file__).resolve()
_MCP_TOOLS = _THIS.parents[1]
if str(_MCP_TOOLS) not in sys.path:
    sys.path.insert(0, str(_MCP_TOOLS))

from colloquium_httpx_client import ColloquiumHttpxClient
import cqds_credentials as cq_cred

CODE_EXT = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".kt",
    ".c", ".cpp", ".h", ".hpp", ".cs", ".rb", ".php", ".sql", ".sh",
    ".ps1", ".yaml", ".yml", ".toml", ".json", ".md",
}

_REPLY_CAP = (
    "Reply in **plain text only**, **at most 12 words**, no markdown headings, no bullet lists, no code fences."
)

_MICRO_PROMPTS: list[str] = [
    "One English word only — name a primary color (red/green/blue):",
    "Reply with exactly one digit 0–9 (nothing else) — your pick:",
    "One word: is 7 prime? (yes or no only):",
    "Single token answer — capital of France (one word):",
    "Reply with one integer only — 2+2:",
    "One word — chemical symbol for gold:",
    "Exactly two characters — your move in chess notation for pawn e2–e4 (e.g. e4) or say NA:",
]

_TTT_SETUPS: list[str] = [
    "Empty 3×3 (cells a1..c3). You are **O**, I am **X**; I have not moved yet. "
    "Reply with **only** your first move (e.g. b2).",
    "Board: only **b2=X** (me), rest `.`. You are **O**. Reply **only** one cell for your move or **PASS**.",
    "Board: **a1=X**, **b1=O**, rest `.`. You are **O** to move. Reply **only** one cell (e.g. c3) or **PASS**.",
    "Board: **c3=X**, **b2=O**, **a1=X** — messy line; you are **O**. Reply **only** your next cell or **PASS**.",
    "Board full draw pattern all `.` except you must pretend **O** opens at center: reply **only** b2 or another cell, one token.",
]


def _norm_actor(name: str) -> str:
    return name.strip().lstrip("@").lower()


def _is_progress_stub(message: str) -> bool:
    msg = (message or "").strip().lower()
    if not msg:
        return False
    markers = (
        "llm request accepted",
        "preparing response",
        "response in progress",
        "\u23f3",
    )
    return any(marker in msg for marker in markers)


def _latest_actor_message(resp: dict, expected_user: str) -> str | None:
    posts = resp.get("posts")
    if not isinstance(posts, dict):
        return None
    vals = [p for p in posts.values() if isinstance(p, dict)]
    if not vals:
        return None
    vals.sort(key=lambda p: int(p.get("id", 0) or 0))
    target = _norm_actor(expected_user)
    for p in reversed(vals):
        if str(p.get("user_name", "")).lower() == target:
            return str(p.get("message", "") or "")
    return None


def _extract_latest_message(resp: dict, expected_user: str) -> str:
    msg = _latest_actor_message(resp, expected_user)
    if msg and not _is_progress_stub(msg):
        return msg
    return ""


async def _wait_reply(
    client: ColloquiumHttpxClient,
    chat_id: int,
    timeout_sec: int,
    expected_actor: str,
) -> dict:
    deadline = time.monotonic() + max(1, timeout_sec)
    last: dict[str, Any] = {"chat_history": "no changes"}
    actor = _norm_actor(expected_actor)

    while time.monotonic() < deadline:
        rem = max(1.0, deadline - time.monotonic())
        poll_cap = 60.0 if timeout_sec >= 120 else 15.0
        resp = await client.get_reply(chat_id, wait=True, timeout=min(rem, poll_cap))
        if not isinstance(resp, dict):
            continue
        last = resp
        latest = _latest_actor_message(resp, actor)
        if latest is not None and latest.strip() and not _is_progress_stub(latest):
            return resp

    return last


def _parse_query_db_rows(exec_result: dict[str, Any]) -> list[list[Any]]:
    shell_status = str(exec_result.get("status") or "")
    raw = str(exec_result.get("output") or "")
    if shell_status != "success":
        raise RuntimeError(f"query_db exec status={shell_status!r}: {raw[:800]}")
    m = re.search(r"<stdout>(.*)</stdout>", raw, re.DOTALL)
    inner = (m.group(1) if m else raw).strip()
    try:
        data = json.loads(inner)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"query_db: not JSON stdout: {inner[:400]}") from e
    if data.get("status") != "success":
        raise RuntimeError(f"query_db: inner status={data!r}")
    rows = data.get("rows")
    if not isinstance(rows, list):
        return []
    return rows


async def _retry(coro_factory: Callable[[], Any], retries: int, base_sleep: float, tag: str):
    for attempt in range(retries + 1):
        try:
            return await coro_factory()
        except Exception as exc:  # noqa: BLE001
            text = str(exc).lower()
            if attempt >= retries:
                raise
            if "429" in text or "rate" in text or "timeout" in text:
                delay = base_sleep * (2**attempt) + random.uniform(0.0, 0.25)
            else:
                delay = base_sleep + random.uniform(0.0, 0.2)
            print(
                f"[retry] {tag} attempt={attempt+1} delay={delay:.2f}s err={exc}",
                file=sys.stderr,
            )
            await asyncio.sleep(delay)


async def _list_files_fallback_attached(
    client: ColloquiumHttpxClient,
    project_id: int,
    limit: int,
    query_timeout: int,
    retries: int,
    base_sleep: float,
) -> list[dict[str, Any]]:
    lim = max(1, min(int(limit), 50_000))
    pid = int(project_id)
    sql = (
        f"SELECT id, file_name FROM attached_files WHERE project_id = {pid} "
        f"ORDER BY id ASC LIMIT {lim}"
    )

    async def _run():
        result = await client.query_db(project_id, sql, timeout=query_timeout)
        return _parse_query_db_rows(result)

    rows = await _retry(_run, retries, base_sleep, "query_db:attached_files")
    out: list[dict[str, Any]] = []
    for row in rows:
        if len(row) < 2:
            continue
        try:
            fid = int(row[0])
        except (TypeError, ValueError):
            continue
        name = str(row[1] or "")
        if fid > 0 and name:
            out.append({"id": fid, "file_name": name})
    if not out:
        raise RuntimeError("Fallback attached_files returned no rows.")
    return out


async def _load_project_files(
    client: ColloquiumHttpxClient,
    args: Any,
) -> tuple[list[dict[str, Any]], str]:
    async def _list_index():
        return await client.list_files(
            args.project_id,
            request_timeout=float(args.list_timeout),
        )

    try:
        files = await _retry(_list_index, args.retries, args.base_sleep, "list_files")
        return files, "file_index"
    except Exception as exc:
        print(
            f"[fallback] list_files failed ({exc!r}); using attached_files query.",
            file=sys.stderr,
        )
        files = await _list_files_fallback_attached(
            client,
            args.project_id,
            args.fallback_sql_limit,
            args.fallback_query_timeout,
            args.retries,
            args.base_sleep,
        )
        return files, "attached_files"


def _pick_files(files: list[dict], max_files: int, offset: int) -> list[dict]:
    selected: list[dict] = []
    for f in files[offset:]:
        name = str(f.get("file_name", "") or "")
        ext = Path(name).suffix.lower()
        if ext and ext not in CODE_EXT:
            continue
        if "/node_modules/" in name.replace("\\", "/"):
            continue
        selected.append(f)
        if len(selected) >= max_files:
            break
    return selected


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _write_report_snapshot(
    path: Path,
    report: dict[str, Any],
    *,
    turns_done: int,
    max_turns: int | None,
    started_ts: int,
    status: str,
    save_note: str,
    exc: BaseException | None = None,
) -> None:
    report["turns_done"] = turns_done
    if max_turns is not None:
        report["max_total_turns"] = max_turns
    report["started_at"] = started_ts
    report["finished_at"] = int(time.time())
    report["elapsed_seconds"] = report["finished_at"] - started_ts
    report["status"] = status
    report["save_note"] = save_note
    if exc is not None:
        report["error"] = f"{type(exc).__name__}: {exc}"
        if not isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)):
            report["error_traceback"] = traceback.format_exc()
    else:
        report.pop("error", None)
        report.pop("error_traceback", None)
    _atomic_write_json(path, report)


def _iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _build_prompt(
    actor: str,
    *,
    body: str,
) -> str:
    act = actor.strip()
    if not act.startswith("@"):
        act = f"@{act}"
    return (
        f"{act} Synthetic **low-token** chat activity (read-only).\n\n"
        f"{body}\n\n"
        f"{_REPLY_CAP}\n"
    )


def _random_activity_prompt(
    rng: random.Random,
    actor: str,
    *,
    file_pool: list[dict[str, Any]],
    weights: tuple[float, float, float, float, float],
) -> tuple[str, str, dict[str, Any]]:
    """Return (kind, full_prompt, extra_fields)."""
    w_ping, w_micro, w_ttt, w_digit, w_file = weights
    total = w_ping + w_micro + w_ttt + w_digit + w_file
    if total <= 0:
        total = 1.0
    r = rng.random() * total
    if r < w_ping:
        return (
            "ping",
            _build_prompt(
                actor,
                body="Ping check. Reply with the single token **PONG** and nothing else.",
            ),
            {},
        )
    r -= w_ping
    if r < w_micro:
        line = rng.choice(_MICRO_PROMPTS)
        return ("micro", _build_prompt(actor, body=line), {})
    r -= w_micro
    if r < w_ttt:
        setup = rng.choice(_TTT_SETUPS)
        body = (
            "Budget **tic-tac-toe** on cells **a1–c3** (text only). "
            f"{setup}\n"
            "Do not print the whole board; do not explain strategy. "
            "If you refuse, reply exactly **PASS**."
        )
        return ("ttt", _build_prompt(actor, body=body), {})
    r -= w_ttt
    if r < w_digit:
        n = rng.randint(0, 99)
        return (
            "digit_echo",
            _build_prompt(
                actor,
                body=f"Echo task: reply with **only** the integer `{n}` and nothing else.",
            ),
            {"echo_n": n},
        )
    r -= w_digit
    if r < w_file and file_pool:
        item = rng.choice(file_pool)
        fid = int(item.get("id") or 0)
        fname = str(item.get("file_name") or "")
        body = (
            "Attached file below. Reply with **one integer only**: "
            "how many distinct file bodies you still see in full in context (including this one). "
            "If unsure, guess a single integer."
            f"\n\n@attached_file#{fid}"
        )
        return (
            "file_one_int",
            _build_prompt(actor, body=body),
            {"file_id": fid, "file_name": fname},
        )
    n = rng.randint(0, 99)
    return (
        "digit_echo",
        _build_prompt(
            actor,
            body=f"Echo task: reply with **only** the integer `{n}` and nothing else.",
        ),
        {"echo_n": n},
    )


async def run(args: argparse.Namespace) -> dict[str, Any]:
    password, _ = cq_cred.resolve_password(args.password, args.password_file)
    if not password:
        raise RuntimeError("Password is required.")

    rng = random.Random(int(args.seed))

    client = ColloquiumHttpxClient(base_url=args.url, username=args.username, password=password)
    started = int(time.time())
    turns_list: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "scenario": "random_chat_low_token_activity",
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "base_url": args.url,
        "project_id": args.project_id,
        "actor_mention": args.actor_mention,
        "chat_description": "",
        "turns": turns_list,
        "results": turns_list,
        "seed": int(args.seed),
        "weights": {
            "ping": args.w_ping,
            "micro": args.w_micro,
            "ttt": args.w_ttt,
            "digit_echo": args.w_digit,
            "file_one_int": args.w_file,
        },
        "notes": [
            "Prompts cap reply length to limit completion_tokens; file_one_int still expands context.",
        ],
    }

    weights = (args.w_ping, args.w_micro, args.w_ttt, args.w_digit, args.w_file)

    try:
        await _retry(
            lambda: client.select_project(args.project_id),
            args.retries,
            args.base_sleep,
            "select_project",
        )

        fw_args = SimpleNamespace(
            project_id=args.project_id,
            list_timeout=float(args.list_timeout),
            retries=args.retries,
            base_sleep=args.base_sleep,
            fallback_sql_limit=args.fallback_sql_limit,
            fallback_query_timeout=args.fallback_query_timeout,
        )
        files, list_src = await _load_project_files(client, fw_args)
        file_pool = _pick_files(files, args.file_pool_size, args.offset)
        report["file_list_source"] = list_src
        report["file_pool_size_config"] = args.file_pool_size
        report["file_pool_actual"] = len(file_pool)

        prefix = (args.chat_name_prefix or "").strip()
        tail = f"cache-random-activity:{_iso_now()}:p{args.project_id}"
        desc = f"{prefix}{tail}" if prefix else tail
        report["chat_description"] = desc

        chat_id = await _retry(
            lambda: client.create_chat(desc),
            args.retries,
            args.base_sleep,
            "create_chat",
        )
        report["chat_id"] = chat_id

        max_turns = max(1, int(args.max_turns))
        for step in range(1, max_turns + 1):
            kind, prompt, extra = _random_activity_prompt(
                rng,
                args.actor_mention,
                file_pool=file_pool,
                weights=weights,
            )

            await _retry(
                lambda p=prompt: client.post_message(chat_id, p),
                args.retries,
                args.base_sleep,
                f"post:{chat_id}:{step}:{kind}",
            )
            reply_obj = await _retry(
                lambda: _wait_reply(
                    client,
                    chat_id,
                    args.wait_timeout,
                    args.actor_mention,
                ),
                args.retries,
                args.base_sleep,
                f"wait:{chat_id}",
            )
            reply_text = _extract_latest_message(reply_obj, args.actor_mention)
            excerpt = (reply_text or "")[:800]

            row: dict[str, Any] = {
                "step": step,
                "kind": kind,
                "reply_char_len": len(reply_text or ""),
                "reply_word_estimate": len((reply_text or "").split()),
                "reply_excerpt": excerpt,
                "ok": bool((reply_text or "").strip()),
                **extra,
            }
            turns_list.append(row)

            if args.out:
                _write_report_snapshot(
                    Path(args.out).resolve(),
                    report,
                    turns_done=step,
                    max_turns=max_turns,
                    started_ts=started,
                    status="in_progress",
                    save_note="random_activity_flush",
                    exc=None,
                )

            await asyncio.sleep(max(0.0, args.per_turn_sleep))

        report["status"] = "ok"
        if args.out:
            _write_report_snapshot(
                Path(args.out).resolve(),
                report,
                turns_done=max_turns,
                max_turns=max_turns,
                started_ts=started,
                status="ok",
                save_note="final",
                exc=None,
            )
    finally:
        await client.aclose()

    return report


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--url", default=os.environ.get("COLLOQUIUM_URL", "http://localhost:8008"))
    ap.add_argument("--username", default=os.environ.get("COLLOQUIUM_USERNAME", "copilot"))
    ap.add_argument("--password", default=os.environ.get("COLLOQUIUM_PASSWORD", ""))
    ap.add_argument("--password-file", default=cq_cred.default_password_file_for_cli())
    ap.add_argument("--project-id", type=int, default=2)
    ap.add_argument("--max-turns", type=int, default=40)
    ap.add_argument("--seed", type=int, default=0, help="0 = seed from time.")
    ap.add_argument("--actor-mention", default="@grok4f")
    ap.add_argument("--wait-timeout", type=int, default=120)
    ap.add_argument("--per-turn-sleep", type=float, default=0.8)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--base-sleep", type=float, default=1.5)
    ap.add_argument("--list-timeout", type=float, default=180.0)
    ap.add_argument("--fallback-sql-limit", type=int, default=8000)
    ap.add_argument("--fallback-query-timeout", type=int, default=120)
    ap.add_argument("--chat-name-prefix", default="")
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument(
        "--file-pool-size",
        type=int,
        default=12,
        help="Сколько файлов в пуле для редкого file_one_int (после фильтра расширений).",
    )
    ap.add_argument("--out", default="", help="JSON отчёт с промежуточными снимками.")
    ap.add_argument("--w-ping", type=float, default=0.22, help="Вес сценария ping/PONG.")
    ap.add_argument("--w-micro", type=float, default=0.28, help="Вес микро-вопроса.")
    ap.add_argument("--w-ttt", type=float, default=0.22, help="Вес крестиков-ноликов (ход одним токеном).")
    ap.add_argument("--w-digit", type=float, default=0.18, help="Вес echo одного числа.")
    ap.add_argument("--w-file", type=float, default=0.10, help="Вес @attached_file + одно число.")
    args = ap.parse_args()

    if int(args.seed) == 0:
        args.seed = int(time.time()) % (2**31)

    out_path = Path(args.out).resolve() if (args.out or "").strip() else None
    try:
        payload = asyncio.run(run(args))
    except BaseException:
        if out_path is not None:
            print(f"Снимок (если был): {out_path}", file=sys.stderr)
        raise

    if args.out.strip():
        print(f"Saved report: {out_path}")
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
