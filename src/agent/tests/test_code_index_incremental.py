# test_code_index_incremental.py — юнит-тесты инкрементального индекса (без ядра БД).
#
# Загрузка модуля по пути к файлу — без «import lib», чтобы не конфликтовать с другим lib на PYTHONPATH.
#
# Запуск из каталога agent:
#   pip install pytest
#   bash scripts/run_code_index_incremental_tests.sh
#   # или MCP git_bash_exec: timeout 90s python -m pytest tests/test_code_index_incremental.py -v
#   python scripts/run_code_index_incremental_tests.py
# или: PYTHONPATH=. python -m pytest tests/test_code_index_incremental.py -v
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

_AGENT = Path(__file__).resolve().parents[1]
_MOD_PATH = _AGENT / "lib" / "code_index_incremental.py"


def _load_cii():
    spec = importlib.util.spec_from_file_location("code_index_incremental", _MOD_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {_MOD_PATH}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_cii = _load_cii()
INDEX_PACKER_VERSION = _cii.INDEX_PACKER_VERSION
build_fingerprints = _cii.build_fingerprints
compute_dirty = _cii.compute_dirty
merge_index = _cii.merge_index
need_fingerprint_seed = _cii.need_fingerprint_seed
should_force_full = _cii.should_force_full
stamp_rebuild_duration = _cii.stamp_rebuild_duration
validate_cache = _cii.validate_cache
_file_id_entity_line = _cii._file_id_entity_line
_file_id_file_row = _cii._file_id_file_row


def _minimal_cache():
    return {
        "packer_version": INDEX_PACKER_VERSION,
        "entities": ["pub,function,,foo,1,1-2,10"],
        "files": ["1,a.py,md5,5,2020-01-01"],
        "file_fingerprints": {"1": {"ts": 100}},
        "templates": {},
        "rebuild_revision": 0,
    }


def test_validate_cache_ok():
    assert validate_cache(_minimal_cache()) is True


def test_validate_cache_bad():
    assert validate_cache({}) is False
    assert validate_cache({"entities": []}) is False


def test_need_fingerprint_seed():
    c = _minimal_cache()
    assert need_fingerprint_seed(c) is False
    del c["file_fingerprints"]
    assert need_fingerprint_seed(c) is True


def test_compute_dirty_ts():
    cache = _minimal_cache()
    entries = [{"id": 1, "ts": 100}]
    d, r = compute_dirty(cache, entries, use_size=False)
    assert d == set() and r == set()

    entries[0]["ts"] = 200
    d, r = compute_dirty(cache, entries, use_size=False)
    assert d == {1} and r == set()


def test_compute_dirty_new_and_removed():
    cache = _minimal_cache()
    entries = [{"id": 1, "ts": 100}, {"id": 2, "ts": 50}]
    d, r = compute_dirty(cache, entries, use_size=False)
    assert d == {2} and r == set()

    cache["file_fingerprints"] = {"1": {"ts": 100}, "2": {"ts": 50}}
    entries = [{"id": 1, "ts": 100}]
    d, r = compute_dirty(cache, entries, use_size=False)
    assert d == set() and r == {2}


def test_should_force_full():
    assert should_force_full({"rebuild_revision": 49}, 50) is False
    assert should_force_full({"rebuild_revision": 50}, 50) is True


def test_stamp_rebuild_duration():
    d = {}
    stamp_rebuild_duration(d, 1.23456)
    assert d["rebuild_duration"] == 1.235


def test_merge_removes_and_appends():
    prev = {
        "packer_version": INDEX_PACKER_VERSION,
        "entities": [
            "pub,function,,a,1,1-2,1",
            "pub,function,,b,2,1-2,1",
        ],
        "files": ["1,a.py,x,1,t1", "2,b.py,x,1,t2"],
        "templates": {"entities": "x"},
        "rebuild_revision": 0,
        "file_fingerprints": {"1": {"ts": 1}, "2": {"ts": 1}},
    }
    partial = {
        "entities": ["pub,function,,a,1,3-4,1"],
        "files": ["1,a.py,y,2,t3"],
        "packer_version": INDEX_PACKER_VERSION,
        "templates": {"entities": "x"},
    }
    entries = [{"id": 1, "ts": 2}]
    out = merge_index(
        prev,
        partial,
        dirty_ids={1},
        removed_ids={2},
        file_entries=entries,
        new_revision=1,
        duration_sec=0.042,
    )
    assert out["rebuild_revision"] == 1
    assert out["rebuild_duration"] == 0.042
    assert out["last_build_kind"] == "incremental"
    assert "2,b.py" not in "".join(out["files"])
    ents = out["entities"]
    assert len(ents) == 1
    assert "3-4" in ents[0]


def test_merge_updates_code_base_files():
    prev = {
        "packer_version": INDEX_PACKER_VERSION,
        "entities": ["pub,function,,a,1,1-2,1", "pub,function,,b,2,1-2,1"],
        "files": ["1,a.py,x,1,t1", "2,b.py,x,1,t2"],
        "code_base_files": [1, 2],
        "templates": {"entities": "x"},
        "rebuild_revision": 0,
        "file_fingerprints": {"1": {"ts": 1}, "2": {"ts": 1}},
    }
    partial = {
        "entities": ["pub,function,,a,1,3-4,1", "pub,function,,c,3,1-3,1"],
        "files": ["1,a.py,y,2,t3", "3,c.py,z,1,t3"],
        "code_base_files": [1, 3],
        "packer_version": INDEX_PACKER_VERSION,
        "templates": {"entities": "x"},
    }
    entries = [{"id": 1, "ts": 2}, {"id": 3, "ts": 1}]
    out = merge_index(
        prev,
        partial,
        dirty_ids={1, 3},
        removed_ids={2},
        file_entries=entries,
        new_revision=1,
    )
    assert out["code_base_files"] == [1, 3]


def test_file_id_parsers():
    assert _file_id_entity_line("pub,fn,,n,42,10-11,3") == 42
    assert _file_id_file_row("7,path/with,commas/in,name,x,1,2,3,4,5") is not None


def test_build_fingerprints():
    fp = build_fingerprints([{"id": 3, "ts": 9, "size_bytes": 12}])
    assert fp["3"]["ts"] == 9
    assert fp["3"]["size_bytes"] == 12


def test_env_incremental_mode_default_and_refresh(monkeypatch):
    monkeypatch.delenv("CORE_INDEX_INCREMENTAL_MODE", raising=False)
    assert _cii.env_incremental_mode() == "fast"
    monkeypatch.setenv("CORE_INDEX_INCREMENTAL_MODE", "refresh")
    assert _cii.env_incremental_mode() == "refresh"
    monkeypatch.setenv("CORE_INDEX_INCREMENTAL_MODE", "FAST")
    assert _cii.env_incremental_mode() == "fast"
    monkeypatch.setenv("CORE_INDEX_INCREMENTAL_MODE", "unknown")
    assert _cii.env_incremental_mode() == "fast"


def test_project_routes_imports_incremental_helpers():
    """Регрессия: вызовы из lib.code_index_incremental должны быть в import-блоке (без полного import routes)."""
    pr = _AGENT / "routes" / "project_routes.py"
    text = pr.read_text(encoding="utf-8")
    m = re.search(
        r"from lib\.code_index_incremental import \(\s*(.*?)\s*\)",
        text,
        re.DOTALL,
    )
    assert m is not None, "import block from lib.code_index_incremental not found"
    block = m.group(1)
    for name in (
        "attach_full_metadata",
        "build_fingerprints",
        "compute_dirty",
        "env_dirty_use_size",
        "env_incremental_enabled",
        "env_incremental_mode",
        "env_max_inc_revs",
        "merge_index",
        "need_fingerprint_seed",
        "should_force_full",
        "stamp_rebuild_duration",
        "validate_cache",
    ):
        assert name in block, f"missing in project_routes import: {name}"
