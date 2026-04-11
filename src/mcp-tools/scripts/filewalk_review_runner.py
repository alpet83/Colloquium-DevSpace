#!/usr/bin/env python3
"""Generate useful synthetic chat traffic: file-by-file read-only reviews."""
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

import httpx
from pathlib import Path
from typing import Any

_THIS = Path(__file__).resolve()
_MCP_TOOLS = _THIS.parents[1]
if str(_MCP_TOOLS) not in sys.path:
    sys.path.insert(0, str(_MCP_TOOLS))

from cqds_client import ColloquiumClient
import cqds_credentials as cq_cred
from cqds_helpers import _is_progress_stub


READONLY_GUARD = (
    "Analysis only. Review real code and explain findings. "
    "Do not propose or execute file/code modifications."
)

# Должен совпадать с agent.managers.replication.DEBUG_BYPASS_TAG (регистр в сообщении не важен).
DEBUG_BYPASS_TAG = "#debug_bypass"


def _sample_scope_line(full_text: str, snippet: str) -> str:
    """Счётчики символов/строк, чтобы модель не тратила ответ на догадки об обрезке."""
    full = full_text or ""
    snip = snippet or ""

    def line_count(s: str) -> int:
        return 0 if not s else s.count("\n") + 1

    cf = len(full)
    cs = len(snip)
    lf = line_count(full)
    ls = line_count(snip)

    if cf == 0:
        return "Sample stats: empty file (0 chars, 0 lines)."
    if cs >= cf:
        return (
            f"Sample stats: full file ({cf} chars, {lf} lines) — not truncated by this runner."
        )
    return (
        f"Sample stats: first {cs} chars (~{ls} lines) of {cf} chars ({lf} lines); "
        "tail cut by char limit. Review only visible text; do not infer or summarize hidden lines."
    )

CODE_EXT = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".kt",
    ".c", ".cpp", ".h", ".hpp", ".cs", ".rb", ".php", ".sql", ".sh",
    ".ps1", ".yaml", ".yml", ".toml", ".json", ".md",
}


def _iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _norm_actor(name: str) -> str:
    return name.strip().lstrip("@").lower()


def _latest_actor_message(resp: dict, expected_user: str) -> str | None:
    """Последний пост указанного актора (по id поста) или None."""
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
    client: ColloquiumClient,
    chat_id: int,
    timeout_sec: int,
    expected_actor: str,
) -> dict:
    """Ждём, пока у актора не будет финального текста (не ⏳ / preparing / in progress).

    В replication ответ обычно приходит через edit_post того же progress_post_id — пока идёт
    генерация, в посте маркеры из posts.py / replication.py (см. _is_progress_stub).
    """
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
    """Разбор stdout из /api/project/exec для inline cq_query_db-скрипта."""
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


async def _list_files_fallback_attached(
    client: ColloquiumClient,
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
        rows = _parse_query_db_rows(result)
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
        return out

    files = await _retry(_run, retries, base_sleep, "query_db:attached_files")
    if not files:
        raise RuntimeError("Fallback attached_files returned no rows.")
    return files


async def _load_project_files(
    client: ColloquiumClient,
    args: argparse.Namespace,
) -> tuple[list[dict[str, Any]], str]:
    """Сначала file_index с увеличенным HTTP-таймаутом; при сбое — attached_files."""

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


async def _retry(coro_factory, retries: int, base_sleep: float, tag: str):
    for attempt in range(retries + 1):
        try:
            return await coro_factory()
        except Exception as exc:  # noqa: BLE001
            text = str(exc).lower()
            if attempt >= retries:
                raise
            # light rate-limit/backpressure handling
            if "429" in text or "rate" in text or "timeout" in text:
                delay = base_sleep * (2 ** attempt) + random.uniform(0.0, 0.25)
            else:
                delay = base_sleep + random.uniform(0.0, 0.2)
            print(
                f"[retry] {tag} attempt={attempt+1} delay={delay:.2f}s err={exc}",
                file=sys.stderr,
            )
            await asyncio.sleep(delay)


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


def _progress_line(turns_done: int, total_planned: int) -> None:
    """Одна «живая» строка для tail -f: без \\n, в конце \\t\\r перерисовывает линию."""
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sys.stdout.write(
        f"[{ts}] выполнен прогон на {turns_done}/{total_planned} итераций   \t\r"
    )
    sys.stdout.flush()


def _format_progress_total_duration(seconds: int) -> str:
    sec = max(0, int(seconds))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        human = f"{h}ч {m}м {s}с"
    elif m:
        human = f"{m}м {s}с"
    else:
        human = f"{s}с"
    return f"Итого время прогона: {human} ({sec} с)"


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
    chosen_len: int,
    turns_done: int,
    max_turns: int | None,
    started_ts: int,
    status: str,
    save_note: str,
    exc: BaseException | None = None,
) -> None:
    """Пишет текущее состояние отчёта (периодически или при вылете)."""
    report["processed_files"] = len(report["results"])
    report["total_candidate_files"] = chosen_len
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


