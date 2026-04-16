from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from text_editor.config import default_data_dir, load_policy
from text_editor.constants import SAVED_TO_DISK
from text_editor.service import EditorService
from text_editor.storage import Storage


def _mk_service(tmp_path: Path) -> EditorService:
    os.environ["TEXT_EDITOR_DATA_DIR"] = str(tmp_path / "data")
    os.chdir(tmp_path)
    data_dir = default_data_dir()
    policy = load_policy(data_dir)
    storage = Storage(data_dir, policy)
    return EditorService(storage, policy)


def test_open_replace_search_save_undo(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    svc = _mk_service(tmp_path)
    opened = svc.open_session({"path": str(target), "capabilities_hint": "search,replace"})
    sid = opened["session_id"]
    assert opened["current_revision"] == 1
    assert "search" in opened["capabilities_guide"]
    assert "telemetry" in opened

    replaced = svc.execute(
        {
            "session_id": sid,
            "expected_revision": 1,
            "op": "replace_range",
            "op_args": {"line_start": 2, "line_end": 2, "replacement_lines": ["beta2"]},
            "response_mode": "numbered_lines",
        }
    )
    assert replaced["current_revision"] == 2
    assert replaced["previous_revision"] == 1

    hits = svc.execute(
        {
            "session_id": sid,
            "op": "search_indexed",
            "op_args": {"query": "beta2"},
            "response_mode": "minimal",
        }
    )
    assert hits["hit_count"] == 1
    assert hits["hits"][0]["first_revision"] == 2

    saved = svc.execute(
        {
            "session_id": sid,
            "expected_revision": 2,
            "op": "save_revision",
            "response_mode": "minimal",
        }
    )
    assert saved["saved_to_disk"] is True
    assert target.read_text(encoding="utf-8").splitlines()[1] == "beta2"

    info = svc.storage.get_session_info(sid)
    with sqlite3.connect(info["session_db_path"]) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM revision_history WHERE revision=2 AND (flags & ?) != 0",
            (SAVED_TO_DISK,),
        ).fetchone()
    assert row is not None
    assert int(row[0]) >= 1

    undone = svc.execute(
        {
            "session_id": sid,
            "expected_revision": 2,
            "op": "undo",
            "response_mode": "minimal",
        }
    )
    assert undone["current_revision"] == 3
    assert undone["redo_revision"] == 2

    # deep undo to specific revision
    deep = svc.execute(
        {
            "session_id": sid,
            "expected_revision": 3,
            "op": "undo",
            "op_args": {"target_revision": 1},
            "response_mode": "minimal",
        }
    )
    assert deep["target_revision"] == 1
    assert deep["current_revision"] == 4


def test_replace_regex_apply_patch_and_wrapped_truncate(tmp_path: Path) -> None:
    target = tmp_path / "sample2.txt"
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")
    svc = _mk_service(tmp_path)
    opened = svc.open_session({"path": str(target)})
    sid = opened["session_id"]

    rr = svc.execute(
        {
            "session_id": sid,
            "expected_revision": 1,
            "op": "replace_regex",
            "op_args": {"pattern": "two", "replacement": "TWO"},
            "response_mode": "minimal",
        }
    )
    assert rr["current_revision"] == 2
    assert rr["replacements"] == 1

    patch_text = "\n".join(
        [
            "@@ -1,3 +1,3 @@",
            " one",
            "-TWO",
            "+SECOND",
            " three",
        ]
    )
    ap = svc.execute(
        {
            "session_id": sid,
            "expected_revision": 2,
            "op": "apply_patch",
            "op_args": {"patch_text": patch_text},
            "response_mode": "minimal",
        }
    )
    assert ap["current_revision"] == 3

    # wrapped output shape
    view = svc.execute(
        {
            "session_id": sid,
            "op": "get_view",
            "response_mode": "numbered_lines",
            "op_args": {"wrap_width": 2, "max_view_lines": 3},
        }
    )
    assert isinstance(view["view"][0]["text"], list)

    # truncation path (> MAX_NUMBERED_LINES)
    big = tmp_path / "big.txt"
    big.write_text("\n".join([f"line_{i}" for i in range(130)]) + "\n", encoding="utf-8")
    opened_big = svc.open_session({"path": str(big)})
    sid_big = opened_big["session_id"]
    tv = svc.execute(
        {
            "session_id": sid_big,
            "op": "get_view",
            "response_mode": "numbered_lines",
            "op_args": {"max_view_lines": 130},
        }
    )
    assert tv["truncated"] is True
    assert tv["returned_lines"] <= 120


