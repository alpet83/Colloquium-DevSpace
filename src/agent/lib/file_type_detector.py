# /lib/file_type_detector.py — классификация файлов для скана проекта и контекста LLM
# Доп. BL расширений: env EXTRA_BLACKLIST_TYPES — через запятую или «;», например: .bak,.min.js,.woff3
# Фрагменты пути (подстрока в posix-пути, lower): встроенно /.git/; env SUBPATH_EXCLUDE_FILTER — разделители , ; : (как в PATH)
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

# Non-code text groups (single source of truth).
DOCUMENT_EXTENSIONS: tuple[str, ...] = (
    ".md",
    ".conf",
    ".toml",
    ".rulz",
)

TEXT_FILE_EXTENSIONS: tuple[str, ...] = (
    ".env",
    ".json",
    ".xml",
    ".yml",
    ".yaml",
    ".txt",
)

NON_CODE_TEXT_EXTENSIONS: frozenset[str] = frozenset(DOCUMENT_EXTENSIONS + TEXT_FILE_EXTENSIONS)

# Расширения, всегда считаем двоичными (исполняемые, библиотеки, медиа, движки БД и т.п.)
BINARY_EXTENSIONS: frozenset[str] = frozenset(
    {
        # Исполняемые / объектники / пакеты
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".bin",
        ".o",
        ".a",
        ".lib",
        ".obj",
        ".class",
        ".jar",
        ".war",
        ".ear",
        ".pyc",
        ".pyo",
        ".wasm",
        ".efi",
        ".msi",
        ".msix",
        ".deb",
        ".rpm",
        ".dmg",
        ".pkg",
        ".com",
        ".sys",
        ".drv",
        ".cpl",
        ".scr",
        ".appimage",
        ".snap",
        ".flatpak",
        # Картинки (не .svg — текстовый XML)
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".bmp",
        ".ico",
        ".tiff",
        ".tif",
        ".heic",
        ".heif",
        ".avif",
        ".jxl",
        ".psd",
        ".xcf",
        # Аудио / видео
        ".mp3",
        ".mp4",
        ".m4a",
        ".aac",
        ".flac",
        ".ogg",
        ".opus",
        ".wav",
        ".wma",
        ".avi",
        ".mov",
        ".mkv",
        ".webm",
        ".wmv",
        ".flv",
        # Архивы и сжатие
        ".zip",
        ".gz",
        ".xz",
        ".7z",
        ".rar",
        ".tar",
        ".bz2",
        ".lz4",
        ".zst",
        ".cab",
        # БД и служебные файлы движков
        ".db",
        ".sqlite",
        ".sqlite3",
        ".mdb",
        ".ldb",
        ".sst",
        ".wal",
        ".shm",
        ".db-wal",
        ".db-shm",
        ".frm",
        ".ibd",
        ".myd",
        ".myi",
        # Шрифты
        ".ttf",
        ".otf",
        ".woff",
        ".woff2",
        ".eot",
        # Прочее бинарное
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".odt",
        ".ods",
        ".odp",
        ".elf",
        ".crdownload",
        ".tar.gz",
        ".tar.bz2",
        ".tar.xz",
        ".tar.zst",
        ".tar.lz4",
    }
)

_EXTRA_BL_RE = re.compile(r"^\.[\w.-]+$", re.ASCII)


def _parse_extra_bl(raw: str) -> frozenset[str]:
    """Токены из EXTRA_BLACKLIST_TYPES: нормализация к нижнему регистру и ведущей точке."""
    if not raw or not str(raw).strip():
        return frozenset()
    out: set[str] = set()
    s = str(raw).strip().replace(";", ",")
    for part in s.split(","):
        t = part.strip().lower()
        if not t:
            continue
        if not t.startswith("."):
            t = "." + t
        if _EXTRA_BL_RE.fullmatch(t):
            out.add(t)
    return frozenset(out)


_BL_MERGED: frozenset[str] | None = None
_BL_MERGED_ENV_SNAP: str | None = None


def bl_exts() -> frozenset[str]:
    """Полный BL расширений: встроенный BINARY_EXTENSIONS ∪ EXTRA_BLACKLIST_TYPES (env)."""
    global _BL_MERGED, _BL_MERGED_ENV_SNAP
    snap = os.environ.get("EXTRA_BLACKLIST_TYPES", "")
    if _BL_MERGED is not None and _BL_MERGED_ENV_SNAP == snap:
        return _BL_MERGED
    extra = _parse_extra_bl(snap)
    _BL_MERGED = frozenset(BINARY_EXTENSIONS | extra)
    _BL_MERGED_ENV_SNAP = snap
    return _BL_MERGED


