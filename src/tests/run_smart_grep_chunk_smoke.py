#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Дымовой тест: GET index_meta + цикл POST smart_grep/chunk до scan_complete.

Запуск из корня cqds:
  python src/tests/run_smart_grep_chunk_smoke.py --project-id 1

Сводка метрик (для крупного проекта, JSON в stdout):
  python src/tests/run_smart_grep_chunk_smoke.py --project-id 2 --query foo \\
    --limit-files 80 --max-hits 500 --max-returned-items 100 --stats-json

Бенчмарк «почти без лимитов» (ядро само режет: limit_files≤200, max_hits≤50000, см. project_routes):
  python src/tests/run_smart_grep_chunk_smoke.py --project-id 2 --query def \\
    --limit-files 200 --max-hits 50000 --stats-json

Авторизация как compare_smart_grep_search_modes (cqds_credentials / cqds_mcp_auth.secret).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_MCP := _REPO / "mcp-tools"))


def _post(url: str, cookie: str, body: dict | None, method: str = "POST") -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "Cookie": f"session_id={cookie}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, json.loads(raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"detail": raw[:2000]}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--api-root", default=os.environ.get("SMART_GREP_API_ROOT") or "http://localhost:8008/api")
    p.add_argument("--project-id", type=int, required=True)
    p.add_argument("--query", default="import")
    p.add_argument("--path-prefix", default="")
    p.add_argument("--limit-files", type=int, default=5)
    p.add_argument("--max-hits", type=int, default=200)
    p.add_argument(
        "--stats-json",
        action="store_true",
        help="В конце вывести одну строку JSON в stdout (метрики ядра + оценка страниц MCP)",
    )
    p.add_argument(
        "--max-returned-items",
        type=int,
        default=100,
        help="Для --stats-json: размер страницы MCP (max_returned_items), 1..500",
    )
    args = p.parse_args()
    root = args.api_root.rstrip("/")
    page_size = max(1, min(int(args.max_returned_items), 500))

    from cqds_credentials import login_base_from_api_root, resolve_password, session_cookie_from_login

    base = login_base_from_api_root(root)
    pw, _src = resolve_password(None, None)
    user = os.environ.get("COLLOQUIUM_USERNAME", "copilot")
    cookie = session_cookie_from_login(base, user, pw, timeout=30.0)
    print("login ok", file=sys.stderr)

    wall0 = time.monotonic()
    index_meta_http = 0
    chunk_http_ok = 0
    chunk_http_409 = 0
    last_total_ids: int | None = None
    last_truncated_max_hits = False

    st, meta = _post(f"{root}/project/{args.project_id}/index_meta", cookie, None, "GET")
    index_meta_http += 1
    if st != 200:
        print("index_meta failed", st, meta, file=sys.stderr)
        return 2
    epoch = int(meta.get("index_epoch", 0))
    print("index_epoch=%d" % epoch, file=sys.stderr)

    offset = 0
    total_hits = 0
    chunk_hit_counts: list[int] = []
    last_chunk: dict = {}
    truncated_max_hits_any = False
    while True:
        body = {
            "project_id": args.project_id,
            "index_epoch": epoch,
            "path_prefix": args.path_prefix,
            "offset": offset,
            "limit_files": args.limit_files,
            "max_hits": args.max_hits,
            "query": args.query,
            "mode": "code",
            "profile": "all",
            "search_mode": "project_registered",
        }
        st, chunk = _post(f"{root}/project/smart_grep/chunk", cookie, body)
        if st == 409:
            chunk_http_409 += 1
            print("stale epoch, retry with:", chunk, file=sys.stderr)
            epoch = int((chunk.get("detail") or {}).get("current_epoch", epoch))
            continue
        if st != 200:
            print("chunk failed", st, chunk, file=sys.stderr)
            return 2
        chunk_http_ok += 1
        nh = len(chunk.get("hits") or [])
        chunk_hit_counts.append(nh)
        total_hits += nh
        epoch = int(chunk.get("index_epoch", epoch))
        offset = int(chunk.get("next_offset", 0))
        last_chunk = chunk
        last_total_ids = int(chunk.get("total_ids_in_scope", 0) or 0)
        last_truncated_max_hits = bool(chunk.get("truncated_by_max_hits"))
        if last_truncated_max_hits:
            truncated_max_hits_any = True
        print(
            "chunk offset=%d next=%d hits=%d complete=%s"
            % (
                chunk.get("offset"),
                offset,
                nh,
                chunk.get("scan_complete"),
            ),
            file=sys.stderr,
        )
        if chunk.get("scan_complete"):
            break
        if nh == 0 and offset >= int(chunk.get("total_ids_in_scope", 0)):
            break

    scan_complete = bool(last_chunk.get("scan_complete"))
    elapsed_wall_sec = round(time.monotonic() - wall0, 3)
    est_pages_merged = int(math.ceil(total_hits / page_size)) if total_hits else 0
    # За один HTTP-чанк: 1 ответ MCP с hits; если nh > page_size, finalize кладёт хвост в store → ещё (ceil(nh/page_size)-1) cq_fetch_result(handle).
    handle_fetches_per_chunk = [max(0, int(math.ceil(nh / page_size)) - 1) for nh in chunk_hit_counts]
    mcp_handle_fetches_sum = sum(handle_fetches_per_chunk)
    mcp_est_tool_calls_total = chunk_http_ok + mcp_handle_fetches_sum

    stats: dict = {
        "project_id": args.project_id,
        "query": args.query,
        "path_prefix": args.path_prefix,
        "limit_files": args.limit_files,
        "max_hits_per_chunk": args.max_hits,
        "max_returned_items_assumed": page_size,
        "core_http_get_index_meta": index_meta_http,
        "core_http_post_smart_grep_chunk_200": chunk_http_ok,
        "core_http_post_smart_grep_chunk_409": chunk_http_409,
        "core_http_total": index_meta_http + chunk_http_ok + chunk_http_409,
        "sum_hits_all_chunks": total_hits,
        "final_index_epoch": epoch,
        "total_ids_in_scope_last": last_total_ids,
        "last_chunk_truncated_by_max_hits": last_truncated_max_hits,
        "truncated_by_max_hits_any_chunk": truncated_max_hits_any,
        "scan_complete": scan_complete,
        "elapsed_wall_sec": elapsed_wall_sec,
        "http_chunk_count": chunk_http_ok,
        "hits_per_http_chunk": chunk_hit_counts,
        "mcp_est_tool_calls_chunk_only": chunk_http_ok,
        "mcp_est_cq_fetch_result_handle_calls": mcp_handle_fetches_sum,
        "mcp_est_tool_calls_chunk_plus_handle_paging": mcp_est_tool_calls_total,
        "mcp_est_pages_if_single_merged_hit_list": est_pages_merged,
        "mcp_note": (
            "chunk_only = число HTTP-чанков (cq_start_grep + cq_fetch_result с chunk_continuation). "
            "handle_calls = сумма по чанкам max(0, ceil(nh/max_returned)-1). "
            "Итого MCP ~ chunk_only + handle_calls."
        ),
    }

    print("OK total_hits=%d final_epoch=%d" % (total_hits, epoch), file=sys.stderr)
    sys.stderr.flush()
    if args.stats_json:
        line = json.dumps(stats, ensure_ascii=False) + "\n"
        try:
            sys.stdout.buffer.write(line.encode("utf-8"))
            sys.stdout.buffer.flush()
        except Exception:
            print(json.dumps(stats, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
