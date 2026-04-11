# /agent/lib/context_reference_store.py
"""Хранилище снимков контекстного fingerprint (референс для DELTA / патчей).

Отключение расширенного референса (post_digest, file_rev_ts, context_patch): переменная окружения
`CQDS_CONTEXT_REFERENCE_STORE=0` — остаётся «узкий» снимок как в schema_ver≈4 без дайджестов постов.
"""
from __future__ import annotations

import hashlib
import os
from typing import Any
import time

import globals as g
from lib.content_block import ContextPatchBlock
from lib.sandwich_pack import estimate_tokens

log = g.get_logger("context_reference_store")

CONTEXT_REFERENCE_ENV = "CQDS_CONTEXT_REFERENCE_STORE"

_SLIM_FP_KEYS = frozenset(
    {
        "pre_prompt_hash",
        "index_hash",
        "head_posts_sig",
        "non_post_sig",
        "last_post_id",
        "last_committed_mode",
        "blocks_count",
        "rql",
        "session_id",
        "updated_at",
    }
)


def context_reference_store_enabled(explicit: bool | None = None) -> bool:
    """Явный аргумент перекрывает env. По умолчанию референс включён (1)."""
    if explicit is not None:
        return bool(explicit)
    v = (os.environ.get(CONTEXT_REFERENCE_ENV) or "1").strip().lower()
    return v not in ("0", "false", "off", "no")


