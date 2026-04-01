# smart_grep_scope_cache.py — LRU кэш списков file_id для smart_grep/chunk (префикс + фильтры)
from __future__ import annotations

import hashlib
import json
import os
import threading
from collections import OrderedDict
from typing import Any, Callable

_DEFAULT_MAX_KEYS = 128
_DEFAULT_MAX_IDS_TOTAL = 2_000_000


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(os.environ.get(name, "").strip() or default)
        return max(lo, min(v, hi))
    except ValueError:
        return default


def filters_fingerprint(
    mode: str,
    profile: str,
    include_glob: list[str],
    time_strict: str | None,
) -> str:
    payload = {
        "mode": mode,
        "profile": profile,
        "include": sorted(include_glob or []),
        "time_strict": time_strict or "",
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def normalize_path_prefix(raw: str | None) -> str:
    if not raw:
        return ""
    s = str(raw).strip().replace("\\", "/")
    while s.startswith("/"):
        s = s[1:]
    if ".." in s.split("/"):
        raise ValueError("path_prefix must not contain '..'")
    return s.rstrip("/")


class SmartGrepScopeCache:
    """Ключ: (project_id, path_prefix, filters_fp) → (epoch при сборке, [file_id…])."""

    def __init__(
        self,
        *,
        max_keys: int | None = None,
        max_ids_total: int | None = None,
    ) -> None:
        self._max_keys = max_keys if max_keys is not None else _env_int(
            "CQDS_SMART_GREP_SCOPE_CACHE_KEYS", _DEFAULT_MAX_KEYS, 8, 4096
        )
        self._max_ids_total = max_ids_total if max_ids_total is not None else _env_int(
            "CQDS_SMART_GREP_SCOPE_MAX_IDS", _DEFAULT_MAX_IDS_TOTAL, 10_000, 20_000_000
        )
        self._data: OrderedDict[tuple[int, str, str], tuple[int, list[int]]] = OrderedDict()
        self._lock = threading.RLock()
        self._ids_total = 0

    def _evict_unlocked(self) -> None:
        while len(self._data) > self._max_keys:
            _, (_, lst) = self._data.popitem(last=False)
            self._ids_total -= len(lst)

    def get_or_build(
        self,
        project_id: int,
        path_prefix_norm: str,
        filters_fp: str,
        current_epoch: int,
        builder: Callable[[], list[int]],
    ) -> list[int]:
        key = (project_id, path_prefix_norm, filters_fp)
        with self._lock:
            ent = self._data.get(key)
            if ent is not None:
                built_epoch, ids = ent
                if built_epoch == current_epoch:
                    self._data.move_to_end(key)
                    return ids
                self._data.pop(key, None)
                self._ids_total -= len(ids)

            ids = builder()
            if len(ids) > self._max_ids_total:
                ids = ids[: self._max_ids_total]
            self._data[key] = (current_epoch, ids)
            self._ids_total += len(ids)
            self._data.move_to_end(key)
            self._evict_unlocked()
            while self._ids_total > self._max_ids_total and len(self._data) > 1:
                k, (_, lst) = self._data.popitem(last=False)
                self._ids_total -= len(lst)
            return ids


_global_scope_cache = SmartGrepScopeCache()


def get_scope_cache() -> SmartGrepScopeCache:
    return _global_scope_cache