def test_external_sync_before_mutation(tmp_path: Path) -> None:
    target = tmp_path / "drift.txt"
    target.write_text("a\nb\n", encoding="utf-8")
    svc = _mk_service(tmp_path)
    opened = svc.open_session({"path": str(target)})
    sid = opened["session_id"]

    # external edit outside session
    target.write_text("a\nb_external\n", encoding="utf-8")

    # mutation should auto-sync and then apply replace
    res = svc.execute(
        {
            "session_id": sid,
            "expected_revision": 1,
            "op": "replace_range",
            "op_args": {"line_start": 1, "line_end": 1, "replacement_lines": ["A"]},
            "response_mode": "minimal",
            "auto_sync": True,
        }
    )
    # one revision for external_sync + one for replace
    assert res["current_revision"] >= 3


def test_profile_syntax_check_python(tmp_path: Path) -> None:
    target = tmp_path / "bad.py"
    target.write_text("def f(:\n    pass\n", encoding="utf-8")
    svc = _mk_service(tmp_path)
    opened = svc.open_session({"path": str(target), "profile_auto": True})
    sid = opened["session_id"]
    assert opened["session_defaults"]["resolved_profile_id"] == "python"

    diag = svc.execute(
        {
            "session_id": sid,
            "op": "diagnostics",
            "response_mode": "minimal",
        }
    )
    assert diag["lint_success"] is False
    assert any(issue["code"] == "syntax_check_failed" for issue in diag["issues"])


def test_format_range_with_external_profile(tmp_path: Path) -> None:
    target = tmp_path / "format_me.txt"
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    (profiles_dir / "upperfmt.json").write_text(
        (
            '{"id":"upperfmt","extensions":["txt"],"priority":100,'
            '"format_cmd":["python","-c","from pathlib import Path;'
            'p=Path(r\\"{path}\\");'
            'p.write_text(p.read_text(encoding=\\"utf-8\\").upper(),encoding=\\"utf-8\\")"]}'
        ),
        encoding="utf-8",
    )
    old_profiles_env = os.environ.get("TEXT_EDITOR_PROFILES_DIR")
    os.environ["TEXT_EDITOR_PROFILES_DIR"] = str(profiles_dir)
    try:
        svc = _mk_service(tmp_path)
        opened = svc.open_session({"path": str(target), "profile_id": "upperfmt", "profile_auto": False})
        sid = opened["session_id"]
        formatted = svc.execute(
            {
                "session_id": sid,
                "expected_revision": 1,
                "op": "format_range",
                "op_args": {"line_start": 2, "line_end": 2},
                "response_mode": "minimal",
            }
        )
        assert formatted["current_revision"] == 2
        assert formatted["formatted"] is True
        after = svc.execute({"session_id": sid, "op": "get_view", "response_mode": "minimal"})
        assert any("TWO" in line for line in after["view"])
    finally:
        if old_profiles_env is None:
            os.environ.pop("TEXT_EDITOR_PROFILES_DIR", None)
        else:
            os.environ["TEXT_EDITOR_PROFILES_DIR"] = old_profiles_env


def test_yaml_profile_loading_when_yaml_available(tmp_path: Path) -> None:
    try:
        import yaml  # type: ignore # noqa: F401
    except Exception:
        return
    target = tmp_path / "yaml_profile.txt"
    target.write_text("a\n", encoding="utf-8")
    profiles_dir = tmp_path / "profiles_yaml"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    (profiles_dir / "custom.yaml").write_text(
        "id: yaml_plain\nextensions: [txt]\npriority: 50\nindent_mode: soft\ntab_width: 2\n",
        encoding="utf-8",
    )
    old_profiles_env = os.environ.get("TEXT_EDITOR_PROFILES_DIR")
    os.environ["TEXT_EDITOR_PROFILES_DIR"] = str(profiles_dir)
    try:
        svc = _mk_service(tmp_path)
        opened = svc.open_session({"path": str(target), "profile_id": "yaml_plain", "profile_auto": False})
        assert opened["session_defaults"]["resolved_profile_id"] == "yaml_plain"
    finally:
        if old_profiles_env is None:
            os.environ.pop("TEXT_EDITOR_PROFILES_DIR", None)
        else:
            os.environ["TEXT_EDITOR_PROFILES_DIR"] = old_profiles_env


