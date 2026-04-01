"""Host-side smart_grep: search under an arbitrary directory (MCP machine), no Colloquium API.

Uses ripgrep when available; otherwise multithreaded Python scan."""
from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("cqds_smart_grep_host")

# Mirrors agent/routes/project_routes.py (keep in sync when presets change)
SMART_GREP_MODES: dict[str, list[str] | None] = {
    "all": None,
    "code": [
        "*.py",
        "*.js",
        "*.ts",
        "*.tsx",
        "*.vue",
        "*.php",
        "*.java",
        "*.go",
        "*.rs",
        "*.sh",
        "*.json",
        "*.yml",
        "*.yaml",
    ],
    "logs": ["*.log", "*.out", "*.err", "*.trace", "*.txt", "logs/*", "*/logs/*"],
    "docs": ["*.md", "*.rst", "*.adoc", "*.txt"],
}

SMART_GREP_PROFILES: dict[str, list[str] | None] = {
    "all": None,
    "backend": [
        "backend/**",
        "src/agent/**",
        "agent/**",
        "**/*route*.py",
        "**/*controller*.*",
        "**/*service*.*",
    ],
    "frontend": [
        "frontend/**",
        "admin/**",
        "**/*.vue",
        "**/*.tsx",
        "**/*.ts",
        "**/*.js",
        "**/*.css",
    ],
    "docs": ["docs/**", "**/*.md", "**/*.rst", "**/*.adoc", "README*", "readme*"],
    "infra": [
        "docker/**",
        "scripts/**",
        "**/Dockerfile*",
        "**/*.yml",
        "**/*.yaml",
        "**/*.toml",
        "**/*.ini",
    ],
    "tests": ["**/test/**", "**/tests/**", "**/*test*.*", "**/*spec*.*"],
    "logs": [
        "logs/**",
        "**/logs/**",
        "**/*.log",
        "**/*.out",
        "**/*.err",
        "**/*.trace",
        "**/*.txt",
    ],
}


def _is_mode_match(file_name: str, mode: str) -> bool:
    mode = mode if mode in SMART_GREP_MODES else "code"
    globs = SMART_GREP_MODES.get(mode)
    if not globs:
        return True
    path = file_name.replace("\\", "/").lstrip("/")
    return any(fnmatch.fnmatch(path, p) for p in globs)


def _is_profile_match(file_name: str, profile: str) -> bool:
    profile = profile if profile in SMART_GREP_PROFILES else "all"
    globs = SMART_GREP_PROFILES.get(profile)
    if not globs:
        return True
    path = file_name.replace("\\", "/").lstrip("/")
    return any(fnmatch.fnmatch(path, p) for p in globs)


def _iter_candidate_files(
    root: Path,
    mode: str,
    profile: str,
    extra_globs: list[str] | None,
) -> list[Path]:
    out: list[Path] = []
    root = root.resolve()
    for dirpath, _dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        # skip heavy / hidden dirs
        base = Path(dirpath)
        rel_dir = base.relative_to(root).as_posix() if base != root else ""
        parts = set(Path(rel_dir).parts) if rel_dir else set()
        if ".git" in parts or "node_modules" in parts or ".venv" in parts:
            continue
        for name in filenames:
            fp = base / name
            try:
                if not fp.is_file():
                    continue
            except OSError:
                continue
            rel = fp.relative_to(root).as_posix()
            if not _is_mode_match(rel, mode):
                continue
            if not _is_profile_match(rel, profile):
                continue
            if extra_globs and not any(fnmatch.fnmatch(rel, g) for g in extra_globs):
                continue
            out.append(fp)
    return out


def _grep_one_file(
    fp: Path,
    root: Path,
    pattern: re.Pattern[str] | None,
    needle: str,
    is_regex: bool,
    case_sensitive: bool,
    max_per_file: int,
) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    try:
        raw = fp.read_bytes()
        if b"\x00" in raw[:8192]:
            return hits
        text = raw.decode("utf-8", errors="replace")
    except OSError:
        return hits
    lines = text.splitlines()
    rel = fp.relative_to(root).as_posix()
    for i, line in enumerate(lines, start=1):
        if is_regex:
            assert pattern is not None
            m = pattern.search(line)
            if not m:
                continue
            matched_text = m.group(0)[:200]
        else:
            hay = line if case_sensitive else line.lower()
            nd = needle if case_sensitive else needle.lower()
            if nd not in hay:
                continue
            matched_text = needle[:200]
        hits.append(
            {
                "file_id": None,
                "file_name": rel,
                "path": str(fp),
                "line": i,
                "line_text": line[:400],
                "match": matched_text,
                "context_before": [],
                "context_after": [],
            }
        )
        if len(hits) >= max_per_file:
            break
    return hits


