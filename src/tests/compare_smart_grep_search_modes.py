#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сравнение smart_grep: project_refresh vs project_registered.

Ожидание: сначала выполняется запрос с search_mode=project_refresh (скан диска),
затем два запроса с одинаковыми query/mode/profile — второй с project_registered,
третий снова с project_refresh. Наборы попаданий (по file_name + line + тексту)
должны совпадать, если между запросами файлы на диске не менялись.

Авторизация (как cqds_mcp_full): если нет --session / --session-file / SMART_GREP_SESSION,
выполняется POST /api/login на базу из COLLOQUIUM_URL (по умолчанию http://localhost:8008),
пароль — COLLOQUIUM_PASSWORD, COLLOQUIUM_PASSWORD_FILE или mcp-tools/cqds_mcp_auth.secret
  (путь к секрету считается от каталога mcp-tools, где лежит cqds_credentials.py, не от cwd).

Примеры:
  python src/tests/compare_smart_grep_search_modes.py --project-id 2

  # без project_refresh (быстро, без полного scan на диске):
  python src/tests/compare_smart_grep_search_modes.py --project-id 2 --without-project-refresh

  python src/tests/compare_smart_grep_search_modes.py --project-id 2 --mcp-page-size 40

  # склейка POST smart_grep/chunk vs один ответ POST smart_grep (project_registered):
  python src/tests/compare_smart_grep_search_modes.py --project-id 2 --without-project-refresh --compare-chunks

  python src/tests/compare_smart_grep_search_modes.py \\
    --api-root http://127.0.0.1:8080/api \\
    --session-file secrets/session_id.txt \\
    --project-id 2 --query def_smart_grep --mode code --profile all

При несовпадении скрипт завершается с кодом 1 и печатает diff.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MCP_TOOLS = _REPO_ROOT / "mcp-tools"


def _ensure_mcp_tools_on_path() -> None:
    if str(_MCP_TOOLS) not in sys.path:
        sys.path.insert(0, str(_MCP_TOOLS))


def _default_api_root() -> str:
    if os.environ.get("SMART_GREP_API_ROOT"):
        return os.environ["SMART_GREP_API_ROOT"].rstrip("/")
    return os.environ.get("COLLOQUIUM_URL", "http://localhost:8008").rstrip("/") + "/api"


def _resolve_session_cookie(args: argparse.Namespace) -> tuple[str, str] | None:
    cookie = (args.session or "").strip()
    if getattr(args, "session_file", None):
        path = str(args.session_file).strip()
        if path and os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                cookie = (f.read() or "").strip()
    if cookie:
        return cookie, "session"

    if getattr(args, "no_auto_login", False):
        print(
            "Нужен session_id (--session, --session-file, SMART_GREP_SESSION) или уберите --no-auto-login",
            file=sys.stderr,
        )
        return None

    _ensure_mcp_tools_on_path()
    from cqds_credentials import (  # noqa: E402
        login_base_from_api_root,
        resolve_password,
        session_cookie_from_login,
    )

    login_base = login_base_from_api_root(args.api_root)
    username = (args.username or os.environ.get("COLLOQUIUM_USERNAME", "copilot") or "copilot").strip()
    pw_file = (getattr(args, "password_file", None) or "").strip() or None
    password, src = resolve_password(
        (args.password or "").strip() or None,
        pw_file,
    )
    timeout = float(getattr(args, "login_timeout", 30.0))
    sid = session_cookie_from_login(login_base, username, password, timeout=timeout)
    print(
        "Интеграционный логин: %s user=%s password_source=%s"
        % (login_base, username, src),
        file=sys.stderr,
    )
    return sid, "api/login"


def _post_json(url: str, cookie: str, body: dict[str, Any], timeout: int = 300) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Cookie": f"session_id={cookie}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {raw[:2000]}") from e
    return json.loads(raw)


def _http_json(
    url: str,
    cookie: str,
    *,
    method: str,
    body: dict[str, Any] | None,
    timeout: int = 120,
) -> tuple[int, dict[str, Any]]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "Cookie": f"session_id={cookie}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, json.loads(raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"detail": raw[:2000]}


def _normalize_hits(hits: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    """Устойчивое сравнение без file_id (на случай пересоздания записей)."""
    rows = []
    for h in hits:
        rows.append(
            (
                str(h.get("file_name") or ""),
                int(h.get("line") or 0),
                str(h.get("line_text") or ""),
                str(h.get("match") or ""),
            )
        )
    rows.sort()
    return rows


def _verify_mcp_paging(api_response: dict[str, Any], page_size: int, label: str) -> tuple[bool, str]:
    """Сверка: цепочка finalize + cq_fetch_result даёт те же нормализованные попадания, что и compress_smart_grep_hits(API)."""
    _ensure_mcp_tools_on_path()
    from cqds_result_pages import (  # noqa: E402
        ResultPageStore,
        compress_smart_grep_hits,
        reassemble_all_hits_from_paged_response,
    )

    raw_hits = api_response.get("hits") or []
    expected = _normalize_hits(compress_smart_grep_hits(raw_hits))

    async def _run() -> list[tuple[Any, ...]]:
        store = ResultPageStore(ttl_sec=120.0, max_handles=16)
        merged = await reassemble_all_hits_from_paged_response(
            dict(api_response), page_size=page_size, store=store
        )
        return _normalize_hits(merged)

    got = asyncio.run(_run())
    if got == expected:
        return True, ""
    return False, "%s: len expected=%d got=%d" % (label, len(expected), len(got))


def collect_hits_via_smart_grep_chunks(
    api_root: str,
    session_cookie: str,
    *,
    project_id: int,
    query: str,
    mode: str,
    profile: str,
    is_regex: bool,
    case_sensitive: bool,
    context_lines: int,
    max_hits: int,
    limit_files: int,
    path_prefix: str = "",
    search_mode_first: str = "project_registered",
    include_glob: list[str] | None = None,
    max_iterations: int = 50000,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Цепочка GET index_meta + POST smart_grep/chunk до scan_complete; при 409 — epoch и offset=0."""
    root = api_root.rstrip("/")
    url_meta = f"{root}/project/{project_id}/index_meta"
    url_chunk = f"{root}/project/smart_grep/chunk"

    st, meta = _http_json(url_meta, session_cookie, method="GET", body=None, timeout=60)
    if st != 200:
        raise RuntimeError(f"index_meta HTTP {st}: {meta}")
    epoch = int(meta.get("index_epoch", 0))

    all_hits: list[dict[str, Any]] = []
    offset = 0
    search_mode = search_mode_first
    truncated_any = False
    iterations = 0

    while iterations < max_iterations:
        iterations += 1
        payload: dict[str, Any] = {
            "project_id": project_id,
            "index_epoch": epoch,
            "path_prefix": path_prefix,
            "offset": offset,
            "limit_files": limit_files,
            "max_hits": max_hits,
            "query": query,
            "mode": mode,
            "profile": profile,
            "is_regex": is_regex,
            "case_sensitive": case_sensitive,
            "context_lines": context_lines,
            "search_mode": search_mode,
        }
        if include_glob:
            payload["include_glob"] = include_glob

        st, chunk = _http_json(url_chunk, session_cookie, method="POST", body=payload, timeout=300)
        if st == 409:
            detail = chunk.get("detail") if isinstance(chunk.get("detail"), dict) else {}
            epoch = int(detail.get("current_epoch", epoch))
            offset = 0
            search_mode = "project_registered"
            continue
        if st != 200:
            raise RuntimeError(f"smart_grep/chunk HTTP {st}: {chunk}")

        if chunk.get("truncated_by_max_hits"):
            truncated_any = True
        all_hits.extend(chunk.get("hits") or [])
        epoch = int(chunk.get("index_epoch", epoch))
        offset = int(chunk.get("next_offset", 0))
        search_mode = "project_registered"

        if chunk.get("scan_complete"):
            break

    meta_out = {
        "chunk_iterations": iterations,
        "truncated_by_max_hits_any": truncated_any,
        "final_index_epoch": epoch,
    }
    return all_hits, meta_out


def compare_chunk_stitch_to_sync(
    api_root: str,
    session_cookie: str,
    reference_hits: list[dict[str, Any]],
    *,
    project_id: int,
    query: str,
    mode: str,
    profile: str,
    is_regex: bool,
    case_sensitive: bool,
    context_lines: int,
    max_hits: int,
    limit_files: int,
    path_prefix: str,
    search_mode_first: str,
    include_glob: list[str] | None,
) -> tuple[bool, str, list[dict[str, Any]], dict[str, Any]]:
    stitched, stitch_meta = collect_hits_via_smart_grep_chunks(
        api_root,
        session_cookie,
        project_id=project_id,
        query=query,
        mode=mode,
        profile=profile,
        is_regex=is_regex,
        case_sensitive=case_sensitive,
        context_lines=context_lines,
        max_hits=max_hits,
        limit_files=limit_files,
        path_prefix=path_prefix,
        search_mode_first=search_mode_first,
        include_glob=include_glob,
    )
    exp = _normalize_hits(reference_hits)
    got = _normalize_hits(stitched)
    if exp == got:
        return (
            True,
            (
                "OK: склейка smart_grep/chunk (%d итераций) совпадает с синхронным ответом (%d попаданий)."
                % (stitch_meta["chunk_iterations"], len(exp))
            ),
            stitched,
            stitch_meta,
        )
    return (
        False,
        (
            "FAIL: склейка чанков vs sync: expected=%d got=%d (iter=%s truncated_any=%s)"
            % (len(exp), len(got), stitch_meta["chunk_iterations"], stitch_meta["truncated_by_max_hits_any"])
        ),
        stitched,
        stitch_meta,
    )


def run_chunk_vs_sync_only(
    api_root: str,
    session_cookie: str,
    project_id: int,
    query: str,
    mode: str,
    profile: str,
    is_regex: bool,
    case_sensitive: bool,
    max_results: int,
    context_lines: int,
    *,
    chunk_limit_files: int,
    chunk_search_mode_first: str,
) -> int:
    """Один синхронный smart_grep (registered) и сверка со склейкой чанков — без тройного сравнения режимов."""
    root = api_root.rstrip("/")
    url = f"{root}/project/smart_grep"
    base_payload = {
        "project_id": project_id,
        "query": query,
        "mode": mode,
        "profile": profile,
        "is_regex": is_regex,
        "case_sensitive": case_sensitive,
        "max_results": max_results,
        "context_lines": context_lines,
        "search_mode": "project_registered",
    }
    print("POST project/smart_grep (registered) — эталон...")
    r_sync = _post_json(url, session_cookie, base_payload)
    if r_sync.get("status") != "ok":
        print("Ошибка ответа sync:", r_sync)
        return 2
    ref = r_sync.get("hits") or []
    print("Склейка smart_grep/chunk...")
    c_ok, c_msg, stitched, _meta = compare_chunk_stitch_to_sync(
        api_root,
        session_cookie,
        ref,
        project_id=project_id,
        query=query,
        mode=mode,
        profile=profile,
        is_regex=is_regex,
        case_sensitive=case_sensitive,
        context_lines=context_lines,
        max_hits=max_results,
        limit_files=chunk_limit_files,
        path_prefix="",
        search_mode_first=chunk_search_mode_first,
        include_glob=None,
    )
    print(c_msg)
    if not c_ok:
        s_exp = set(_normalize_hits(ref))
        s_got = set(_normalize_hits(stitched))
        only_g = s_got - s_exp
        only_e = s_exp - s_got
        print("  только в чанках:", len(only_g), "только в sync:", len(only_e))
        for row in sorted(only_g)[:12]:
            print("    +", row)
        for row in sorted(only_e)[:12]:
            print("    -", row)
        return 1
    return 0


def run_compare(
    api_root: str,
    session_cookie: str,
    project_id: int,
    query: str,
    mode: str,
    profile: str,
    is_regex: bool,
    case_sensitive: bool,
    max_results: int,
    context_lines: int,
    mcp_page_size: int = 0,
    without_project_refresh: bool = False,
    compare_chunks: bool = False,
    chunk_limit_files: int = 50,
    chunk_search_mode_first: str = "project_registered",
) -> int:
    root = api_root.rstrip("/")
    url = f"{root}/project/smart_grep"

    base_payload = {
        "project_id": project_id,
        "query": query,
        "mode": mode,
        "profile": profile,
        "is_regex": is_regex,
        "case_sensitive": case_sensitive,
        "max_results": max_results,
        "context_lines": context_lines,
    }

    if without_project_refresh:
        print("Режим --without-project-refresh: три вызова project_registered (без scan диска).")
        print("POST project_registered (1/3)...")
        r_refresh1 = _post_json(url, session_cookie, {**base_payload, "search_mode": "project_registered"})
        print("POST project_registered (2/3)...")
        r_reg = _post_json(url, session_cookie, {**base_payload, "search_mode": "project_registered"})
        print("POST project_registered (3/3)...")
        r_refresh2 = _post_json(url, session_cookie, {**base_payload, "search_mode": "project_registered"})
    else:
        print("POST project_refresh (warm scan)...")
        r_refresh1 = _post_json(url, session_cookie, {**base_payload, "search_mode": "project_refresh"})
        if r_refresh1.get("status") != "ok":
            print("Ошибка ответа:", r_refresh1)
            return 2

        print("POST project_registered...")
        r_reg = _post_json(url, session_cookie, {**base_payload, "search_mode": "project_registered"})
        print("POST project_refresh (повтор)...")
        r_refresh2 = _post_json(url, session_cookie, {**base_payload, "search_mode": "project_refresh"})

    for name, r in ("r1", r_refresh1), ("r2", r_reg), ("r3", r_refresh2):
        if r.get("status") != "ok":
            print(f"Ошибка ответа ({name}):", r)
            return 2

    h1 = _normalize_hits(r_refresh1.get("hits") or [])
    h2 = _normalize_hits(r_reg.get("hits") or [])
    h3 = _normalize_hits(r_refresh2.get("hits") or [])

    print()
    if without_project_refresh:
        print("totals: reg1=%d reg2=%d reg3=%d" % (len(h1), len(h2), len(h3)))
    else:
        print("totals: refresh1=%d registered=%d refresh2=%d" % (len(h1), len(h2), len(h3)))
    print("truncated: r1=%s reg=%s r2=%s" % (
        r_refresh1.get("truncated"),
        r_reg.get("truncated"),
        r_refresh2.get("truncated"),
    ))

    ok = h1 == h2 == h3
    if ok:
        if without_project_refresh:
            print("OK: три вызова project_registered дали одинаковый набор попаданий.")
        else:
            print("OK: все три режима дали одинаковый набор попаданий.")
        if mcp_page_size > 0:
            for label, r in (
                ("first", r_refresh1),
                ("second", r_reg),
                ("third", r_refresh2),
            ):
                p_ok, p_msg = _verify_mcp_paging(r, mcp_page_size, label)
                if not p_ok:
                    print("FAIL: пейджинг MCP vs сырой ответ (%s): %s" % (label, p_msg))
                    return 1
            print(
                "OK: пейджинг извлечения (finalize + cq_fetch_result, page_size=%d) совпадает с compress(API)."
                % mcp_page_size
            )
        if compare_chunks:
            print()
            print("Сверка склейки чанков с ответом project_registered (середина тройки, r2)...")
            c_ok, c_msg, stitched, _st_meta = compare_chunk_stitch_to_sync(
                api_root,
                session_cookie,
                r_reg.get("hits") or [],
                project_id=project_id,
                query=query,
                mode=mode,
                profile=profile,
                is_regex=is_regex,
                case_sensitive=case_sensitive,
                context_lines=context_lines,
                max_hits=max_results,
                limit_files=chunk_limit_files,
                path_prefix="",
                search_mode_first=chunk_search_mode_first,
                include_glob=None,
            )
            print(c_msg)
            if not c_ok:
                s_exp = set(_normalize_hits(r_reg.get("hits") or []))
                s_got = set(_normalize_hits(stitched))
                only_g = s_got - s_exp
                only_e = s_exp - s_got
                print("  только в чанках:", len(only_g), "только в sync:", len(only_e))
                for row in sorted(only_g)[:12]:
                    print("    +", row)
                for row in sorted(only_e)[:12]:
                    print("    -", row)
                return 1
        return 0

    s1, s2, s3 = set(h1), set(h2), set(h3)
    print("FAIL: наборы различаются.")
    if s1 != s2:
        only_b = s2 - s1
        only_a = s1 - s2
        label_a, label_b = ("r1", "r2") if without_project_refresh else ("refresh1", "registered")
        print("  %s vs %s: только во втором:" % (label_a, label_b), len(only_b), "только в первом:", len(only_a))
        for row in sorted(only_b)[:15]:
            print("    +", row)
        for row in sorted(only_a)[:15]:
            print("    -", row)
    if s2 != s3:
        print("  r2 vs r3: diff size", len(s2 ^ s3))
    if s1 != s3:
        print("  r1 vs r3: diff size", len(s1 ^ s3))
    return 1


def main() -> int:
    p = argparse.ArgumentParser(description="Сравнить smart_grep project_refresh vs project_registered")
    p.add_argument(
        "--api-root",
        default=_default_api_root(),
        help="База API (.../api). По умолчанию COLLOQUIUM_URL + /api или SMART_GREP_API_ROOT",
    )
    p.add_argument(
        "--session",
        default=os.environ.get("SMART_GREP_SESSION", ""),
        help="Cookie session_id (или SMART_GREP_SESSION); если пусто — логин по паролю как у MCP",
    )
    p.add_argument(
        "--session-file",
        default=os.environ.get("SMART_GREP_SESSION_FILE", ""),
        help="Файл с одной строкой session_id",
    )
    p.add_argument(
        "--no-auto-login",
        action="store_true",
        help="Не вызывать /api/login; требовать явный session",
    )
    p.add_argument(
        "--username",
        default=os.environ.get("COLLOQUIUM_USERNAME", "copilot"),
        help="Логин Colloquium при авто-входе (default: copilot)",
    )
    p.add_argument(
        "--password",
        default=None,
        help="Пароль (лучше COLLOQUIUM_PASSWORD или файл)",
    )
    p.add_argument(
        "--password-file",
        default=None,
        help="Файл с паролем (иначе COLLOQUIUM_PASSWORD_FILE или cqds_mcp_auth.secret)",
    )
    p.add_argument(
        "--login-timeout",
        type=float,
        default=30.0,
        help="Таймаут POST /api/login (сек)",
    )
    p.add_argument("--project-id", type=int, required=True)
    p.add_argument("--query", default="smart_grep", help="Подстрока или regex")
    p.add_argument("--mode", default="code")
    p.add_argument("--profile", default="all")
    p.add_argument("--regex", action="store_true", help="query как regex")
    p.add_argument("--case-sensitive", action="store_true")
    p.add_argument("--max-results", type=int, default=500)
    p.add_argument("--context-lines", type=int, default=0)
    p.add_argument(
        "--mcp-page-size",
        type=int,
        default=0,
        help="Если >0: после совпадения режимов проверить восстановление hits через пейджинг MCP (как max_returned_items)",
    )
    p.add_argument(
        "--without-project-refresh",
        action="store_true",
        help="Не вызывать project_refresh (долгий scan); три раза project_registered — быстрый интеграционный прогон",
    )
    p.add_argument(
        "--compare-chunks",
        action="store_true",
        help="После успешной тройной сверки: склейка POST smart_grep/chunk vs ответ r2 (project_registered)",
    )
    p.add_argument(
        "--chunk-limit-files",
        type=int,
        default=50,
        help="Размер порции файлов для --compare-chunks (default: 50)",
    )
    p.add_argument(
        "--chunk-search-mode-first",
        choices=("project_registered", "project_refresh"),
        default="project_registered",
        help="Первый чанк: registered (default) или refresh (только offset=0)",
    )
    p.add_argument(
        "--chunk-vs-sync-only",
        action="store_true",
        help="Только сверка: один sync smart_grep (registered) против склейки чанков (без тройного сравнения режимов)",
    )
    args = p.parse_args()

    resolved = _resolve_session_cookie(args)
    if resolved is None:
        return 2
    cookie, _auth_src = resolved

    if args.chunk_vs_sync_only:
        return run_chunk_vs_sync_only(
            args.api_root,
            cookie,
            args.project_id,
            args.query,
            args.mode,
            args.profile,
            args.regex,
            args.case_sensitive,
            args.max_results,
            args.context_lines,
            chunk_limit_files=args.chunk_limit_files,
            chunk_search_mode_first=args.chunk_search_mode_first,
        )

    return run_compare(
        args.api_root,
        cookie,
        args.project_id,
        args.query,
        args.mode,
        args.profile,
        args.regex,
        args.case_sensitive,
        args.max_results,
        args.context_lines,
        mcp_page_size=args.mcp_page_size,
        without_project_refresh=args.without_project_refresh,
        compare_chunks=args.compare_chunks,
        chunk_limit_files=args.chunk_limit_files,
        chunk_search_mode_first=args.chunk_search_mode_first,
    )


if __name__ == "__main__":
    raise SystemExit(main())
