from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class EditorError(Exception):
    err_class: str
    code: str
    message: str
    retryable: bool = False
    hint: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "class": self.err_class,
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }
        if self.hint:
            payload["hint"] = self.hint
        return payload


def bad_request(code: str, message: str, *, hint: str | None = None, **details: Any) -> EditorError:
    return EditorError("validation", code, message, retryable=False, hint=hint, details=details)

