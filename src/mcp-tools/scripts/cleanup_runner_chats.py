#!/usr/bin/env python3
"""Удаление временных чатов, созданных раннерами (filewalk, phase1 и т.д.).

По умолчанию только dry-run. Удаление: --yes

Совпадение: description (после strip) начинается с одного из префиксов ИЛИ содержит
маркер cache-filewalk: / cache-phase1-test: (для имён вида batch…-cache-filewalk:…).

Только стандартная библиотека (urllib + CookieJar), без httpx.

Из контейнера задайте COLLOQUIUM_URL (например URL сервиса core в docker-сети), если localhost недоступен."""
from __future__ import annotations

import argparse
import json
import os
import sys
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener

_THIS = Path(__file__).resolve()
_MCP_TOOLS = _THIS.parents[1]
if str(_MCP_TOOLS) not in sys.path:
    sys.path.insert(0, str(_MCP_TOOLS))

import cqds_credentials as cq_cred


class _ColloquiumHttp:
    """Минимальный HTTP-клиент на urllib (cookies после login сохраняются в CookieJar)."""

    def __init__(self, base_url: str, username: str, password: str) -> None:
        self._base = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._jar = CookieJar()
        self._opener = build_opener(HTTPCookieProcessor(self._jar))
        self._logged_in = False

    def _ensure_login(self) -> None:
        if self._logged_in:
            return
        body = json.dumps(
            {"username": self._username, "password": self._password},
            ensure_ascii=False,
        ).encode("utf-8")
        req = Request(
            f"{self._base}/api/login",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener.open(req, timeout=30) as resp:
                if resp.getcode() != 200:
                    raise RuntimeError(f"Colloquium login failed: status={resp.getcode()}")
                resp.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Colloquium login failed: {exc.code} {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"Colloquium login: cannot connect to {self._base}: {exc}") from exc
        self._logged_in = True

    def list_chats(self) -> list[dict]:
        self._ensure_login()
        req = Request(f"{self._base}/api/chat/list")
        try:
            with self._opener.open(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"list_chats failed: {exc.code} {detail}") from exc
        return data if isinstance(data, list) else []

    def delete_chat(self, chat_id: int) -> dict:
        self._ensure_login()
        body = json.dumps({"chat_id": chat_id}).encode("utf-8")
        req = Request(
            f"{self._base}/api/chat/delete",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self._opener.open(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))


DEFAULT_PREFIXES = (
    "longrun-",
    "longrun2-",
    "longrun3-",
    "longrun4-",
    "fullrun-",
    "livestats-",
    "dbgramp-",
    "cache-filewalk:",
    "cache-phase1-test:",
    "cache-context-growth:",
    "stats-",
)


# Подстроки: ловят batch-имена вида batch20260404_120851-cache-filewalk:... (не только startswith).
_RUNNER_DESC_MARKERS = (
    "cache-filewalk:",
    "cache-phase1-test:",
    "cache-context-growth:",
    "cache-random-activity:",
    "cache-delta-safe:",
    "patch-accept-",
)


def _matches(desc: str, prefixes: tuple[str, ...]) -> bool:
    d = (desc or "").strip().lower()
    for p in prefixes:
        if d.startswith(p.lower()):
            return True
    for marker in _RUNNER_DESC_MARKERS:
        if marker in d:
            return True
    return False


def run(args: argparse.Namespace) -> dict[str, Any]:
    password, _ = cq_cred.resolve_password(args.password, args.password_file)
    if not password:
        raise RuntimeError("Password required.")

    prefixes = tuple(
        x.strip()
        for x in (args.prefix or [])
        if x.strip()
    )
    if not prefixes:
        prefixes = DEFAULT_PREFIXES

    client = _ColloquiumHttp(base_url=args.url, username=args.username, password=password)
    report: dict[str, Any] = {
        "prefixes": list(prefixes),
        "dry_run": not args.yes,
        "matched": [],
        "deleted": [],
        "errors": [],
    }
    chats = client.list_chats()
    for c in chats:
        if not isinstance(c, dict):
            continue
        cid = c.get("chat_id")
        desc = str(c.get("description", "") or "")
        if cid is None or not _matches(desc, prefixes):
            continue
        report["matched"].append({"chat_id": int(cid), "description": desc[:120]})
        if args.yes:
            try:
                client.delete_chat(int(cid))
                report["deleted"].append(int(cid))
            except Exception as exc:  # noqa: BLE001
                report["errors"].append({"chat_id": int(cid), "error": str(exc)})

    return report


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default=os.environ.get("COLLOQUIUM_URL", "http://localhost:8008"))
    ap.add_argument("--username", default=os.environ.get("COLLOQUIUM_USERNAME", "copilot"))
    ap.add_argument("--password", default=os.environ.get("COLLOQUIUM_PASSWORD", ""))
    ap.add_argument("--password-file", default=cq_cred.default_password_file_for_cli())
    ap.add_argument(
        "--prefix",
        action="append",
        default=[],
        help="Доп. префикс description (можно несколько). Встроенные: longrun-, cache-filewalk:, ...",
    )
    ap.add_argument(
        "--yes",
        action="store_true",
        help="Реально удалить чаты. Без флага — только список (dry-run).",
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    try:
        payload = run(args)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        n = len(payload["matched"])
        print(f"Найдено чатов по префиксам: {n}")
        for m in payload["matched"][:50]:
            print(f"  chat_id={m['chat_id']}  {m['description']!r}")
        if n > 50:
            print(f"  ... и ещё {n - 50}")
        if not args.yes:
            print("\nDry-run. Повторите с --yes для удаления.")
        else:
            print(f"\nУдалено: {len(payload['deleted'])}")
            if payload["errors"]:
                print("Ошибки:", payload["errors"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
