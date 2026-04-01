# Таблица config: entry_key PK, entry_value (переопределение), entry_fallback (дефолт).
# Эффективное значение: entry_value, если не NULL; иначе entry_fallback.
import re
from typing import Any, Optional

from managers.db import Database

_KEY_RE = re.compile(r"^[a-zA-Z0-9._-]{1,256}$")
_UNSET = object()


def validate_config_key(key: str) -> None:
    if not key or not _KEY_RE.match(key):
        raise ValueError("Недопустимый ключ: допустимы 1–256 символов [a-zA-Z0-9._-]")


def effective_value(entry_value: Optional[str], entry_fallback: Optional[str]) -> Optional[str]:
    if entry_value is not None:
        return entry_value
    return entry_fallback


def _row_triplet(row: Any) -> tuple[Optional[str], Optional[str], Optional[str]]:
    if row is None:
        return None, None, None
    m = getattr(row, "_mapping", None)
    if m is not None:
        return (
            m.get("entry_key"),
            m.get("entry_value"),
            m.get("entry_fallback"),
        )
    return row[0], row[1], row[2]


class ConfigStore:
    def __init__(self) -> None:
        self.db = Database.get_database()

    def list_entries(self) -> list[dict]:
        rows = self.db.fetch_all(
            "SELECT entry_key, entry_value, entry_fallback FROM config ORDER BY entry_key"
        )
        out: list[dict] = []
        for row in rows or []:
            k, v, fb = _row_triplet(row)
            out.append(
                {
                    "key": k,
                    "value": v,
                    "fallback": fb,
                    "effective": effective_value(v, fb),
                }
            )
        return out

    def get_entry(self, key: str) -> Optional[dict]:
        validate_config_key(key)
        row = self.db.fetch_one(
            "SELECT entry_key, entry_value, entry_fallback FROM config WHERE entry_key = :k",
            {"k": key},
        )
        if row is None:
            return None
        k, v, fb = _row_triplet(row)
        return {
            "key": k,
            "value": v,
            "fallback": fb,
            "effective": effective_value(v, fb),
        }

    def create(
        self,
        key: str,
        value: Optional[str] = None,
        fallback: Optional[str] = None,
    ) -> dict:
        validate_config_key(key)
        existing = self.db.fetch_one(
            "SELECT 1 FROM config WHERE entry_key = :k",
            {"k": key},
        )
        if existing is not None:
            raise KeyError(key)
        self.db.execute(
            """
            INSERT INTO config (entry_key, entry_value, entry_fallback)
            VALUES (:k, :v, :fb)
            """,
            {"k": key, "v": value, "fb": fallback},
        )
        return self.get_entry(key)  # type: ignore[return-value]

    def patch(
        self,
        key: str,
        *,
        value: Any = _UNSET,
        fallback: Any = _UNSET,
    ) -> Optional[dict]:
        """Поля со значением _UNSET не меняются; явный None записывает NULL в БД."""
        validate_config_key(key)
        if value is _UNSET and fallback is _UNSET:
            raise ValueError("Пустой patch")
        row = self.db.fetch_one(
            "SELECT entry_value, entry_fallback FROM config WHERE entry_key = :k",
            {"k": key},
        )
        if row is None:
            return None
        m = getattr(row, "_mapping", None)
        if m is not None:
            ev, ef = m.get("entry_value"), m.get("entry_fallback")
        else:
            ev, ef = row[0], row[1]

        if value is not _UNSET:
            ev = value
        if fallback is not _UNSET:
            ef = fallback

        self.db.execute(
            """
            UPDATE config
            SET entry_value = :v, entry_fallback = :fb
            WHERE entry_key = :k
            """,
            {"k": key, "v": ev, "fb": ef},
        )
        return self.get_entry(key)

    def delete(self, key: str) -> bool:
        validate_config_key(key)
        r = self.db.execute(
            "DELETE FROM config WHERE entry_key = :k",
            {"k": key},
        )
        try:
            return (r.rowcount or 0) > 0
        except Exception:
            return True

    def apply_patch(self, key: str, patch: dict) -> Optional[dict]:
        """patch — подмножество ключей value/fallback; None записывается в БД."""
        if not patch:
            raise ValueError("Пустой patch")
        value: Any = _UNSET
        fallback: Any = _UNSET
        if "value" in patch:
            value = patch["value"]
        if "fallback" in patch:
            fallback = patch["fallback"]
        if value is _UNSET and fallback is _UNSET:
            raise ValueError("Пустой patch")
        return self.patch(key, value=value, fallback=fallback)
