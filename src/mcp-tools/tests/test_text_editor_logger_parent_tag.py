from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from text_editor import basic_logger as bl


def test_parent_tag_cursor_from_name(monkeypatch) -> None:
    monkeypatch.setattr(bl, "_parent_process_name", lambda: "Cursor.exe")
    assert bl._parent_tag() == "cursor"


def test_parent_tag_code_from_name(monkeypatch) -> None:
    monkeypatch.setattr(bl, "_parent_process_name", lambda: "Code.exe")
    assert bl._parent_tag() == "code"


def test_parent_tag_sanitized_fallback(monkeypatch) -> None:
    monkeypatch.setattr(bl, "_parent_process_name", lambda: "My Weird Host!!.exe")
    assert bl._parent_tag() == "myweirdhost"
