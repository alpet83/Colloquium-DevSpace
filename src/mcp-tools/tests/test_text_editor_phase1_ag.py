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


def test_group_a_lifecycle_open_reopen_persistence(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("one\ntwo\n", encoding="utf-8")
    svc = _mk_service(tmp_path)

    opened1 = svc.open_session({"path": str(target)})
    sid = opened1["session_id"]
    assert opened1["current_revision"] == 1

    mutated = svc.execute(
        {
            "session_id": sid,
            "expected_revision": 1,
            "op": "replace_range",
            "op_args": {"line_start": 2, "line_end": 2, "replacement_lines": ["TWO"]},
            "response_mode": "minimal",
        }
    )
    assert mutated["current_revision"] == 2

    opened2 = svc.open_session({"path": str(target)})
    assert opened2["session_id"] == sid
    assert opened2["current_revision"] == 2


def test_group_b_canonical_identity_same_file(tmp_path: Path) -> None:
    target = tmp_path / "b.txt"
    target.write_text("x\n", encoding="utf-8")
    svc = _mk_service(tmp_path)

    abs_path = str(target.resolve())
    dotted_path = str(tmp_path / "." / "b.txt")
    s1 = svc.open_session({"path": abs_path})["session_id"]
    s2 = svc.open_session({"path": dotted_path})["session_id"]
    assert s1 == s2


def test_group_c_concurrency_revision_mismatch(tmp_path: Path) -> None:
    target = tmp_path / "c.txt"
    target.write_text("a\nb\n", encoding="utf-8")
    svc = _mk_service(tmp_path)
    sid = svc.open_session({"path": str(target)})["session_id"]

    ok = svc.execute(
        {
            "session_id": sid,
            "expected_revision": 1,
            "op": "replace_range",
            "op_args": {"line_start": 1, "line_end": 1, "replacement_lines": ["A"]},
            "response_mode": "minimal",
        }
    )
    assert ok["current_revision"] == 2

    with pytest.raises(EditorError) as exc_info:
        svc.execute(
            {
                "session_id": sid,
                "expected_revision": 1,
                "op": "replace_range",
                "op_args": {"line_start": 2, "line_end": 2, "replacement_lines": ["B"]},
                "response_mode": "minimal",
            }
        )
    exc = exc_info.value
    assert exc.err_class == "concurrency"
    assert exc.code == "revision_mismatch"
    assert exc.details["expected_revision"] == 1
    assert exc.details["actual_revision"] == 2


def test_group_d_security_outside_allowlist_and_binary(tmp_path: Path) -> None:
    svc = _mk_service(tmp_path)
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")

    with pytest.raises(EditorError) as exc_info:
        svc.open_session({"path": str(outside)})
    exc = exc_info.value
    assert exc.err_class == "security"
    assert exc.code == "path_not_allowed"

    bad = tmp_path / "bad.bin"
    bad.write_bytes(b"\xff\xfe\x00\xff")
    with pytest.raises(EditorError) as exc_info2:
        svc.open_session({"path": str(bad)})
    exc2 = exc_info2.value
    assert exc2.err_class == "security"
    assert exc2.code == "file_not_text"


def test_group_e_external_sync_auto_sync_false(tmp_path: Path) -> None:
    target = tmp_path / "e.txt"
    target.write_text("a\nb\n", encoding="utf-8")
    svc = _mk_service(tmp_path)
    sid = svc.open_session({"path": str(target)})["session_id"]

    target.write_text("a\nb_external\n", encoding="utf-8")
    with pytest.raises(EditorError) as exc_info:
        svc.execute(
            {
                "session_id": sid,
                "expected_revision": 1,
                "op": "replace_range",
                "op_args": {"line_start": 1, "line_end": 1, "replacement_lines": ["A"]},
                "response_mode": "minimal",
                "auto_sync": False,
            }
        )
    exc = exc_info.value
    assert exc.err_class == "source_sync"
    assert exc.code == "source_changed_externally"


def test_group_f_revision_invariants_and_dry_run(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("l1\nl2\n", encoding="utf-8")
    svc = _mk_service(tmp_path)
    sid = svc.open_session({"path": str(target)})["session_id"]

    preview = svc.execute(
        {
            "session_id": sid,
            "expected_revision": 1,
            "op": "replace_regex",
            "op_args": {"pattern": "l2", "replacement": "L2"},
            "response_mode": "minimal",
            "dry_run": True,
        }
    )
    assert preview["dry_run"] is True
    info0 = svc.storage.get_session_info(sid)
    assert int(info0["current_revision_number"]) == 1

    done = svc.execute(
        {
            "session_id": sid,
            "expected_revision": 1,
            "op": "replace_regex",
            "op_args": {"pattern": "l2", "replacement": "L2"},
            "response_mode": "minimal",
        }
    )
    assert done["current_revision"] == 2

    info = svc.storage.get_session_info(sid)
    with sqlite3.connect(str(info["session_db_path"])) as conn:
        bad_rows = conn.execute(
            """
            SELECT COUNT(*) FROM revision_history
            WHERE line_num < 1 OR (deleted_idx < 0 AND added_idx < 0)
            """
        ).fetchone()
        broken_links = conn.execute(
            """
            SELECT COUNT(*) FROM current_revision c
            LEFT JOIN text_lines t ON t.idx = c.line_idx
            WHERE t.idx IS NULL
            """
        ).fetchone()
    assert bad_rows is not None and int(bad_rows[0]) == 0
    assert broken_links is not None and int(broken_links[0]) == 0


def test_group_g_undo_redo_and_redo_truncation(tmp_path: Path) -> None:
    target = tmp_path / "g.txt"
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

    undone = svc.execute({"session_id": sid, "expected_revision": 2, "op": "undo", "response_mode": "minimal"})
    assert undone["current_revision"] == 3

    redone = svc.execute({"session_id": sid, "expected_revision": 3, "op": "redo", "response_mode": "minimal"})
    assert redone["current_revision"] == 4

    undone2 = svc.execute({"session_id": sid, "expected_revision": 4, "op": "undo", "response_mode": "minimal"})
    assert undone2["current_revision"] == 5
    _new_mut = svc.execute(
        {
            "session_id": sid,
            "expected_revision": 5,
            "op": "replace_range",
            "op_args": {"line_start": 1, "line_end": 1, "replacement_lines": ["A2"]},
            "response_mode": "minimal",
        }
    )
    after = svc.execute({"session_id": sid, "expected_revision": 6, "op": "redo", "response_mode": "minimal"})
    view = svc.execute({"session_id": sid, "op": "get_view", "response_mode": "minimal"})
    body = "\n".join(view["view"])
    assert "B2" not in body
    assert after["current_revision"] >= 7

