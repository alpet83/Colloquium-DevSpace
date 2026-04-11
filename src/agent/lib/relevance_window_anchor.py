"""Якорь окна релевантности постов: обновляется только при промахе кэша (FULL).

В assemble_posts линейный спад rel_offset (base_rel - count) применяется только к постам
с post_id строго больше anchor; для «замороженного» префикса offset стабилен между
ходами DELTA_SAFE, чтобы не сдвигать окно на каждое новое сообщение.
"""
from __future__ import annotations

import threading

import globals as g

_lock = threading.RLock()
# (chat_id, session_id) -> last_post_id на момент последнего FULL для этого чата/сессии
_anchors: dict[tuple[int, str], int] = {}

log = g.get_logger("relevance_anchor")


def get_anchor(chat_id: int, session_id: str | None) -> int:
    """Граница префикса: посты с id <= anchor не участвуют в пошаговом спаде rel_offset."""
    key = (int(chat_id), str(session_id or ""))
    with _lock:
        return int(_anchors.get(key, 0) or 0)


def set_anchor_on_full(chat_id: int, session_id: str | None, last_post_id: int) -> None:
    """Вызывать из LLMInteractor при решении cache mode FULL после сборки контекста."""
    pid = int(last_post_id or 0)
    if pid <= 0:
        return
    key = (int(chat_id), str(session_id or ""))
    with _lock:
        _anchors[key] = pid
    slog = key[1][:24] + "…" if len(key[1]) > 24 else key[1]
    log.debug(
        "relevance_window_anchor: chat_id=%d session=%s anchor_post_id=%d",
        key[0],
        slog or "-",
        pid,
    )


def clear_anchor(chat_id: int, session_id: str | None = None) -> None:
    """Сброс (тесты / отладка)."""
    key = (int(chat_id), str(session_id or ""))
    with _lock:
        _anchors.pop(key, None)