def digest_token(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8", errors="replace")).hexdigest()[:24]


def post_digest_for_cache(text: str) -> str:
    t = (text or "").lstrip()
    if t.startswith("⏳"):
        return "__PROGRESS__"
    if t.startswith("⚠️"):
        return "__WARN__"
    return digest_token(text)


def post_digest_map(blocks: list) -> dict[int, str]:
    out: dict[int, str] = {}
    for b in blocks:
        if getattr(b, "content_type", None) != ":post":
            continue
        pid = int(b.post_id or 0)
        if pid <= 0:
            continue
        out[pid] = post_digest_for_cache(b.content_text or "")
    return out


def file_rev_ts_map(blocks: list) -> dict[int, float]:
    out: dict[int, float] = {}
    for b in blocks:
        if getattr(b, "content_type", None) == ":post":
            continue
        fid = getattr(b, "file_id", None)
        if fid is None:
            continue
        try:
            fid = int(fid)
        except (TypeError, ValueError):
            continue
        rts = getattr(b, "revision_ts", None)
        if rts is None:
            continue
        try:
            fts = float(rts)
        except (TypeError, ValueError):
            continue
        out[fid] = max(out.get(fid, 0.0), fts)
    return out


def map_get(m: dict, key: int):
    if key in m:
        return m[key]
    sk = str(key)
    if sk in m:
        return m[sk]
    return None


def append_incremental_patches(
    ci: Any,
    filtered_blocks: list,
    prev: dict | None,
    total_tokens: int,
    tokens_limit: int,
    *,
    reference_enabled: bool,
) -> int:
    """Добавляет :context_patch в хвост; при reference_enabled=False — no-op."""
    if not reference_enabled or not prev:
        return total_tokens
    prev_pd = prev.get("post_digest") or {}
    prev_fv = prev.get("file_rev_ts") or {}
    if not isinstance(prev_pd, dict) or not isinstance(prev_fv, dict):
        return total_tokens
    cur_pd = post_digest_map(ci.blocks)
    patches: list[ContextPatchBlock] = []

    for b in ci.blocks:
        if getattr(b, "content_type", None) != ":post":
            continue
        pid = int(b.post_id or 0)
        if pid <= 0:
            continue
        cur_d = cur_pd.get(pid)
        old_d = map_get(prev_pd, pid)
        if old_d is None or cur_d is None or cur_d == old_d:
            continue
        if old_d == "__PROGRESS__" and cur_d not in ("__PROGRESS__", "__WARN__"):
            continue
        patches.append(
            ContextPatchBlock(
                b.content_text or "",
                patch_kind="post",
                post_id=pid,
                user_id=b.user_id,
                timestamp=b.timestamp,
                revision_ts=b.revision_ts,
            )
        )

    # Tombstone: пост был в прошлом снимке, но исчез из текущего потока :post.
    now_ts = int(time.time())
    for raw_pid in prev_pd.keys():
        try:
            pid = int(raw_pid)
        except (TypeError, ValueError):
            continue
        if pid <= 0 or pid in cur_pd:
            continue
        patches.append(
            ContextPatchBlock(
                "#deleted_by:user",
                patch_kind="post",
                post_id=pid,
                user_id=0,
                timestamp=now_ts,
                revision_ts=float(now_ts),
            )
        )

    seen_file_patch: set[int] = set()
    for b in ci.blocks:
        ct = getattr(b, "content_type", None)
        if ct in (":post", ":context_patch"):
            continue
        fid = getattr(b, "file_id", None)
        if fid is None:
            continue
        try:
            fid = int(fid)
        except (TypeError, ValueError):
            continue
        rts = getattr(b, "revision_ts", None)
        if rts is None:
            continue
        try:
            fts = float(rts)
        except (TypeError, ValueError):
            continue
        old_ts = map_get(prev_fv, fid)
        if old_ts is None:
            continue
        try:
            if fts <= float(old_ts) + 1e-6:
                continue
        except (TypeError, ValueError):
            continue
        if fid in seen_file_patch:
            continue
        seen_file_patch.add(fid)
        patches.append(
            ContextPatchBlock(
                b.content_text or "",
                patch_kind="file",
                file_id=fid,
                timestamp=b.timestamp,
                revision_ts=fts,
            )
        )

    if not patches:
        return total_tokens

    log.info(
        "ContextIncrementalPatches count=%d (revisions vs previous fingerprint)",
        len(patches),
    )
    t = total_tokens
    for pb in patches:
        tk = estimate_tokens(pb.to_sandwich_block())
        if t + tk >= tokens_limit:
            log.warn("Пропуск context_patch: лимит токенов context (%d)", tokens_limit)
            break
        filtered_blocks.append(pb)
        t += tk
    return t


class ContextReferenceStore:
    """Хранилище in-memory состояния контекста для одного процесса ядра.

    Держит два независимых слоя:
    - Layer A (`_snapshots`): fingerprint/референс для решения FULL vs DELTA_SAFE.
    - Layer B (`_materialized_prefixes`): материализованный префикс, который можно
      переиспользовать в DELTA_SAFE-цепочке сборки.

    Ключ доступа: строка `actor_id:chat_id:session_id`.
    """

    def __init__(self, enabled: bool | None = None):
        self._enabled = context_reference_store_enabled(enabled)
        self._snapshots: dict[str, dict[str, Any]] = {}
        # Материализованный префикс (слой B): короткоживущий и подлежит eviction при FULL.
        self._materialized_prefixes: dict[str, dict[str, Any]] = {}
        if not self._enabled:
            log.debug(
                "%s=0 — узкий fingerprint (без post_digest/file_rev_ts и без context_patch)",
                CONTEXT_REFERENCE_ENV,
            )

    @property
    def enabled(self) -> bool:
        return self._enabled

    def get(self, cache_key: str) -> dict[str, Any] | None:
        """Вернуть snapshot Layer A.

        Args:
            cache_key: Композитный ключ `actor_id:chat_id:session_id`.

        Returns:
            Снимок fingerprint или ``None``.
        """
        return self._snapshots.get(cache_key)

    def put(self, cache_key: str, fingerprint: dict[str, Any]) -> None:
        """Сохранить snapshot Layer A.

        Args:
            cache_key: Композитный ключ `actor_id:chat_id:session_id`.
            fingerprint: Полный снимок состояния сборки контекста.

        Notes:
            При отключённом расширенном референсе сохраняется только узкий набор
            полей (`_SLIM_FP_KEYS`) без `post_digest`/`file_rev_ts`.
        """
        if self._enabled:
            self._snapshots[cache_key] = dict(fingerprint)
        else:
            self._snapshots[cache_key] = {
                k: fingerprint[k] for k in _SLIM_FP_KEYS if k in fingerprint
            }

    def clear(self) -> None:
        """Очистить оба слоя in-memory кэша для всех ключей."""
        self._snapshots.clear()
        self._materialized_prefixes.clear()

    # --- Layer B: materialized prefix cache ---
    def get_mp(self, cache_key: str) -> dict[str, Any] | None:
        """Вернуть payload материализованного префикса (Layer B).

        Args:
            cache_key: Композитный ключ `actor_id:chat_id:session_id`.

        Returns:
            Словарь payload префикса или ``None``.
        """
        return self._materialized_prefixes.get(cache_key)

    def put_mp(self, cache_key: str, payload: dict[str, Any]) -> None:
        """Сохранить payload материализованного префикса (Layer B).

        Args:
            cache_key: Композитный ключ `actor_id:chat_id:session_id`.
            payload: Данные префикса для потенциального переиспользования.
        """
        # Храним копию, чтобы снаружи не было неявной мутации.
        self._materialized_prefixes[cache_key] = dict(payload or {})

    def evict_mp(self, cache_key: str) -> None:
        """Идемпотентно удалить payload Layer B для одного ключа.

        Args:
            cache_key: Композитный ключ `actor_id:chat_id:session_id`.
        """
        self._materialized_prefixes.pop(cache_key, None)

    def evict_scope(self, *, chat_id: int, actor_id: int | None = None) -> int:
        """Удалить Layer A/B по области actor/chat (все session_id).

        Args:
            chat_id: ID чата.
            actor_id: ID актёра; если None — все актёры чата.

        Returns:
            Количество удалённых ключей.
        """
        cid = int(chat_id)
        aid = None if actor_id is None else int(actor_id)
        keys = set(self._snapshots.keys()) | set(self._materialized_prefixes.keys())
        victims: list[str] = []
        for k in keys:
            parts = str(k).split(":", 2)
            if len(parts) < 3:
                continue
            try:
                ka = int(parts[0])
                kc = int(parts[1])
            except (TypeError, ValueError):
                continue
            if kc != cid:
                continue
            if aid is not None and ka != aid:
                continue
            victims.append(k)
        for k in victims:
            self._snapshots.pop(k, None)
            self._materialized_prefixes.pop(k, None)
        return len(victims)
