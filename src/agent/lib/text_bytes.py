# Декодирование текстовых файлов: UTF-8 строго, затем детект и fallback (не «латиница на всё»).
from __future__ import annotations

import codecs
import os
import re
from typing import Optional, Tuple

from lib.file_type_detector import bytes_txt

# Сохранение на диск: lf | crlf | cr | mixed (при mixed пишем как lf).
EOL_CANONICAL = frozenset({"lf", "crlf", "cr", "mixed"})

# После успешного детекта / env: порог «беспорядка» charset-normalizer (ниже — лучше).
_DEFAULT_MAX_CHAOS = 0.35

# Явная цепочка после детекта (редкие случаи без библиотеки или сомнительный best).
_MANUAL_FALLBACKS: tuple[str, ...] = (
    "cp1251",
    "koi8_r",
    "iso8859-5",
    "mac_cyrillic",
    "latin-1",
)


def _env_manual_encodings() -> tuple[str, ...]:
    raw = (os.environ.get("CQDS_TEXT_DECODE_FALLBACK") or "").strip()
    if not raw:
        return ()
    parts = [p.strip() for p in re.split(r"[,;:]+", raw) if p.strip()]
    return tuple(parts)


def _max_chaos() -> float:
    raw = (os.environ.get("CQDS_TEXT_DECODE_MAX_CHAOS") or "").strip()
    if not raw:
        return _DEFAULT_MAX_CHAOS
    try:
        v = float(raw)
        return min(1.0, max(0.0, v))
    except ValueError:
        return _DEFAULT_MAX_CHAOS


def _utf16_bom(data: bytes) -> Optional[Tuple[str, str]]:
    if len(data) < 2:
        return None
    if data[:2] == b"\xff\xfe":
        return data.decode("utf-16-le"), "utf-16-le"
    if data[:2] == b"\xfe\xff":
        return data.decode("utf-16-be"), "utf-16-be"
    return None


def _try_charset_normalizer(data: bytes) -> Optional[Tuple[str, str]]:
    try:
        from charset_normalizer import from_bytes
    except ImportError:
        return None
    matches = from_bytes(data)
    best = matches.best()
    if best is None:
        return None
    chaos = float(getattr(best, "chaos", 1.0))
    if chaos > _max_chaos():
        return None
    enc = str(getattr(best, "encoding", "") or "unknown")
    return str(best), enc


def _try_manual(data: bytes) -> Tuple[str, str]:
    """Последовательный strict decode; latin-1 всегда успешен."""
    extra = _env_manual_encodings()
    seen: set[str] = set()
    for enc in tuple(extra) + _MANUAL_FALLBACKS:
        if enc in seen:
            continue
        seen.add(enc)
        try:
            return data.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("latin-1"), "latin-1"


def normalize_codec_name(name: str | None) -> str:
    """Имя кодека для Python encode/decode (charset-normalizer → нормализованное имя)."""
    if not name or str(name).lower() in ("unknown", "utf-8-replace"):
        return "utf-8"
    n = str(name).strip().lower().replace("_", "-")
    try:
        return codecs.lookup(n).name
    except LookupError:
        return "utf-8"


def normalize_eol_label(eol: str | None) -> str:
    e = (eol or "lf").strip().lower()
    return e if e in EOL_CANONICAL else "lf"


def detect_eol_from_bytes(data: bytes, encoding: str) -> str:
    """Стиль перевода строк в сырых байтах (после выбора кодировки)."""
    if not data:
        return "lf"
    enc = normalize_codec_name(encoding)
    if enc in ("utf-16-le",):
        crlf = data.count(b"\r\x00\n\x00")
        nl = data.count(b"\n\x00")
        cr = data.count(b"\r\x00")
    elif enc in ("utf-16-be",):
        crlf = data.count(b"\x00\r\x00\n")
        nl = data.count(b"\x00\n")
        cr = data.count(b"\x00\r")
    else:
        crlf = data.count(b"\r\n")
        nl = data.count(b"\n")
        cr = data.count(b"\r")
    lone_lf = nl - crlf
    lone_cr = cr - crlf
    if crlf == 0 and lone_cr == 0:
        return "lf"
    if lone_lf == 0 and lone_cr == 0:
        return "crlf"
    if crlf == 0 and lone_lf == 0:
        return "cr"
    return "mixed"


def normalize_newlines_for_write(text: str, eol: str) -> str:
    """Привести str к одному стилю перевода строк для записи."""
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    e = normalize_eol_label(eol)
    if e == "crlf":
        return t.replace("\n", "\r\n")
    if e == "cr":
        return t.replace("\n", "\r")
    return t


def encode_text_to_bytes(text: str, encoding: str, eol: str) -> Tuple[bytes, str]:
    """
    Кодирует текст для записи на диск. Возвращает (bytes, фактическая кодировка после успеха или fallback utf-8).
    При UnicodeEncodeError — utf-8 и нормализация переводов строк к lf.
    """
    enc = normalize_codec_name(encoding)
    normalized = normalize_newlines_for_write(text, eol)
    try:
        return normalized.encode(enc), enc
    except UnicodeEncodeError:
        u8 = normalize_newlines_for_write(text, "lf").encode("utf-8")
        return u8, "utf-8"


def decode_file_bytes(data: bytes) -> Optional[Tuple[str, str, str]]:
    """
    Декодирует содержимое файла в str. Возвращает (text, encoding_name, eol) или None, если похоже на бинарник.

    Политика:
    - Сначала UTF-8 (strict), UTF-16 по BOM, UTF-8-sig.
    - Если UTF-8 не подошёл: при отсутствии текстовой эвристики (bytes_txt) — None.
    - Иначе charset-normalizer при наличии; иначе ручная цепочка (+ CQDS_TEXT_DECODE_FALLBACK).
    """
    if len(data) == 0:
        return "", "utf-8", "lf"

    sample = data if len(data) <= 65536 else data[:65536]
    if b"\x00" in sample:
        return None

    try:
        text, enc = data.decode("utf-8"), "utf-8"
        return text, enc, detect_eol_from_bytes(data, enc)
    except UnicodeDecodeError:
        pass

    bom16 = _utf16_bom(data)
    if bom16 is not None:
        text, enc = bom16
        return text, enc, detect_eol_from_bytes(data, enc)

    if data.startswith(b"\xef\xbb\xbf"):
        try:
            text, enc = data.decode("utf-8-sig"), "utf-8-sig"
            return text, enc, detect_eol_from_bytes(data, enc)
        except UnicodeDecodeError:
            pass

    if not bytes_txt(sample):
        return None

    guessed = _try_charset_normalizer(data)
    if guessed is not None:
        text, enc = guessed
        return text, enc, detect_eol_from_bytes(data, enc)

    text, enc = _try_manual(data)
    return text, enc, detect_eol_from_bytes(data, enc)


def decode_known_text_bytes(data: bytes) -> Tuple[str, str, str]:
    """
    То же, но без отсечения «не текст»: для байтов, которые уже помечены как текстовые контентом БД и т.п.
    """
    r = decode_file_bytes(data)
    if r is not None:
        return r
    eol = detect_eol_from_bytes(data, "utf-8")
    return data.decode("utf-8", errors="replace"), "utf-8-replace", eol
