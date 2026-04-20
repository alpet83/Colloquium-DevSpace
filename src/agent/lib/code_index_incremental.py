# code_index_incremental.py — инкрементальное обновление кеша sandwiches_index (проектный code_index).
from __future__ import annotations

import copy
import json
import os
from datetime import datetime, timezone
from typing import Any

# Версия формата индекса на диске (совпадает с SandwichPack после bump).
INDEX_PACKER_VERSION = "0.7"


def stamp_rebuild_duration(index_dict: dict[str, Any], seconds: float) -> None:
    """Интегральное время последнего ребилда (сек), для сравнения full vs incremental."""
    index_dict["rebuild_duration"] = round(float(seconds), 3)


def env_incremental_enabled() -> bool:
    v = (os.environ.get("CORE_INDEX_ENABLE_INCREMENTAL") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def env_max_inc_revs() -> int:
    """Макс. число инкрементальных ребилдов подряд до принудительного full.

    Читает ``CORE_INDEX_INCREMENTAL_MAX_REVISION`` (целое, по умолчанию 50);
    значения < 1 приводятся к 1. При невалидной строке — 50.
    """
    try:
        return max(1, int(os.environ.get("CORE_INDEX_INCREMENTAL_MAX_REVISION", "50")))
    except ValueError:
        return 50


def env_dirty_use_size() -> bool:
    v = (os.environ.get("CORE_INDEX_DIRTY_USE_SIZE") or "0").strip().lower()
    return v in ("1", "true", "yes", "on")


def env_incremental_mode() -> str:
    """Режим инкрементального ребилда: ``fast`` (по умолчанию) или ``refresh``."""
    v = (os.environ.get("CORE_INDEX_INCREMENTAL_MODE") or "fast").strip().lower()
    return "refresh" if v == "refresh" else "fast"


def _file_id_entity_line(line: str) -> int | None:
    if not isinstance(line, str) or not line.strip():
        return None
    parts = line.split(",")
    if len(parts) < 7:
        return None
    try:
        return int(parts[4].strip())
    except ValueError:
        return None


def _file_id_file_row(line: str) -> int | None:
    """Первое поле file_id у строки filelist (file_name может содержать запятые — берём до 2-го поля аккуратно)."""
    if not isinstance(line, str) or not line.strip():
        return None
    # file_id,file_name,md5,tokens,timestamp — split с limit для имени
    parts = line.split(",", 4)
    if len(parts) < 5:
        return None
    try:
        return int(parts[0].strip())
    except ValueError:
        return None


def validate_cache(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    if not isinstance(payload.get("entities"), list) or not isinstance(payload.get("files"), list):
        return False
    return True


def need_fingerprint_seed(payload: dict[str, Any]) -> bool:
    """Старый кеш без file_fingerprints — нужен полный ребилд для заполнения."""
    fp = payload.get("file_fingerprints")
    if not isinstance(fp, dict) or len(fp) == 0:
        return True
    return False


def build_fingerprints(file_entries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """По строкам file_index: ts обязателен; size_bytes — если есть в записи."""
    out: dict[str, dict[str, Any]] = {}
    for e in file_entries:
        fid = int(e["id"])
        rec: dict[str, Any] = {"ts": int(e["ts"])}
        if e.get("size_bytes") is not None:
            try:
                rec["size_bytes"] = int(e["size_bytes"])
            except (TypeError, ValueError):
                pass
        out[str(fid)] = rec
    return out


def should_force_full(payload: dict[str, Any], max_rev: int) -> bool:
    if not isinstance(payload, dict):
        return True
    try:
        rev = int(payload.get("rebuild_revision", 0))
    except (TypeError, ValueError):
        return True
    return rev >= max_rev


def compute_dirty(
    cache: dict[str, Any],
    file_entries: list[dict[str, Any]],
    *,
    use_size: bool,
) -> tuple[set[int], set[int]]:
    """
    Возвращает (dirty_ids, removed_ids) относительно file_fingerprints в cache.
    dirty: новый файл или изменились ts / (опц.) size.
    removed: id был в отпечатках, но нет в текущем file_entries.
    """
    prev_fp = cache.get("file_fingerprints")
    if not isinstance(prev_fp, dict):
        return set(), set()

    current_ids = {int(e["id"]) for e in file_entries}
    dirty: set[int] = set()
    removed: set[int] = set()

    for k, v in prev_fp.items():
        try:
            fid = int(k)
        except ValueError:
            continue
        if fid not in current_ids:
            removed.add(fid)

    for e in file_entries:
        fid = int(e["id"])
        ts = int(e["ts"])
        old = prev_fp.get(str(fid))
        if old is None:
            dirty.add(fid)
            continue
        try:
            old_ts = int(old.get("ts", -1))
        except (TypeError, ValueError):
            old_ts = -1
        if old_ts != ts:
            dirty.add(fid)
            continue
        if use_size:
            cur_sz = e.get("size_bytes")
            old_sz = old.get("size_bytes")
            if cur_sz is not None and old_sz is not None:
                try:
                    if int(cur_sz) != int(old_sz):
                        dirty.add(fid)
                except (TypeError, ValueError):
                    dirty.add(fid)
            elif cur_sz is not None or old_sz is not None:
                dirty.add(fid)

    return dirty, removed


def _filter_entity_lines(lines: list[Any], drop_ids: set[int]) -> list[str]:
    out: list[str] = []
    for line in lines:
        if not isinstance(line, str):
            continue
        fid = _file_id_entity_line(line)
        if fid is None or fid in drop_ids:
            continue
        out.append(line)
    return out


def _filter_file_lines(lines: list[Any], drop_ids: set[int]) -> list[str]:
    out: list[str] = []
    for line in lines:
        if not isinstance(line, str):
            continue
        fid = _file_id_file_row(line)
        if fid is None or fid in drop_ids:
            continue
        out.append(line)
    return out


def _as_int_set(value: Any) -> set[int]:
    out: set[int] = set()
    if not isinstance(value, list):
        return out
    for v in value:
        try:
            out.add(int(v))
        except (TypeError, ValueError):
            continue
    return out


def merge_index(
    previous: dict[str, Any],
    partial: dict[str, Any] | None,
    *,
    dirty_ids: set[int],
    removed_ids: set[int],
    file_entries: list[dict[str, Any]],
    new_revision: int,
    duration_sec: float | None = None,
) -> dict[str, Any]:
    """
    partial — результат pack() только по dirty file_ids (или None, если только удаления).
    Удаляем строки с file_id ∈ dirty ∪ removed, добавляем строки из partial.
    """
    drop = dirty_ids | removed_ids
    merged = copy.deepcopy(previous)
    merged["entities"] = _filter_entity_lines(previous.get("entities") or [], drop)
    merged["files"] = _filter_file_lines(previous.get("files") or [], drop)
    prev_code_ids = _as_int_set(previous.get("code_base_files"))
    merged_code_ids = {fid for fid in prev_code_ids if fid not in drop}

    if partial:
        pe = partial.get("entities")
        pf = partial.get("files")
        if isinstance(pe, list):
            merged["entities"] = list(merged["entities"]) + [str(x) for x in pe if isinstance(x, str)]
        if isinstance(pf, list):
            merged["files"] = list(merged["files"]) + [str(x) for x in pf if isinstance(x, str)]
        if partial.get("packer_version"):
            merged["packer_version"] = partial["packer_version"]
        if partial.get("templates") and isinstance(partial["templates"], dict):
            merged["templates"] = copy.deepcopy(partial["templates"])
        merged_code_ids.update(_as_int_set(partial.get("code_base_files")))

    merged["context_date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    merged["rebuild_revision"] = int(new_revision)
    merged["last_build_kind"] = "incremental"
    merged["file_fingerprints"] = build_fingerprints(file_entries)
    merged["code_base_files"] = sorted(merged_code_ids)
    merged["packer_version"] = INDEX_PACKER_VERSION
    if duration_sec is not None:
        stamp_rebuild_duration(merged, duration_sec)
    return merged


def attach_full_metadata(
    index_dict: dict[str, Any],
    file_entries: list[dict[str, Any]],
    *,
    duration_sec: float | None = None,
) -> dict[str, Any]:
    """После полного pack: отпечатки, revision, вид сборки."""
    out = copy.deepcopy(index_dict)
    out["packer_version"] = INDEX_PACKER_VERSION
    out["rebuild_revision"] = 0
    out["last_build_kind"] = "full"
    out["file_fingerprints"] = build_fingerprints(file_entries)
    if duration_sec is not None:
        stamp_rebuild_duration(out, duration_sec)
    return out


def safe_load_cache_json(raw: str) -> dict[str, Any] | None:
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else None
    except json.JSONDecodeError:
        return None