_DEFAULT_SUBPATH_EXCLUDE: tuple[str, ...] = ("/.git/",)

_SUBPATH_EXCLUDE_DELIM = re.compile(r"[,;:]+")

_SUBPATH_EXCL: tuple[str, ...] | None = None
_SUBPATH_EXCL_SNAP: str | None = None


def _parse_subpath_exclude(raw: str) -> tuple[str, ...]:
    """Токены из SUBPATH_EXCLUDE_FILTER: разделители запятая, ; и : (как PATH); нормализация к lower и /."""
    if not raw or not str(raw).strip():
        return ()
    out: list[str] = []
    for part in _SUBPATH_EXCLUDE_DELIM.split(str(raw).strip()):
        t = part.strip().lower().replace("\\", "/")
        if len(t) < 2:
            continue
        if not t.startswith("/"):
            t = "/" + t
        out.append(t)
    return tuple(dict.fromkeys(out))


def subpath_exclude_patterns() -> tuple[str, ...]:
    """Подстроки пути: дефолт /.git/ ∪ SUBPATH_EXCLUDE_FILTER (кэш на процесс)."""
    global _SUBPATH_EXCL, _SUBPATH_EXCL_SNAP
    snap = os.environ.get("SUBPATH_EXCLUDE_FILTER", "")
    if _SUBPATH_EXCL is not None and _SUBPATH_EXCL_SNAP == snap:
        return _SUBPATH_EXCL
    extra = _parse_subpath_exclude(snap)
    _SUBPATH_EXCL = tuple(dict.fromkeys(_DEFAULT_SUBPATH_EXCLUDE + extra))
    _SUBPATH_EXCL_SNAP = snap
    return _SUBPATH_EXCL


def path_subpath_excluded(path: Path) -> bool:
    """True, если полный posix-путь содержит один из маркеров (напр. /.git/)."""
    try:
        s = path.as_posix().lower()
    except (OSError, ValueError):
        s = str(path).replace("\\", "/").lower()
    for pat in subpath_exclude_patterns():
        if pat in s:
            return True
    return False


_BINARY_COMPOUND_SUFFIXES: tuple[str, ...] = (
    ".tar.gz",
    ".tar.bz2",
    ".tar.xz",
    ".tar.zst",
    ".tar.lz4",
)


def _norm_ext(path: Path) -> str:
    name_lower = path.name.lower()
    for suf in _BINARY_COMPOUND_SUFFIXES:
        if name_lower.endswith(suf):
            return suf
    suf = path.suffix.lower()
    return ("." + suf.lstrip(".")) if suf else ""


_SP_EXT_WL: frozenset[str] | None = None


def sp_ext_wl() -> frozenset[str]:
    """WL (whitelist) расширений `.ext` из SP (SandwichPack); кэш на процесс."""
    global _SP_EXT_WL
    if _SP_EXT_WL is not None:
        return _SP_EXT_WL
    from lib.sandwich_pack import SandwichPack

    if not SandwichPack._block_classes:
        SandwichPack.load_block_classes()
    out: set[str] = set()
    for bc in SandwichPack._block_classes:
        for t in getattr(bc, "supported_types", ()):
            if isinstance(t, str) and t.startswith("."):
                out.add(t.lower())
    _SP_EXT_WL = frozenset(out)
    return _SP_EXT_WL


def _mime_bin(mime: str) -> bool:
    m = mime.split(";")[0].strip().lower()
    if m.startswith("text/"):
        return False
    if m in ("inode/x-empty", "inode/directory"):
        return False
    if m in (
        "application/json",
        "application/xml",
        "application/javascript",
        "application/x-javascript",
        "application/ecmascript",
        "application/sql",
        "application/x-sh",
        "application/x-php",
        "application/x-ruby",
        "application/x-yaml",
        "application/yaml",
        "application/toml",
        "application/x-wine-extension-ini",
    ):
        return False
    if m.startswith("image/") or m.startswith("video/") or m.startswith("audio/"):
        return True
    if m.startswith("application/vnd."):
        return True
    if m in (
        "application/octet-stream",
        "application/x-executable",
        "application/x-sharedlib",
        "application/x-pie-executable",
        "application/x-object",
        "application/zip",
        "application/gzip",
        "application/x-gzip",
        "application/x-bzip2",
        "application/x-xz",
        "application/x-7z-compressed",
        "application/x-rar-compressed",
        "application/java-archive",
        "application/x-java-archive",
    ):
        return True
    return False


