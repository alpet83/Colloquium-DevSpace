#!/usr/bin/env python3
"""Один чат, быстро растущий контекст: каждый раунд файла — @attached_file# и серия вопросов.

По умолчанию один запрос на файл; с --queries-per-file 2|3 на один и тот же файл подряд идут
разные read-only задачи (сначала общее число строк, затем строки-комментарии, затем строки кода).
Так индекс чата меняется реже между соседними LLM-вызовами, и проще поймать tail_append / DELTA_SAFE
в метриках без лишних index_changed от переиндексации между файлами.

Зачем: по ответам и ground truth (read_file) видно окно постов, лимит токенов, fresh_files
(build_context в llm_interactor.py; assemble_posts).

Режим **--until-window-shift**: после каждого LLM-хода (начиная с --min-llm-steps-before-metrics)
читает хвост context_cache_metrics по chat_id. Успех = в хвосте есть хотя бы одна строка
DELTA_SAFE (типично tail_append_detected) и хотя бы одна FULL с reason из множества
сдвига окна (head_posts_changed / index_changed / attachments_or_spans_changed).
Проходы по списку файлов повторяются, пока условие не выполнится или не исчерпан --max-llm-steps.
Рекомендация: --max-total-turns 0 --max-files высокий --min-lines 200..400 --queries-per-file 2|3.

Авторизация: ``cqds_credentials.resolve_password`` — ``--password`` / ``COLLOQUIUM_PASSWORD`` /
``--password-file`` / ``COLLOQUIUM_PASSWORD_FILE`` / по умолчанию ``mcp-tools/cqds_mcp_auth.secret``.

Зависимости: httpx, colloquium_httpx_client (без cqds_client / пакета mcp — совместимо с venv ядра).
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
from typing import Any

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


def _parse_full_shift_reasons(arg: str) -> frozenset[str]:
    raw = (arg or "").strip()
    if not raw:
        return frozenset(
            {
                "head_posts_changed",
                "index_changed",
                "attachments_or_spans_changed",
            }
        )
    parts = {p.strip() for p in raw.split(",") if p.strip()}
    return frozenset(parts)


def _window_shift_detected(
    rows: list[dict[str, Any]],
    *,
    shift_reasons: frozenset[str],
    min_delta_rows: int,
) -> tuple[bool, str]:
    """rows: новые сверху (ORDER BY metric_id DESC)."""
    if not rows:
        return False, "no_metric_rows"
    delta_n = sum(1 for r in rows if str(r.get("mode") or "") == "DELTA_SAFE")
    if delta_n < max(1, int(min_delta_rows)):
        return False, f"delta_safe_rows={delta_n}_need_{min_delta_rows}"
    for r in rows:
        if str(r.get("mode") or "") != "FULL":
            continue
        reason = str(r.get("reason") or "")
        if reason in shift_reasons:
            return True, f"full_reason={reason}:metric_id={r.get('metric_id')}"
    return False, "no_full_with_shift_reason"


async def _fetch_cache_metrics_tail(
    client: ColloquiumHttpxClient,
    project_id: int,
    chat_id: int,
    depth: int,
    *,
    query_timeout: int,
    retries: int,
    base_sleep: float,
) -> list[dict[str, Any]]:
    cid = int(chat_id)
    lim = max(5, min(int(depth), 500))
    q = (
        f"SELECT mode, reason, sent_tokens, metric_id FROM context_cache_metrics "
        f"WHERE chat_id = {cid} ORDER BY metric_id DESC LIMIT {lim}"
    )

    async def _run():
        result = await client.query_db(project_id, q, timeout=query_timeout)
        return _parse_query_db_rows(result)

    rows_raw = await _retry(_run, retries, base_sleep, "query_db:context_cache_metrics")
    out: list[dict[str, Any]] = []
    for r in rows_raw:
        if len(r) < 4:
            continue
        try:
            mid = int(r[3])
        except (TypeError, ValueError):
            mid = 0
        try:
            st = int(r[2] or 0)
        except (TypeError, ValueError):
            st = 0
        out.append(
            {
                "mode": str(r[0] or ""),
                "reason": str(r[1] or ""),
                "sent_tokens": st,
                "metric_id": mid,
            }
        )
    return out


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


async def _retry(coro_factory, retries: int, base_sleep: float, tag: str):
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


def _pick_files(
    files: list[dict],
    max_files: int,
    offset: int,
    *,
    include_all_extensions: bool = False,
) -> list[dict]:
    selected: list[dict] = []
    for f in files[offset:]:
        name = str(f.get("file_name", "") or "")
        if not include_all_extensions:
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
    chosen_len: int,
    turns_done: int,
    max_turns: int | None,
    started_ts: int,
    status: str,
    save_note: str,
    exc: BaseException | None = None,
) -> None:
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


def _iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _line_count(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + 1


# Ключ фазы, затем текст «primary task» для этой фазы (англ., чтобы модель стабильнее парсила поля).
PHASE_TEMPLATES: list[tuple[str, str]] = [
    (
        "total_lines",
        "Primary task for the **attached file**: report **total_line_estimate** (integer) — visible full body line count.",
    ),
    (
        "comment_lines",
        "Primary task for the **attached file**: report **comment_line_estimate** (integer) — lines that are "
        "**comment-only** or **wholly comments** (#, //, /* */, <!-- -->, docstring-only lines: use your judgment). "
        "If language/embedding unclear, say so in **note**.",
    ),
    (
        "code_lines",
        "Primary task for the **attached file**: report **code_line_estimate** (integer) — **substantive code** lines "
        "(exclude blank lines and comment-only lines).",
    ),
]


def _phase_specs(queries_per_file: int) -> list[tuple[str, str]]:
    q = max(1, min(int(queries_per_file), len(PHASE_TEMPLATES)))
    return PHASE_TEMPLATES[:q]


def _build_prompt(
    actor: str,
    *,
    file_id: int,
    file_name: str,
    phase_key: str,
    phase_instruction: str,
    phase_index: int,
    phases_total: int,
    file_round: int,
    files_total: int,
    global_step: int,
    global_total: int,
) -> str:
    act = actor.strip()
    if not act.startswith("@"):
        act = f"@{act}"
    return (
        f"{act} Synthetic context-window probe (read-only, no code edits).\n\n"
        f"File `{file_name}` (file_id={file_id}). "
        f"Round **{file_round}/{files_total}**, phase **{phase_index}/{phases_total}** (`{phase_key}`). "
        f"Global LLM step **{global_step}/{global_total}**.\n\n"
        f"{phase_instruction}\n\n"
        "Reply using this structure (markdown headings ok):\n"
        "1) **files_visible** — one line per distinct file whose body you still see: "
        "`file_id=<id> path=<path> estimated_lines=<int>`.\n"
        f"2) **phase_answer** — one line: `{phase_key}=<integer>` matching the primary task above.\n"
        "3) **total_visible_lines** — one integer: sum of estimated_lines over distinct visible files.\n"
        "4) **user_posts_visible** — approximate integer.\n"
        "5) **assistant_posts_visible** — approximate integer.\n"
        "6) **note** — truncation, sliding window, or index churn.\n\n"
        "Use only text in context; do not invent hidden lines.\n\n"
        f"@attached_file#{file_id}"
    )


async def run(args: argparse.Namespace) -> dict[str, Any]:
    password, _ = cq_cred.resolve_password(args.password, args.password_file)
    if not password:
        raise RuntimeError("Password is required.")

    client = ColloquiumHttpxClient(base_url=args.url, username=args.username, password=password)
    started = int(time.time())
    qpf = max(1, min(int(args.queries_per_file), len(PHASE_TEMPLATES)))
    phase_specs = _phase_specs(qpf)
    turns_list: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "scenario": "context_window_growth_single_chat",
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "base_url": args.url,
        "project_id": args.project_id,
        "actor_mention": args.actor_mention,
        "chat_description": "",
        "turns": turns_list,
        # alias для _write_report_snapshot из filewalk (считает len(results))
        "results": turns_list,
        "notes": [
            "Ground truth `cumulative_true_lines` sums full file bodies from read_file for files touched so far; "
            "visible context may be smaller (token limit, fresh_files filter, post relevance).",
        ],
        "queries_per_file": qpf,
        "query_phase_keys": [p[0] for p in phase_specs],
        "include_all_extensions": bool(args.include_all_extensions),
    }

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
        chosen = _pick_files(
            files,
            args.max_files,
            args.offset,
            include_all_extensions=bool(args.include_all_extensions),
        )
        if args.min_lines > 0:
            filtered: list[dict[str, Any]] = []
            for item in chosen:
                fid = int(item.get("id") or item.get("file_id") or 0)
                if fid <= 0:
                    continue
                try:
                    raw = await _retry(
                        lambda f=fid: client.read_file(f),
                        args.retries,
                        args.base_sleep,
                        f"read_file:{fid}",
                    )
                except httpx.HTTPStatusError as e:
                    code = e.response.status_code if e.response is not None else 0
                    if code == 404 and args.skip_missing_files:
                        continue
                    raise
                if _line_count(str(raw or "")) >= args.min_lines:
                    filtered.append(item)
            chosen = filtered

        if not chosen:
            raise RuntimeError(
                f"No candidate files after filters: list_src={list_src}, files_from_api={len(files)}, "
                f"offset={int(args.offset)}, max_files={int(args.max_files)}, "
                f"include_all_extensions={bool(args.include_all_extensions)}. "
                f"Проверьте --project-id и заполненность индекса в Core; опция --include-all-extensions снимает фильтр по расширениям."
            )

        if args.max_total_turns > 0:
            chosen = chosen[: args.max_total_turns]

        prefix = (args.chat_name_prefix or "").strip()
        tail = f"cache-context-growth:{_iso_now()}:p{args.project_id}"
        desc = f"{prefix}{tail}" if prefix else tail
        report["chat_description"] = desc
        report["file_list_source"] = list_src

        chat_id = await _retry(
            lambda: client.create_chat(desc),
            args.retries,
            args.base_sleep,
            "create_chat",
        )
        report["chat_id"] = chat_id

        cumulative_true_lines = 0
        total_files = len(chosen)
        global_total = total_files * qpf
        global_total_prompt = (
            int(args.max_llm_steps) if args.until_window_shift else global_total
        )
        global_step = 0
        shift_reasons = _parse_full_shift_reasons(args.full_shift_reasons)
        report["until_window_shift"] = bool(args.until_window_shift)
        if args.until_window_shift:
            report["max_llm_steps"] = int(args.max_llm_steps)
            report["min_llm_steps_before_metrics"] = int(args.min_llm_steps_before_metrics)
            report["metrics_tail_depth"] = int(args.metrics_tail_depth)
            report["min_delta_rows_in_tail"] = int(args.min_delta_rows_in_tail)
            report["full_shift_reasons"] = sorted(shift_reasons)

        stop_early = False
        growth_pass = 0
        while not stop_early:
            growth_pass += 1
            if args.until_window_shift:
                report["growth_pass"] = growth_pass

            for idx, item in enumerate(chosen, start=1):
                file_id = int(item.get("id") or item.get("file_id") or 0)
                file_name = str(item.get("file_name") or "")
                if file_id <= 0 or not file_name:
                    continue

                try:
                    raw = await _retry(
                        lambda: client.read_file(file_id),
                        args.retries,
                        args.base_sleep,
                        f"read_file:{file_id}",
                    )
                except httpx.HTTPStatusError as e:
                    code = e.response.status_code if e.response is not None else 0
                    if code == 404 and args.skip_missing_files:
                        report["turns"].append(
                            {
                                "global_step": None,
                                "file_round": idx,
                                "growth_pass": growth_pass,
                                "file_id": file_id,
                                "file_name": file_name,
                                "skipped_missing_file": True,
                            }
                        )
                        continue
                    raise

                text = str(raw or "")
                true_lines = _line_count(text)
                cumulative_true_lines += true_lines

                for phase_index, (phase_key, phase_instruction) in enumerate(phase_specs, start=1):
                    if args.until_window_shift and global_step >= int(args.max_llm_steps):
                        report["window_shift_detection"] = {
                            "ok": False,
                            "detail": "max_llm_steps_exhausted",
                            "global_step": global_step,
                            "growth_pass": growth_pass,
                        }
                        stop_early = True
                        break

                    global_step += 1
                    prompt = _build_prompt(
                        args.actor_mention,
                        file_id=file_id,
                        file_name=file_name,
                        phase_key=phase_key,
                        phase_instruction=phase_instruction,
                        phase_index=phase_index,
                        phases_total=len(phase_specs),
                        file_round=idx,
                        files_total=total_files,
                        global_step=global_step,
                        global_total=global_total_prompt,
                    )
                    await _retry(
                        lambda p=prompt: client.post_message(chat_id, p),
                        args.retries,
                        args.base_sleep,
                        f"post:{chat_id}:{file_id}:{phase_key}",
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

                    turn_row: dict[str, Any] = {
                        "global_step": global_step,
                        "file_round": idx,
                        "growth_pass": growth_pass,
                        "phase": phase_key,
                        "phase_index": phase_index,
                        "file_id": file_id,
                        "file_name": file_name,
                        "true_lines_this_file": true_lines,
                        "cumulative_true_lines_all_files_so_far": cumulative_true_lines,
                        "reply_excerpt": (reply_text or "")[:4000],
                        "ok": bool((reply_text or "").strip()),
                    }

                    if (
                        args.until_window_shift
                        and global_step >= int(args.min_llm_steps_before_metrics)
                    ):
                        try:
                            tail = await _fetch_cache_metrics_tail(
                                client,
                                args.project_id,
                                chat_id,
                                int(args.metrics_tail_depth),
                                query_timeout=int(args.metrics_query_timeout),
                                retries=args.retries,
                                base_sleep=args.base_sleep,
                            )
                        except Exception as poll_exc:  # noqa: BLE001
                            turn_row["metrics_poll_error"] = f"{type(poll_exc).__name__}: {poll_exc}"
                            tail = []
                        turn_row["metrics_tail_preview"] = tail[:5]
                        ok_shift, why = _window_shift_detected(
                            tail,
                            shift_reasons=shift_reasons,
                            min_delta_rows=int(args.min_delta_rows_in_tail),
                        )
                        turn_row["window_shift_candidate"] = ok_shift
                        turn_row["window_shift_detail"] = why
                        if ok_shift:
                            report["window_shift_detection"] = {
                                "ok": True,
                                "detail": why,
                                "global_step": global_step,
                                "growth_pass": growth_pass,
                                "metrics_tail_head": tail[:12],
                            }
                            stop_early = True

                    report["turns"].append(turn_row)

                    max_turns_snap = (
                        int(args.max_llm_steps) if args.until_window_shift else global_total
                    )
                    if args.out:
                        _write_report_snapshot(
                            Path(args.out).resolve(),
                            report,
                            chosen_len=total_files,
                            turns_done=global_step,
                            max_turns=max_turns_snap,
                            started_ts=started,
                            status="in_progress",
                            save_note="context_growth_flush",
                            exc=None,
                        )

                    await asyncio.sleep(max(0.0, args.per_turn_sleep))

                if stop_early:
                    break

            if stop_early:
                break
            if not args.until_window_shift:
                break

        report["finished_at"] = int(time.time())
        report["elapsed_seconds"] = report["finished_at"] - started
        if args.until_window_shift:
            wd = report.get("window_shift_detection") or {}
            if wd.get("ok"):
                report["status"] = "ok_window_shift"
            elif wd.get("detail") == "max_llm_steps_exhausted":
                report["status"] = "timeout_no_window_shift"
            else:
                report["status"] = "timeout_no_window_shift"
                report["window_shift_detection"] = {
                    "ok": False,
                    "detail": "end_of_file_passes_without_match",
                    "global_step": global_step,
                    "growth_pass": growth_pass,
                }
        else:
            report["status"] = "ok"

        if args.out:
            _write_report_snapshot(
                Path(args.out).resolve(),
                report,
                chosen_len=total_files,
                turns_done=len(report["turns"]),
                max_turns=(
                    int(args.max_llm_steps) if args.until_window_shift else global_total
                ),
                started_ts=started,
                status=str(report.get("status") or "ok"),
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
    ap.add_argument(
        "--password-file",
        default=cq_cred.default_password_file_for_cli(),
        help="Файл с паролем; по умолчанию COLLOQUIUM_PASSWORD_FILE или mcp-tools/cqds_mcp_auth.secret.",
    )
    ap.add_argument("--project-id", type=int, default=2)
    ap.add_argument("--max-files", type=int, default=200)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument(
        "--max-total-turns",
        type=int,
        default=30,
        help="Сколько файлов по порядку после фильтров (не число LLM-шагов; при --queries-per-file>1 шагов будет больше).",
    )
    ap.add_argument(
        "--queries-per-file",
        type=int,
        default=1,
        choices=(1, 2, 3),
        help="Сколько подряд разных read-only вопросов на один файл (1=одна фаза; 2–3=строки/комментарии/код) — реже смена индекса между соседними вызовами.",
    )
    ap.add_argument(
        "--min-lines",
        type=int,
        default=0,
        help="Если >0 — отбирать только файлы с таким числом строк (дороже: read_file на кандидатов).",
    )
    ap.add_argument(
        "--include-all-extensions",
        action="store_true",
        help="Не фильтровать по CODE_EXT (оставить только исключение node_modules) — если индекс без .py/.md и т.п.",
    )
    ap.add_argument("--actor-mention", default="@grok4f")
    ap.add_argument("--wait-timeout", type=int, default=240)
    ap.add_argument("--per-turn-sleep", type=float, default=1.0)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--base-sleep", type=float, default=1.5)
    ap.add_argument("--list-timeout", type=float, default=180.0)
    ap.add_argument("--fallback-sql-limit", type=int, default=8000)
    ap.add_argument("--fallback-query-timeout", type=int, default=120)
    ap.add_argument("--chat-name-prefix", default="")
    ap.add_argument(
        "--out",
        default="",
        help="JSON-отчёт (промежуточные снимки как у filewalk).",
    )
    ap.add_argument(
        "--no-skip-missing-files",
        action="store_true",
        help="Не пропускать сиротские file_id (404).",
    )
    ap.add_argument(
        "--until-window-shift",
        action="store_true",
        help="Повторять проходы по файлам и опрашивать context_cache_metrics до FULL со сдвигом окна "
        "(см. docstring) или до --max-llm-steps.",
    )
    ap.add_argument(
        "--max-llm-steps",
        type=int,
        default=400,
        help="С --until-window-shift: лимит LLM-ходов (постов в чат), затем timeout_no_window_shift.",
    )
    ap.add_argument(
        "--min-llm-steps-before-metrics",
        type=int,
        default=6,
        help="С --until-window-shift: не опрашивать метрики на ранних шагах (cold start / шум).",
    )
    ap.add_argument(
        "--metrics-tail-depth",
        type=int,
        default=80,
        help="С --until-window-shift: LIMIT последних строк context_cache_metrics по chat_id.",
    )
    ap.add_argument(
        "--metrics-query-timeout",
        type=int,
        default=90,
        help="Таймаут exec/query_db при опросе метрик.",
    )
    ap.add_argument(
        "--min-delta-rows-in-tail",
        type=int,
        default=2,
        help="Минимум строк DELTA_SAFE в хвосте метрик до проверки FULL-сдвига.",
    )
    ap.add_argument(
        "--full-shift-reasons",
        default="",
        help="Через запятую доп. FULL-reason (по умолчанию head_posts_changed,index_changed,attachments_or_spans_changed).",
    )
    args = ap.parse_args()
    args.skip_missing_files = not bool(args.no_skip_missing_files)

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
    if str(payload.get("status") or "") == "timeout_no_window_shift":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