def _smart_grep_python(
    root: Path,
    query: str,
    *,
    mode: str,
    profile: str,
    include_glob: list[str] | None,
    is_regex: bool,
    case_sensitive: bool,
    max_results: int,
    context_lines: int,
    workers: int,
) -> dict[str, Any]:
    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = re.compile(query, flags) if is_regex else None
    needle = query
    files = _iter_candidate_files(root, mode, profile, include_glob)
    hits: list[dict[str, Any]] = []
    truncated = False
    max_per_file = max_results

    def task(fp: Path) -> list[dict[str, Any]]:
        return _grep_one_file(
            fp, root, pattern, needle, is_regex, case_sensitive, max_per_file
        )

    w = max(1, min(workers, 32, max(1, len(files))))
    with ThreadPoolExecutor(max_workers=w) as ex:
        futs = [ex.submit(task, fp) for fp in files]
        for fut in as_completed(futs):
            if truncated:
                break
            for h in fut.result():
                hits.append(h)
                if len(hits) >= max_results:
                    truncated = True
                    break
            if truncated:
                break

    if context_lines > 0 and hits:
        # enrich context by re-reading small windows
        by_file: dict[str, list[dict[str, Any]]] = {}
        for h in hits:
            by_file.setdefault(h["path"], []).append(h)
        for path_s, group in by_file.items():
            try:
                lines = Path(path_s).read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for h in group:
                i = h["line"]
                h["context_before"] = lines[max(0, i - 1 - context_lines) : i - 1]
                h["context_after"] = lines[i : i + context_lines]

    return {
        "status": "ok",
        "search_mode": "host_fs",
        "engine": "python_threads",
        "host_path": str(root),
        "mode": mode,
        "profile": profile,
        "query": query,
        "is_regex": is_regex,
        "case_sensitive": case_sensitive,
        "total": len(hits),
        "truncated": truncated,
        "hits": hits[:max_results],
        "files_scanned": len(files),
    }


async def smart_grep_host_fs(
    host_path: str,
    query: str,
    *,
    mode: str = "code",
    profile: str = "all",
    include_glob: list[str] | None = None,
    is_regex: bool = False,
    case_sensitive: bool = False,
    max_results: int = 100,
    context_lines: int = 0,
    timeout_sec: int = 120,
    workers: int = 8,
) -> dict[str, Any]:
    import asyncio

    root = Path(host_path).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"host_path is not a directory: {root}")

    max_results = max(1, min(int(max_results), 10000))
    context_lines = max(0, min(int(context_lines), 3))
    timeout_sec = max(5, min(int(timeout_sec), 600))
    workers = max(1, min(int(workers), 32))

    rg_exe = shutil.which("rg")
    if rg_exe:
        return await _smart_grep_ripgrep(
            rg_exe,
            root,
            query,
            mode=mode,
            profile=profile,
            include_glob=include_glob,
            is_regex=is_regex,
            case_sensitive=case_sensitive,
            max_results=max_results,
            context_lines=context_lines,
            timeout_sec=timeout_sec,
        )

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: _smart_grep_python(
            root,
            query,
            mode=mode,
            profile=profile,
            include_glob=include_glob,
            is_regex=is_regex,
            case_sensitive=case_sensitive,
            max_results=max_results,
            context_lines=context_lines,
            workers=workers,
        ),
    )


