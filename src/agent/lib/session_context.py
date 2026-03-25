# /agent/lib/session_context.py
"""
Context variable for tracking session_id across async/sync requests.
Used by BasicLogger to inject session_id into every log line.
"""
import contextvars
from typing import Optional

# Context variable that holds session_id for current request/task
_session_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    'session_id', 
    default=None
)


def set_session_id(session_id: Optional[str]) -> None:
    """Set session_id in context (called from middleware at request start)."""
    _session_id.set(session_id)


def get_session_id() -> Optional[str]:
    """Get session_id from context (returns None if not set)."""
    return _session_id.get()


def clear_session_id() -> None:
    """Clear session_id from context (called at request end)."""
    _session_id.set(None)
