#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тесты пейджинга и сжатия cq_start_grep (finalize + cq_fetch_result), без поднятого Colloquium."""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mcp-tools"))

from cqds_result_pages import (  # noqa: E402
    LINE_TEXT_MAX,
    MATCH_TEXT_MAX,
    ResultPageStore,
    compress_smart_grep_hit,
    compress_smart_grep_hits,
    extra_result_page,
    finalize_smart_grep_response,
    reassemble_all_hits_from_paged_response,
)


def _fake_hits(n: int) -> list[dict]:
    return [
        {
            "file_id": i,
            "file_name": f"src/m{i % 10}.py",
            "line": i + 1,
            "line_text": f"needle line {i}",
            "match": "needle",
            "context_before": [],
            "context_after": [],
        }
        for i in range(n)
    ]


def test_finalize_no_paging_small_result():
    async def _run():
        store = ResultPageStore(ttl_sec=60.0, max_handles=8)
        raw = {"status": "ok", "hits": _fake_hits(5), "query": "needle", "project_id": 1}
        out = await finalize_smart_grep_response(raw, page_size=100, store=store)
        assert out["paging"]["enabled"] is False
        assert out["paging"]["total"] == 5
        assert len(out["hits"]) == 5

    asyncio.run(_run())


def test_finalize_paging_reconstructs_full_set():
    async def _run():
        store = ResultPageStore(ttl_sec=60.0, max_handles=8)
        hits = _fake_hits(250)
        raw = {
            "status": "ok",
            "hits": hits,
            "query": "needle",
            "project_id": 2,
            "search_mode": "project_registered",
            "mode": "code",
            "profile": "all",
        }
        page_size = 80
        collected = await reassemble_all_hits_from_paged_response(
            raw, page_size=page_size, store=store
        )
        assert len(collected) == 250
        assert collected == compress_smart_grep_hits(hits)

    asyncio.run(_run())


def test_extra_result_unknown_handle():
    async def _run():
        store = ResultPageStore(ttl_sec=60.0, max_handles=8)
        err = await extra_result_page("deadbeef" * 8, 0, 50, store)
        assert err["status"] == "error"
        assert err.get("error") == "unknown_or_expired_handle"

    asyncio.run(_run())


def test_store_ttl_deadline_expires_immediately():
    async def _run():
        store = ResultPageStore(ttl_sec=3600.0, max_handles=8)
        hid = await store.store(
            [{"k": 1}],
            "cq_start_grep",
            {},
            ttl_deadline=time.monotonic() - 0.05,
        )
        err = await extra_result_page(hid, 0, 50, store)
        assert err["status"] == "error"
        assert err.get("error") == "unknown_or_expired_handle"

    asyncio.run(_run())


def test_compress_truncates_long_line_and_match():
    long_line = "x" * (LINE_TEXT_MAX + 50)
    long_match = "m" * (MATCH_TEXT_MAX + 40)
    h = compress_smart_grep_hit(
        {
            "file_name": "a.py",
            "line": 1,
            "line_text": long_line,
            "match": long_match,
        }
    )
    assert len(h["line_text"]) <= LINE_TEXT_MAX
    assert h.get("line_text_truncated") is True
    assert len(h["match"]) <= MATCH_TEXT_MAX


def test_compress_context_lists():
    long = "c" * (LINE_TEXT_MAX + 10)
    h = compress_smart_grep_hit(
        {
            "file_name": "b.py",
            "line": 2,
            "line_text": "ok",
            "context_before": [long],
            "context_after": ["short"],
        }
    )
    assert isinstance(h["context_before"], list)
    assert len(h["context_before"][0]) <= LINE_TEXT_MAX


if __name__ == "__main__":
    test_finalize_no_paging_small_result()
    test_finalize_paging_reconstructs_full_set()
    test_extra_result_unknown_handle()
    test_store_ttl_deadline_expires_immediately()
    test_compress_truncates_long_line_and_match()
    test_compress_context_lists()
    print("test_cqds_result_pages: OK")
