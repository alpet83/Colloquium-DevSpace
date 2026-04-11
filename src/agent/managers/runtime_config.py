# Чтение операционных настроек: таблица config (effective), иначе env, иначе кодовый дефолт.
# Кэш сбрасывается через invalidate_runtime_config_cache() при изменениях через /admin/config.
from __future__ import annotations

import os
import threading
from typing import Iterable, Optional

from managers.config_store import ConfigStore, validate_config_key

_lock = threading.RLock()
_cache: dict[str, Optional[str]] = {}
_store: Optional[ConfigStore] = None


def _get_store() -> ConfigStore:
    global _store
    if _store is None:
        _store = ConfigStore()
    return _store


def invalidate_runtime_config_cache(keys: Optional[Iterable[str]] = None) -> None:
    with _lock:
        if keys is None:
            _cache.clear()
            return
        for k in keys:
            _cache.pop(str(k), None)


def _resolve_raw(key: str) -> Optional[str]:
    with _lock:
        if key in _cache:
            return _cache[key]

    text: Optional[str] = None
    try:
        validate_config_key(key)
        entry = _get_store().get_entry(key)
    except Exception:
        entry = None

    if entry is not None:
        eff = entry.get("effective")
        if eff is not None:
            text = eff.strip() if isinstance(eff, str) else str(eff)

    if text is None:
        v = os.getenv(key)
        if v is not None:
            text = v.strip()
            if text == "":
                text = None

    with _lock:
        _cache[key] = text

    return text


def get_bool(key: str, *, default: bool = False) -> bool:
    raw = _resolve_raw(key)
    if raw is None or raw == "":
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def is_runtime_config_set(key: str) -> bool:
    """True, если для ключа задано непустое effective-значение (config или env)."""
    raw = _resolve_raw(key)
    return raw is not None and raw != ""


def get_int(key: str, default: int, lo: int, hi: int) -> int:
    raw = _resolve_raw(key)
    if raw is None or raw == "":
        v = default
    else:
        try:
            v = int(raw)
        except ValueError:
            v = default
    return max(lo, min(v, hi))


def get_float(key: str, default: float, lo: float, hi: float) -> float:
    raw = _resolve_raw(key)
    if raw is None or raw == "":
        v = default
    else:
        try:
            v = float(raw)
        except ValueError:
            v = default
    return max(lo, min(v, hi))