async def run(args: argparse.Namespace) -> dict[str, Any]:
    password, _ = cq_cred.resolve_password(args.password, args.password_file)
    if not password:
        raise RuntimeError("Password is required (same sources as mcp-tools).")
    if int(args.files_per_chat) < 1:
        raise RuntimeError("files_per_chat must be >= 1")

    out_path = Path(args.out).resolve() if (args.out or "").strip() else None
    flush_every = max(0, int(args.flush_every))

    client = ColloquiumClient(base_url=args.url, username=args.username, password=password)
    started = int(time.time())
    report: dict[str, Any] = {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "base_url": args.url,
        "actor_mention": args.actor_mention,
        "project_id": args.project_id,
        "offset": int(args.offset),
        "files_per_chat": args.files_per_chat,
        "max_files": args.max_files,
        "list_timeout_sec": args.list_timeout,
        "file_list_source": "",
        "results": [],
        "debug_bypass": bool(getattr(args, "debug_bypass", False)),
    }
    chosen: list[dict[str, Any]] = []
    turns_done = 0
    max_turns: int | None = args.max_total_turns if args.max_total_turns > 0 else None

    try:
        await _retry(lambda: client.select_project(args.project_id), args.retries, args.base_sleep, "select_project")
        files, list_src = await _load_project_files(client, args)
        report["file_list_source"] = list_src
        chosen = _pick_files(files, args.max_files, args.offset)
        if not chosen:
            raise RuntimeError("No candidate files selected by filters.")

        current_chat_id = 0
        in_chat_counter = 0
        prefix = (args.chat_name_prefix or "").strip()

        async def _after_one_file_iteration() -> None:
            nonlocal in_chat_counter, turns_done
            in_chat_counter += 1
            turns_done += 1
            _iter_total = max_turns if max_turns is not None else len(chosen)
            _progress_line(turns_done, _iter_total)
            if (
                out_path
                and flush_every > 0
                and len(report["results"]) % flush_every == 0
            ):
                _write_report_snapshot(
                    out_path,
                    report,
                    chosen_len=len(chosen),
                    turns_done=turns_done,
                    max_turns=max_turns,
                    started_ts=started,
                    status="in_progress",
                    save_note="periodic_flush",
                    exc=None,
                )
            await asyncio.sleep(max(0.0, args.per_file_sleep))

        for item in chosen:
            if max_turns is not None and turns_done >= max_turns:
                break
            if current_chat_id <= 0 or in_chat_counter >= args.files_per_chat:
                if current_chat_id > 0 and args.batch_sleep > 0:
                    await asyncio.sleep(args.batch_sleep)
                tail = f"cache-filewalk:{_iso_now()}:project{args.project_id}"
                desc = f"{prefix}{tail}" if prefix else tail
                current_chat_id = await _retry(
                    lambda: client.create_chat(desc), args.retries, args.base_sleep, "create_chat"
                )
                in_chat_counter = 0

            file_id = int(item.get("id") or item.get("file_id") or 0)
            file_name = str(item.get("file_name") or "")
            if file_id <= 0 or not file_name:
                continue

            if args.skip_missing_files:
                try:
                    content = await client.read_file(file_id)
                except httpx.HTTPStatusError as e:
                    code = e.response.status_code if e.response is not None else 0
                    if code == 404:
                        report["results"].append(
                            {
                                "chat_id": current_chat_id,
                                "file_id": file_id,
                                "file_name": file_name,
                                "reply_excerpt": "(пропуск: нет содержимого / сиротский file_id, HTTP 404)",
                                "ok": False,
                                "skipped_missing_file": True,
                            }
                        )
                        await _after_one_file_iteration()
                        continue
                    raise
                raw = str(content or "")
            else:
                content = await _retry(
                    lambda: client.read_file(file_id),
                    args.retries,
                    args.base_sleep,
                    f"read_file:{file_id}",
                )
                raw = str(content or "")
            snippet = raw[: args.max_chars]
            scope = _sample_scope_line(raw, snippet)
            prompt = (
                f"{args.actor_mention} {READONLY_GUARD}\n\n"
                f"File: `{file_name}` (file_id={file_id}).\n"
                f"{scope}\n"
                "Task: brief review in 4 bullets:\n"
                "1) top 1-2 risk(s),\n"
                "2) possible improvement,\n"
                "3) confidence (low/med/high),\n"
                "4) one read-only verification step.\n\n"
                f"```text\n{snippet}\n```"
            )
            if args.debug_bypass:
                prompt = f"{prompt.rstrip()}\n\n{DEBUG_BYPASS_TAG}"

            await _retry(
                lambda: client.post_message(current_chat_id, prompt),
                args.retries,
                args.base_sleep,
                f"post_message:{current_chat_id}:{file_id}",
            )
            reply = await _retry(
                lambda: _wait_reply(
                    client, current_chat_id, args.wait_timeout, args.actor_mention
                ),
                args.retries,
                args.base_sleep,
                f"wait_reply:{current_chat_id}",
            )
            latest = _extract_latest_message(reply, args.actor_mention)
            report["results"].append(
                {
                    "chat_id": current_chat_id,
                    "file_id": file_id,
                    "file_name": file_name,
                    "reply_excerpt": latest[:600],
                    "ok": bool(latest),
                }
            )
            await _after_one_file_iteration()

        # Завершили цикл: перевод строки, чтобы после tail -f шла обычная строка «Saved report».
        sys.stdout.write("\n")
        sys.stdout.flush()
        _elapsed = int(time.time()) - started
        sys.stdout.write(_format_progress_total_duration(_elapsed) + "\n")
        sys.stdout.flush()

        if out_path:
            _write_report_snapshot(
                out_path,
                report,
                chosen_len=len(chosen),
                turns_done=turns_done,
                max_turns=max_turns,
                started_ts=started,
                status="ok",
                save_note="final",
                exc=None,
            )
        else:
            report["status"] = "ok"
            report["processed_files"] = len(report["results"])
            report["total_candidate_files"] = len(chosen)
            report["turns_done"] = turns_done
            if max_turns is not None:
                report["max_total_turns"] = max_turns
            report["started_at"] = started
            report["finished_at"] = int(time.time())
            report["elapsed_seconds"] = report["finished_at"] - report["started_at"]
            report["save_note"] = "final"
    except BaseException as exc:
        interrupted = isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError))
        if out_path:
            _write_report_snapshot(
                out_path,
                report,
                chosen_len=len(chosen),
                turns_done=turns_done,
                max_turns=max_turns,
                started_ts=started,
                status="interrupted" if interrupted else "error",
                save_note="interrupted" if interrupted else "exception",
                exc=exc,
            )
        raise
    finally:
        await client.aclose()

    if not out_path:
        return report
    # Поля уже в report после последнего _write_report_snapshot
    return report


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Pseudo-useful filewalk review traffic runner. "
            "По умолчанию 1 файл = 1 чат — плотнее context_cache_metrics при Phase 1."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Ускоренное наполнение статистики (пример): "
            "  --files-per-chat 1 --max-total-turns 50 --offset N "
            "(сдвигайте offset между прогонами).\n"
            "Без вызова LLM (только build_context + context_cache_metrics): "
            "  --debug-bypass [--wait-timeout 20]"
        ),
    )
    ap.add_argument("--url", default=os.environ.get("COLLOQUIUM_URL", "http://localhost:8008"))
    ap.add_argument("--username", default=os.environ.get("COLLOQUIUM_USERNAME", "copilot"))
    ap.add_argument("--password", default=os.environ.get("COLLOQUIUM_PASSWORD", ""))
    ap.add_argument("--password-file", default=cq_cred.default_password_file_for_cli())
    ap.add_argument("--project-id", type=int, default=2)
    ap.add_argument("--max-files", type=int, default=80)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument(
        "--files-per-chat",
        type=int,
        default=1,
        help="1 = отдельный чат на каждый файл (рекомендуется для Phase 1 / плотность метрик).",
    )
    ap.add_argument("--max-chars", type=int, default=2800)
    ap.add_argument("--actor-mention", default="@grok4f")
    ap.add_argument(
        "--wait-timeout",
        type=int,
        default=180,
        help="Секунды ожидания ответа модели на один файл (по умолчанию 180 = 3 мин)",
    )
    ap.add_argument(
        "--per-file-sleep",
        type=float,
        default=0.85,
        help="Пауза между файлами (с); чуть ниже для более быстрых батчей, при 429 поднимите.",
    )
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--base-sleep", type=float, default=1.5)
    ap.add_argument(
        "--list-timeout",
        type=float,
        default=180.0,
        help="HTTP timeout (s) for GET /api/project/file_index",
    )
    ap.add_argument(
        "--fallback-sql-limit",
        type=int,
        default=8000,
        help="LIMIT for attached_files fallback SELECT",
    )
    ap.add_argument(
        "--fallback-query-timeout",
        type=int,
        default=120,
        help="exec timeout (s) for attached_files SQL (max 300 on server)",
    )
    ap.add_argument(
        "--max-total-turns",
        type=int,
        default=0,
        help="Cap processed files (0 = no cap)",
    )
    ap.add_argument(
        "--chat-name-prefix",
        default="",
        help="Prepended to auto chat description (e.g. mybatch-)",
    )
    ap.add_argument(
        "--batch-sleep",
        type=float,
        default=0.0,
        help="Seconds to sleep before opening each new chat after the first",
    )
    ap.add_argument("--out", default="")
    ap.add_argument(
        "--flush-every",
        type=int,
        default=5,
        help="При --out: сохранять JSON каждые N обработанных файлов (0 = только финал и при ошибке).",
    )
    ap.add_argument(
        "--no-skip-missing-files",
        action="store_true",
        help="При 404 на read_file не пропускать файл, а ретраить как обычно (дольше на «сиротах»).",
    )
    ap.add_argument(
        "--debug-bypass",
        action="store_true",
        help=(
            "Добавить #debug_bypass в каждый пост: сервер не вызывает провайдера, "
            "но выполняет build_context и пишет context_cache_metrics (быстрый Phase 1)."
        ),
    )
    args = ap.parse_args()
    args.skip_missing_files = not bool(args.no_skip_missing_files)

    out_display = Path(args.out).resolve() if (args.out or "").strip() else None
    try:
        payload = asyncio.run(run(args))
    except BaseException:
        if out_display is not None:
            print(f"Снимок отчёта (если успели записать): {out_display}", file=sys.stderr)
        raise

    if args.out.strip():
        print(f"Saved report: {out_display}")
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