def build_ripgrep_argv(
    rg_exe: str,
    root: Path,
    query: str,
    *,
    mode: str,
    profile: str,
    include_glob: list[str] | None,
    is_regex: bool,
    case_sensitive: bool,
    context_lines: int,
) -> list[str]:
    """Полная argv для `rg --json` (как в синхронном host_fs)."""
    cmd: list[str] = [
        rg_exe,
        "--json",
        "--hidden",
        "--glob",
        "!.git/*",
    ]
    if context_lines > 0:
        cmd.extend(["-C", str(context_lines)])
    if not case_sensitive:
        cmd.append("-i")
    if is_regex:
        cmd.extend(["-e", query])
    else:
        cmd.extend(["-F", "-e", query])

    globs = SMART_GREP_MODES.get(mode if mode in SMART_GREP_MODES else "code")
    if globs:
        for g in globs:
            cmd.extend(["--glob", g])

    if include_glob:
        for g in include_glob:
            if g.strip():
                cmd.extend(["--glob", g.strip()])

    cmd.append(str(root))
    return cmd


def hit_dict_from_rg_json_line(
    line: str,
    root: Path,
    profile: str,
    query: str,
) -> dict[str, Any] | None:
    """Одна строка stdout ripgrep --json → hit или None."""
    if not line.startswith("{"):
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if obj.get("type") != "match":
        return None
    data = obj.get("data") or {}
    path_obj = data.get("path") or {}
    ptext = path_obj.get("text") or ""
    if not ptext:
        return None
    raw_p = Path(ptext)
    abs_path = raw_p.resolve() if raw_p.is_absolute() else (root / raw_p).resolve()
    try:
        rel = abs_path.relative_to(root).as_posix()
    except ValueError:
        rel = ptext.replace("\\", "/")
    prof_ok = SMART_GREP_PROFILES.get(profile if profile in SMART_GREP_PROFILES else "all")
    if prof_ok and not _is_profile_match(rel, profile):
        return None
    line_no = int(data.get("line_number") or 0)
    lines_obj = data.get("lines") or {}
    line_text = (lines_obj.get("text") or "").splitlines()
    text0 = line_text[0] if line_text else ""
    subs = data.get("submatches") or []
    mtext = ""
    if subs:
        mtext = (subs[0].get("match") or {}).get("text") or ""
    if not mtext:
        mtext = query[:200]
    return {
        "file_id": None,
        "file_name": rel,
        "path": str(abs_path),
        "line": line_no,
        "line_text": text0[:400],
        "match": (mtext or "")[:200],
        "context_before": [],
        "context_after": [],
    }


async def _smart_grep_ripgrep(
    rg_exe: str,
    root: Path,
    query: str,
    *,
    mode: str,
    profile: str,
    include_glob: list[str] | None,
    is_regex: bool,
    case_sensitive: bool,
    max_results: int,
    context_lines: int,
    timeout_sec: int,
) -> dict[str, Any]:
    import asyncio

    cmd = build_ripgrep_argv(
        rg_exe,
        root,
        query,
        mode=mode,
        profile=profile,
        include_glob=include_glob,
        is_regex=is_regex,
        case_sensitive=case_sensitive,
        context_lines=context_lines,
    )

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(root),
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=float(timeout_sec))
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise TimeoutError(f"ripgrep timed out after {timeout_sec}s")

    # Windows: rg may exit 2 when hitting sockets/reparse points under secrets/ or Docker links.
    if proc.returncode not in (0, 1, 2):
        err = stderr_b.decode("utf-8", errors="replace")[:500]
        LOGGER.warning("rg failed rc=%s err=%s", proc.returncode, err)
        raise RuntimeError(f"ripgrep exited {proc.returncode}: {err or 'no stderr'}")
    if proc.returncode == 2:
        LOGGER.warning(
            "ripgrep exited 2 (unreadable paths); parsing matches from stdout if any"
        )

    hits: list[dict[str, Any]] = []
    truncated = False

    for line in stdout_b.decode("utf-8", errors="replace").splitlines():
        h = hit_dict_from_rg_json_line(line, root, profile, query)
        if h is None:
            continue
        hits.append(h)
        if len(hits) >= max_results:
            truncated = True
            break

    return {
        "status": "ok",
        "search_mode": "host_fs",
        "engine": "ripgrep",
        "host_path": str(root),
        "mode": mode,
        "profile": profile,
        "query": query,
        "is_regex": is_regex,
        "case_sensitive": case_sensitive,
        "total": len(hits),
        "truncated": truncated,
        "hits": hits,
    }
