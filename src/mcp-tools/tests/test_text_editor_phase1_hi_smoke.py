from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from text_editor.config import default_data_dir, load_policy
from text_editor.errors import EditorError
from text_editor.service import EditorService
from text_editor.storage import Storage


def _mk_service(tmp_path: Path) -> EditorService:
    os.environ["TEXT_EDITOR_DATA_DIR"] = str(tmp_path / "data")
    os.chdir(tmp_path)
    data_dir = default_data_dir()
    policy = load_policy(data_dir)
    storage = Storage(data_dir, policy)
    return EditorService(storage, policy)


def test_group_h_smoke_undo_target_missing_snapshot(tmp_path: Path) -> None:
    target = tmp_path / "h.txt"
    target.write_text("a\nb\n", encoding="utf-8")
    svc = _mk_service(tmp_path)
    sid = svc.open_session({"path": str(target)})["session_id"]

    r2 = svc.execute(
        {
            "session_id": sid,
            "expected_revision": 1,
            "op": "replace_range",
            "op_args": {"line_start": 2, "line_end": 2, "replacement_lines": ["B2"]},
            "response_mode": "minimal",
        }
    )
    assert r2["current_revision"] == 2

    # smoke for "compaction readiness": snapshot for target revision is unavailable
    info = svc.storage.get_session_info(sid)
    with sqlite3.connect(str(info["session_db_path"])) as conn:
        conn.execute("DELETE FROM revision_snapshots WHERE revision=1")
        conn.commit()

    with pytest.raises(EditorError) as exc_info:
        svc.execute(
            {
                "session_id": sid,
                "expected_revision": 2,
                "op": "undo",
                "op_args": {"target_revision": 1},
                "response_mode": "minimal",
            }
        )
    exc = exc_info.value
    assert exc.err_class == "validation"
    assert exc.code == "revision_not_available_after_compaction"


def test_group_i_smoke_error_contract_and_session_defaults(tmp_path: Path) -> None:
    target = tmp_path / "i.txt"
    target.write_text("x\n", encoding="utf-8")
    svc = _mk_service(tmp_path)
    opened = svc.open_session({"path": str(target)})
    sid = opened["session_id"]

    # session_open UX part: defaults should be available without separate help call
    assert "session_defaults" in opened
    assert "allowed_ops" in opened["session_defaults"]

    # error-contract part: key fields in envelope for known error
    svc.execute(
        {
            "session_id": sid,
            "expected_revision": 1,
            "op": "replace_range",
            "op_args": {"line_start": 1, "line_end": 1, "replacement_lines": ["X"]},
            "response_mode": "minimal",
        }
    )
    with pytest.raises(EditorError) as exc_info:
        svc.execute(
            {
                "session_id": sid,
                "expected_revision": 1,
                "op": "replace_range",
                "op_args": {"line_start": 1, "line_end": 1, "replacement_lines": ["Y"]},
                "response_mode": "minimal",
            }
        )
    payload = exc_info.value.to_payload()
    assert payload["class"] == "concurrency"
    assert payload["code"] == "revision_mismatch"
    assert "retryable" in payload
    assert "details" in payload and "actual_revision" in payload["details"]

