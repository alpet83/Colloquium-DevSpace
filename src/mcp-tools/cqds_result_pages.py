# cqds_result_pages.py — in-memory paged tool results (TTL + LRU) for MCP
from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any

DEFAULT_PAGE_SIZE = 100
DEFAULT_SCAN_HIT_CAP = 10000
DEFAULT_TTL_SEC = 900.0
DEFAULT_TTL_AFTER_COMPLETE_SEC = 1800.0
DEFAULT_MAX_HANDLES = 48
LINE_TEXT_MAX = 200
MATCH_TEXT_MAX = 120


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(os.environ.get(name, "").strip() or default)
        return max(lo, min(v, hi))
    except ValueError:
        return default


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    try:
        v = float(os.environ.get(name, "").strip() or default)
        return max(lo, min(v, hi))
    except ValueError:
        return default


TTL_AFTER_COMPLETE_SEC = _env_float(
    "CQDS_MCP_PAGE_TTL_AFTER_COMPLETE_SEC",
    DEFAULT_TTL_AFTER_COMPLETE_SEC,
    60.0,
    86400.0,
)


@dataclass
class _Entry:
    items: list[Any]
    created: float
    source_tool: str
    meta: dict[str, Any]
    ttl_deadline: float | None = None


class ResultPageStore:
    """Stores full hit lists keyed by handle; pages served via cq_fetch_result."""

    def __init__(
        self,
        *,
        ttl_sec: float | None = None,
        max_handles: int | None = None,
    ):
        self._ttl = float(ttl_sec if ttl_sec is not None else os.environ.get("CQDS_MCP_PAGE_TTL_SEC", "") or DEFAULT_TTL_SEC)
        self._max_handles = max_handles if max_handles is not None else _env_int("CQDS_MCP_PAGE_MAX_HANDLES", DEFAULT_MAX_HANDLES, 4, 256)
        self._data: dict[str, _Entry] = {}
        self._lock = asyncio.Lock()

    def _purge_unlocked(self) -> None:
        now = time.monotonic()
        for h, e in list(self._data.items()):
            if e.ttl_deadline is not None:
                if now >= e.ttl_deadline:
                    del self._data[h]
            elif now - e.created > self._ttl:
                del self._data[h]
        while len(self._data) > self._max_handles:
            oldest = min(self._data.items(), key=lambda x: x[1].created)[0]
            del self._data[oldest]

    async def store(
        self,
        items: list[Any],
        source_tool: str,
        meta: dict[str, Any],
        *,
        ttl_deadline: float | None = None,
    ) -> str:
        async with self._lock:
            self._purge_unlocked()
            while len(self._data) >= self._max_handles:
                oldest = min(self._data.items(), key=lambda x: x[1].created)[0]
                del self._data[oldest]
            hid = uuid.uuid4().hex
            self._data[hid] = _Entry(
                items=list(items),
                created=time.monotonic(),
                source_tool=source_tool,
                meta=dict(meta),
                ttl_deadline=ttl_deadline,
            )
            return hid

    async def get_page(
        self, handle: str, page_index: int, page_size: int
    ) -> tuple[list[Any] | None, dict[str, Any]]:
        """Returns (None, {error}) if unknown/expired; else (slice, info)."""
        async with self._lock:
            self._purge_unlocked()
            ent = self._data.get(handle)
            if ent is None:
                return None, {"error": "unknown_or_expired_handle", "handle": handle}
            if page_index < 0 or page_size < 1:
                return None, {"error": "invalid_page", "page_index": page_index, "page_size": page_size}
            start = page_index * page_size
            total = len(ent.items)
            if start >= total:
                return [], {
                    "handle": handle,
                    "page_index": page_index,
                    "page_size": page_size,
                    "total": total,
                    "returned": 0,
                    "has_more": False,
                    "source_tool": ent.source_tool,
                    **ent.meta,
                }
            chunk = ent.items[start : start + page_size]
            return chunk, {
                "handle": handle,
                "page_index": page_index,
                "page_size": page_size,
                "total": total,
                "returned": len(chunk),
                "has_more": start + len(chunk) < total,
                "next_page_index": page_index + 1 if start + len(chunk) < total else None,
                "source_tool": ent.source_tool,
                **ent.meta,
            }


_global_store = ResultPageStore()


def get_page_store() -> ResultPageStore:
    return _global_store


