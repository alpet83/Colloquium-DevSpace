# /lib/file_link_prefix.py — маркер ссылки в attached_files.file_name
#
# Проблема: ведущий «@» в БД снимали через lstrip('@'), из‑за чего терялись все ведущие @
# у относительного пути (например npm-скоуп @scope/pkg).
#
# Решение: новые строки пишем с префиксом ® (U+00AE); при чтении снимаем ровно один маркер
# ® или @ (мягкая миграция). Путь после маркера хранится как есть.
#
# Разметка чата (@attached_file#, @user) не меняется — только поле file_name в attached_files.

from __future__ import annotations

REF = "\u00ae"  # ® зарегистрированный знак — редко в начале пути
LEGACY_AT = "@"


def strip_storage_prefix(s: str) -> str:
    """Снять ровно один ведущий маркер ссылки (® или @). Не трогает второй символ @ у пути."""
    t = str(s)
    if t.startswith(REF):
        return t[len(REF) :]
    if t.startswith(LEGACY_AT):
        return t[len(LEGACY_AT) :]
    return t


def has_storage_prefix(s: str) -> bool:
    t = str(s)
    return t.startswith(REF) or t.startswith(LEGACY_AT)


def store_storage_path(rel: str) -> str:
    """Строка для INSERT/UPDATE: ® + относительный путь (обрезаем только ведущие /)."""
    r = str(rel).lstrip("/")
    return REF + r


def sql_link_prefixed_params() -> tuple[str, dict]:
    """Фрагмент AND … + параметры: строка — ссылка по префиксу ® или @."""
    return (
        "(file_name LIKE :_fl_pref_ref OR file_name LIKE :_fl_pref_at)",
        {"_fl_pref_ref": f"{REF}%", "_fl_pref_at": f"{LEGACY_AT}%"},
    )