def _mime_txt(mime: str) -> bool:
    m = mime.split(";")[0].strip().lower()
    if m.startswith("text/"):
        return True
    if m in ("inode/x-empty",):
        return True
    if m in (
        "application/json",
        "application/xml",
        "application/javascript",
        "application/x-javascript",
        "application/ecmascript",
        "application/sql",
        "application/x-sh",
        "application/x-php",
        "application/x-ruby",
        "application/x-yaml",
        "application/yaml",
        "application/toml",
    ):
        return True
    return False


def mime_file(path: Path, timeout_sec: float = 2.0) -> str | None:
    """MIME через `file -b --mime-type` (Unix). Нет `file` в PATH — None."""
    if not path.is_file():
        return None
    if not shutil.which("file"):
        return None
    try:
        r = subprocess.run(
            ["file", "-b", "--mime-type", str(path)],
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
        if r.returncode != 0 or not r.stdout:
            return None
        return r.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None


def bytes_txt(sample: bytes, min_ratio: float = 0.88) -> bool:
    """Быстрая эвристика: сэмпл похож на текст (UTF-8-friendly); для octet-stream / без `file`."""
    if not sample:
        return True
    chunk = sample[:32768]
    if b"\x00" in chunk:
        return False
    n = len(chunk)
    if n == 0:
        return True
    ok = 0
    for b in chunk:
        if b == 9 or b == 10 or b == 13:
            ok += 1
        elif 32 <= b <= 126:
            ok += 1
        elif b >= 128:
            ok += 1
    return (ok / n) >= min_ratio


def bhead(path: Path, max_bytes: int = 32768) -> bytes | None:
    """Первые max_bytes файла (binary read)."""
    try:
        with path.open("rb") as f:
            return f.read(max_bytes)
    except OSError:
        return None


def is_acceptable_file(path: Path) -> bool:
    """
    Укладывается ли файл в scan/index проекта (attached_files при scan_project_files).
    BL (blacklist) ext → нет; SP WL → да; иначе MIME (`file`) либо bytes_txt по сэмплу.
    """
    if not path.is_file():
        return False
    if path_subpath_excluded(path):
        return False
    ext = _norm_ext(path)
    if ext and ext in bl_exts():
        return False
    name = path.name
    wl = sp_ext_wl()
    if ext and ext.lower() in wl:
        return True
    from lib.sandwich_pack import SandwichPack

    if SandwichPack.supported_type(ext) or SandwichPack.supported_type(name):
        return True
    mime = mime_file(path)
    if mime:
        if _mime_bin(mime) and not _mime_txt(mime):
            if mime.strip().lower() == "application/octet-stream":
                sample = bhead(path)
                return bool(sample is not None and bytes_txt(sample))
            return False
        if _mime_txt(mime):
            return True
    sample = bhead(path)
    if sample is None:
        return False
    return bytes_txt(sample)


def ctx_allows_text(
    path: Path | None,
    extension: str,
    *,
    utf8_content: str | None = None,
) -> bool:
    """
    LLM ctx (контекст): расширение вне SP — можно ли вшить текст.
    BL ext → нет; иначе MIME по диску или bytes_txt по уже декодированной строке.
    """
    ext = extension.lower() if extension else ""
    if not ext.startswith("."):
        ext = "." + ext if ext else ""
    if ext in bl_exts():
        return False
    if path is not None and path.is_file():
        if path_subpath_excluded(path):
            return False
        mime = mime_file(path)
        if mime:
            if _mime_txt(mime):
                return True
            if _mime_bin(mime) and mime.strip().lower() != "application/octet-stream":
                return False
            if mime.strip().lower() == "application/octet-stream":
                sample = bhead(path)
                return bool(sample is not None and bytes_txt(sample))
        sample = bhead(path)
        return bool(sample is not None and bytes_txt(sample))
    if utf8_content is not None:
        data = utf8_content.encode("utf-8", errors="replace")
        return bytes_txt(data[:32768])
    return False
