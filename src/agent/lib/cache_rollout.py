# /agent/lib/cache_rollout.py
"""Фаза 5: компактные guardrails и rollout для контекстного кэша.

Переменные окружения:
- CQDS_CACHE_ROLLOUT — мастер-выключатель (по умолчанию включён: 1).
  При 0: решение кэша принудительно FULL (reason=rollout_disabled), без инкрементальных
  патчей по предыдущему снимку, без prefix reuse и без provider prefix hints.
- CQDS_CONTEXT_CACHE_METRICS_WRITE — запись в context_cache_metrics (1 по умолчанию, 0 — off).
- CQDS_CONTEXT_CACHE_METRICS_SAMPLE_PCT — доля записей 0–100 (по умолчанию 100).
- CQDS_CONTEXT_SENT_TOKENS_WARN — порог предупреждения в лог по sent_tokens (0 = выключено).

Сессия: g.set_session_option(sid, 'cache_rollout', False) отключает кэш-поведение для сессии
при включённом CQDS_CACHE_ROLLOUT в окружении.
"""
from __future__ import annotations

import os

import globals as g


def cache_rollout_enabled(session_id: str | None = None, user_id: int | None = None) -> bool:
    """Включён ли «умный» путь кэша (DELTA_SAFE, патчи, reuse, provider hints).

    user_id зарезервирован под будущие per-user флаги; сейчас не используется.
    """
    _ = user_id
    v = (os.environ.get("CQDS_CACHE_ROLLOUT") or "1").strip().lower()
    env_on = v not in ("0", "false", "off", "no")
    if not env_on:
        return False
    opt = g.get_session_option(session_id, "cache_rollout", None)
    if opt is None:
        return True
    if isinstance(opt, bool):
        return opt
    s = str(opt).strip().lower()
    if s in ("0", "false", "off", "no"):
        return False
    if s in ("1", "true", "on", "yes"):
        return True
    return bool(opt)


def context_cache_metrics_write_enabled() -> bool:
    v = (os.environ.get("CQDS_CONTEXT_CACHE_METRICS_WRITE") or "1").strip().lower()
    return v not in ("0", "false", "off", "no")


def context_cache_metrics_sample_pct() -> int:
    try:
        pct = int(os.environ.get("CQDS_CONTEXT_CACHE_METRICS_SAMPLE_PCT", "100"))
    except (TypeError, ValueError):
        pct = 100
    return max(0, min(100, pct))


def sent_tokens_warn_threshold() -> int:
    try:
        return max(0, int(os.environ.get("CQDS_CONTEXT_SENT_TOKENS_WARN", "0")))
    except (TypeError, ValueError):
        return 0