def test_cleanup_stale_sessions(tmp_path: Path) -> None:
    target = tmp_path / "stale.txt"
    target.write_text("x\n", encoding="utf-8")
    svc = _mk_service(tmp_path)
    opened = svc.open_session({"path": str(target)})
    sid = opened["session_id"]
    info = svc.storage.get_session_info(sid)
    session_db = Path(str(info["session_db_path"]))
    assert session_db.exists()

    old_ts = int(time.time()) - 40 * 24 * 60 * 60
    with sqlite3.connect(str(svc.storage.registry_path)) as conn:
        conn.execute(
            "UPDATE sessions_registry SET last_write_at=? WHERE session_id=?",
            (old_ts, sid),
        )
        conn.commit()

    result = svc.execute({"op": "cleanup_stale_sessions", "op_args": {"stale_after_days": 30}})
    assert result["ok"] is True
    assert result["stats"]["checked"] >= 1
    assert result["stats"]["removed"] + result["stats"]["missing_db"] >= 1

    with sqlite3.connect(str(svc.storage.registry_path)) as conn:
        row = conn.execute("SELECT COUNT(*) FROM sessions_registry WHERE session_id=?", (sid,)).fetchone()
    assert row is not None
    assert int(row[0]) == 0


def test_telemetry_report(tmp_path: Path) -> None:
    target = tmp_path / "telemetry.txt"
    target.write_text("a\nb\n", encoding="utf-8")
    svc = _mk_service(tmp_path)
    opened = svc.open_session({"path": str(target), "capabilities_hint": "search"})
    sid = opened["session_id"]
    _view = svc.execute({"session_id": sid, "op": "get_view", "response_mode": "minimal"})
    rep = svc.execute({"op": "telemetry_report"})
    assert rep["ok"] is True
    assert rep["report"]["entries"] >= 2
    assert rep["report"]["totals"]["request_tokens_est"] >= 1


def test_assign_workspace_admin_op(tmp_path: Path) -> None:
    svc = _mk_service(tmp_path)
    ws = tmp_path / "sample.code-workspace"
    ws.write_text(
        '{"folders":[{"path":"."},{"path":"../docs"}]}',
        encoding="utf-8",
    )
    res = svc.execute({"op": "assign_workspace", "op_args": {"workspace_file": str(ws)}})
    assert res["ok"] is True
    assert any(Path(p).resolve() == tmp_path.resolve() for p in res["allowed_roots"])
    pol = svc.execute({"op": "policy_show"})
    assert pol["ok"] is True
    assert "allowed_roots" in pol
    assert pol["policy_source"] in {"assigned_workspace", "multi_workspace"}
    assert pol["workspace_file"].endswith("sample.code-workspace")
    assert pol["bindings_count"] >= 1
    assert any(str(item).endswith("sample.code-workspace") for item in pol["active_workspaces"])


def test_basic_logger_writes_journal(tmp_path: Path) -> None:
    target = tmp_path / "log_target.txt"
    target.write_text("x\n", encoding="utf-8")
    svc = _mk_service(tmp_path)
    opened = svc.open_session({"path": str(target)})
    _ = svc.execute({"session_id": opened["session_id"], "op": "get_view", "response_mode": "minimal"})
    mcp_tools_root = Path(__file__).resolve().parents[1]
    log_dir = mcp_tools_root / "logs"
    files = sorted(log_dir.glob("*/text_editor_service*_*.log"))
    if not files:
        # fallback for environments where shared log dir is unavailable
        local = Path(str(svc.storage.data_dir)) / "logs"
        files = sorted(local.glob("*/text_editor_service*_*.log"))
    assert files
    text = files[-1].read_text(encoding="utf-8")
    assert "open_session_ok" in text
    assert "execute_ok" in text


def test_op_help_inline_schema(tmp_path: Path) -> None:
    svc = _mk_service(tmp_path)
    res = svc.execute({"op": "op_help", "op_args": {"ops": ["replace_range", "save_revision"]}})
    assert res["ok"] is True
    help_payload = res["help"]["help_by_op"]
    assert "replace_range" in help_payload
    assert "save_revision" in help_payload
    assert "line_start" in help_payload["replace_range"]["markdown"]
    assert "op_args_schema" in help_payload["replace_range"]
    assert "templates" in help_payload["replace_range"]
    assert help_payload["replace_range"]["requires_expected_revision"] is True