def compress_smart_grep_hit(hit: dict[str, Any]) -> dict[str, Any]:
    out = dict(hit)
    lt = out.get("line_text")
    if isinstance(lt, str) and len(lt) > LINE_TEXT_MAX:
        out["line_text"] = lt[: LINE_TEXT_MAX - 1] + "…"
        out["line_text_truncated"] = True
    mt = out.get("match")
    if isinstance(mt, str) and len(mt) > MATCH_TEXT_MAX:
        out["match"] = mt[: MATCH_TEXT_MAX - 1] + "…"
    for key in ("context_before", "context_after"):
        if key in out and isinstance(out[key], list):
            out[key] = [
                (s[: LINE_TEXT_MAX - 1] + "…") if isinstance(s, str) and len(s) > LINE_TEXT_MAX else s
                for s in out[key]
            ]
    return out


def compress_smart_grep_hits(hits: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for h in hits:
        if isinstance(h, dict):
            out.append(compress_smart_grep_hit(h))
    return out


async def finalize_smart_grep_response(
    result: dict[str, Any],
    *,
    page_size: int,
    store: ResultPageStore | None = None,
    source_tool: str = "cq_start_grep",
    scan_complete: bool | None = None,
) -> dict[str, Any]:
    """Truncate long fields; if hits > page_size, keep first page inline and cache the rest.

    Если scan_complete=True (финальный чанк или одношаговый host_fs), срок жизни handle в store
    отсчитывается от момента записи как TTL_AFTER_COMPLETE_SEC (по умолчанию 30 мин), а не DEFAULT_TTL_SEC.
    """
    st = store or get_page_store()
    hits_raw = result.get("hits")
    if not isinstance(hits_raw, list):
        return result

    hits = compress_smart_grep_hits(hits_raw)
    total = len(hits)
    page_size = max(1, min(page_size, 500))

    meta = {
        "query": result.get("query"),
        "project_id": result.get("project_id"),
        "search_mode": result.get("search_mode"),
        "mode": result.get("mode"),
        "profile": result.get("profile"),
    }

    if total <= page_size:
        out = {**result, "hits": hits, "total": total}
        out["paging"] = {
            "enabled": False,
            "page_size": page_size,
            "total": total,
            "returned": total,
            "has_more": False,
        }
        return out

    ttl_deadline: float | None = None
    ttl_hint = int(DEFAULT_TTL_SEC)
    if scan_complete is True:
        ttl_deadline = time.monotonic() + TTL_AFTER_COMPLETE_SEC
        ttl_hint = int(TTL_AFTER_COMPLETE_SEC)
    handle = await st.store(hits, source_tool, meta, ttl_deadline=ttl_deadline)
    out = {**result, "hits": hits[:page_size], "total": total}
    out["paging"] = {
        "enabled": True,
        "handle": handle,
        "page_index": 0,
        "page_size": page_size,
        "total": total,
        "returned": page_size,
        "has_more": True,
        "next_page_index": 1,
        "ttl_hint_sec": ttl_hint,
        "hint": "cq_fetch_result: 0-based page_index; первая страница уже в hits, следующая — page_index=1.",
    }
    return out


async def reassemble_all_hits_from_paged_response(
    result: dict[str, Any],
    *,
    page_size: int,
    store: ResultPageStore | None = None,
) -> list[dict[str, Any]]:
    """Как MCP-клиент: finalize → первая страница в result['hits'] + cq_fetch_result для остальных."""
    st = store or get_page_store()
    finalized = await finalize_smart_grep_response(
        dict(result), page_size=page_size, store=st, source_tool="cq_start_grep"
    )
    hits_out = list(finalized.get("hits") or [])
    pg = finalized.get("paging") or {}
    if not pg.get("enabled"):
        return hits_out
    handle = str(pg.get("handle") or "")
    ps = int(pg.get("page_size") or page_size)
    idx = 1
    while True:
        chunk = await extra_result_page(handle, idx, ps, st)
        if chunk.get("status") != "ok":
            break
        part = chunk.get("hits") or []
        if not part:
            break
        hits_out.extend(part)
        if not (chunk.get("paging") or {}).get("has_more"):
            break
        idx += 1
    return hits_out


async def extra_result_page(
    handle: str,
    page_index: int,
    page_size: int,
    store: ResultPageStore | None = None,
) -> dict[str, Any]:
    st = store or get_page_store()
    page_size = max(1, min(int(page_size), 500))
    chunk, info = await st.get_page(handle.strip(), int(page_index), page_size)
    if chunk is None:
        return {"status": "error", **info}
    return {
        "status": "ok",
        "hits": chunk,
        "paging": {k: v for k, v in info.items() if k not in ("query", "project_id", "search_mode", "mode", "profile")},
        "echo": {k: info.get(k) for k in ("query", "project_id", "search_mode", "mode", "profile") if info.get(k) is not None},
    }
