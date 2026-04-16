from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from text_editor.config import assign_workspace_allowed_roots, load_policy, policy_bindings


def test_policy_auto_init_and_assign_workspace(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    root1 = tmp_path / "ws1"
    root2 = tmp_path / "ws2"
    root1.mkdir(parents=True, exist_ok=True)
    root2.mkdir(parents=True, exist_ok=True)
    policy = load_policy(data_dir)
    policy_path = data_dir / "security_policy.json"
    assert policy_path.exists()
    assert len(policy.allowed_roots) >= 1

    ws_file = tmp_path / "local.code-workspace"
    ws_file.write_text(
        json.dumps({"folders": [{"path": "ws1"}, {"path": "ws2"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    updated = assign_workspace_allowed_roots(data_dir, ws_file)
    assert str(root1.resolve()) in updated.allowed_roots
    assert str(root2.resolve()) in updated.allowed_roots
    raw = json.loads(policy_path.read_text(encoding="utf-8"))
    assert str(root1.resolve()) in raw["allowed_roots"]
    bindings = policy_bindings(data_dir)
    assert str(ws_file.resolve()) in bindings
    assert str(root1.resolve()) in bindings[str(ws_file.resolve())]


def test_policy_auto_init_uses_cwd_workspace(tmp_path: Path) -> None:
    data_dir = tmp_path / "data2"
    ws_root = tmp_path / "wsroot"
    docs_root = tmp_path / "docsroot"
    ws_root.mkdir(parents=True, exist_ok=True)
    docs_root.mkdir(parents=True, exist_ok=True)
    ws = tmp_path / "auto.code-workspace"
    ws.write_text(
        json.dumps({"folders": [{"path": "wsroot"}, {"path": "docsroot"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    old_cwd = Path.cwd()
    try:
        os.chdir(tmp_path)
        policy = load_policy(data_dir)
    finally:
        os.chdir(old_cwd)
    assert str(ws_root.resolve()) in policy.allowed_roots
    assert str(docs_root.resolve()) in policy.allowed_roots


def test_assign_workspace_merges_bindings_not_replace(tmp_path: Path) -> None:
    data_dir = tmp_path / "data3"
    a = tmp_path / "a"
    b = tmp_path / "b"
    c = tmp_path / "c"
    a.mkdir(parents=True, exist_ok=True)
    b.mkdir(parents=True, exist_ok=True)
    c.mkdir(parents=True, exist_ok=True)
    ws1 = tmp_path / "ws1.code-workspace"
    ws2 = tmp_path / "ws2.code-workspace"
    ws1.write_text(json.dumps({"folders": [{"path": "a"}, {"path": "b"}]}), encoding="utf-8")
    ws2.write_text(json.dumps({"folders": [{"path": "c"}]}), encoding="utf-8")
    assign_workspace_allowed_roots(data_dir, ws1)
    policy = assign_workspace_allowed_roots(data_dir, ws2)
    assert str(a.resolve()) in policy.allowed_roots
    assert str(b.resolve()) in policy.allowed_roots
    assert str(c.resolve()) in policy.allowed_roots


def test_active_workspace_reloads_roots_from_workspace_file(tmp_path: Path) -> None:
    data_dir = tmp_path / "data4"
    a = tmp_path / "a"
    b = tmp_path / "b"
    c = tmp_path / "c"
    a.mkdir(parents=True, exist_ok=True)
    b.mkdir(parents=True, exist_ok=True)
    c.mkdir(parents=True, exist_ok=True)
    ws = tmp_path / "live.code-workspace"
    ws.write_text(json.dumps({"folders": [{"path": "a"}, {"path": "b"}]}), encoding="utf-8")
    p1 = assign_workspace_allowed_roots(data_dir, ws)
    assert str(a.resolve()) in p1.allowed_roots
    assert str(b.resolve()) in p1.allowed_roots
    ws.write_text(json.dumps({"folders": [{"path": "a"}, {"path": "c"}]}), encoding="utf-8")
    p2 = load_policy(data_dir)
    assert str(a.resolve()) in p2.allowed_roots
    assert str(c.resolve()) in p2.allowed_roots
    assert str(b.resolve()) not in p2.allowed_roots