def test_help_selftest_and_validation_example_payload(tmp_path: Path) -> None:
    target = tmp_path / "selftest.txt"
    target.write_text("one\ntwo\n", encoding="utf-8")
    svc = _mk_service(tmp_path)
    opened = svc.open_session({"path": str(target)})
    sid = opened["session_id"]
    selftest = svc.execute({"op": "help_selftest"})
    assert selftest["ok"] is True
    assert "checks" in selftest
    try:
        svc.execute({"session_id": sid, "op": "replace_range", "op_args": {"line_start": 1, "line_end": 1}})
        assert False, "expected validation error"
    except Exception as exc:
        payload = getattr(exc, "to_payload", lambda: {})()
        assert payload.get("code") == "invalid_request"
        assert "example_payload" in payload.get("details", {})


def test_session_mod_mvp_and_filtered_help(tmp_path: Path) -> None:
    target = tmp_path / "mod.txt"
    target.write_text("a\nb\nc\n", encoding="utf-8")
    svc = _mk_service(tmp_path)
    opened = svc.open_session({"path": str(target)})
    sid = opened["session_id"]
    first = svc.execute(
        {
            "session_id": sid,
            "expected_revision": 1,
            "op": "replace_range",
            "op_args": {"line_start": 2, "line_end": 2, "replacement_lines": ["B"]},
            "response_mode": "minimal",
        }
    )
    assert first["current_revision"] == 2
    assert str(first.get("command_id", "")).startswith("cmd_")
    preview = svc.execute_mod(
        {
            "session_id": sid,
            "derived_from": "last_success",
            "run_mode": "preview",
            "expected_revision": 2,
            "line_start": 3,
            "line_end": 3,
            "replacement_lines": ["C"],
        }
    )
    assert preview["executed"] is False
    assert preview["resolved_payload"]["op"] == "replace_range"
    applied = svc.execute_mod(
        {
            "session_id": sid,
            "derived_from": "last_success",
            "expected_revision": 2,
            "line_start": 3,
            "line_end": 3,
            "replacement_lines": ["C"],
        }
    )
    assert applied["executed"] is True
    assert applied["result"]["current_revision"] == 3
    assert str(applied.get("base_command_id", "")).startswith("cmd_")
    by_id = svc.execute_mod(
        {
            "session_id": sid,
            "derived_from": str(first["command_id"]),
            "run_mode": "preview",
            "expected_revision": 2,
            "line_start": 1,
            "line_end": 1,
            "replacement_lines": ["A"],
        }
    )
    assert by_id["executed"] is False
    assert by_id["base_command_id"] == first["command_id"]

    try:
        svc.execute_mod(
            {
                "session_id": sid,
                "derived_from": "last_success",
                "target_op": "replace_range",
                "line_start": 1,
                "line_end": 1,
                "replacement_lines": ["X"],
            }
        )
        assert False, "expected validation error for missing explicit expected_revision"
    except Exception as exc:
        payload = getattr(exc, "to_payload", lambda: {})()
        assert payload.get("code") == "invalid_request"

    help_res = svc.execute(
        {
            "op": "op_help",
            "op_args": {"ops": ["replace_range"], "sections": ["op_args_schema", "templates"], "verbosity": "brief"},
        }
    )
    card = help_res["help"]["help_by_op"]["replace_range"]
    assert "op_args_schema" in card
    assert "templates" in card


def test_op_help_structured_json_mode(tmp_path: Path) -> None:
    svc = _mk_service(tmp_path)
    res = svc.execute(
        {
            "op": "op_help",
            "op_args": {
                "ops": ["replace_range"],
                "output_mode": "structured_json",
                "sections": ["contract", "op_args_schema", "templates"],
            },
        }
    )
    assert res["ok"] is True
    help_payload = res["help"]
    assert help_payload["format"] == "structured_json"
    rr = help_payload["ops"]["replace_range"]
    assert rr["contract"]["requires_expected_revision"] is True
    assert "input_schema" in rr
    assert "line_start" in rr["input_schema"]["properties"]
    assert "errors" in rr

