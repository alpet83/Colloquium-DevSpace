# cqds_credentials.py — пароль и сессия для HTTP API (MCP, интеграционные тесты)
# Секрет по умолчанию: mcp-tools/cqds_mcp_auth.secret (путь от __file__, не от cwd).
# Шаблон для копирования в репозитории: cqds_mcp_auth.sample.secret
# Устаревшее имя sidecar: copilot_mcp_tool.secret — подхватывается, если нового файла нет.
from __future__ import annotations

import json
import os
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_MCP_TOOLS_DIR = Path(__file__).resolve().parent

_SIDECAR_CANDIDATES: tuple[tuple[Path, str], ...] = (
    (_MCP_TOOLS_DIR / "cqds_mcp_auth.secret", "cqds_mcp_auth.secret"),
    (_MCP_TOOLS_DIR / "copilot_mcp_tool.secret", "copilot_mcp_tool.secret"),
)


def _existing_sidecar() -> tuple[Path, str] | None:
    for path, label in _SIDECAR_CANDIDATES:
        if path.is_file():
            return path, label
    return None


def default_password_file_for_cli() -> str:
    """Путь для argparse ``--password-file`` в HTTP-раннерах (как ``run_filewalk_batch.ps1``).

    Приоритет: ``COLLOQUIUM_PASSWORD_FILE`` (env), иначе sidecar
    (``cqds_mcp_auth.secret``, затем устаревший ``copilot_mcp_tool.secret``), если есть;
    иначе пустая строка (тогда ``resolve_password`` снова проверит sidecar).
    """
    env = (os.environ.get("COLLOQUIUM_PASSWORD_FILE") or "").strip()
    if env:
        return env
    pair = _existing_sidecar()
    if pair:
        return str(pair[0])
    return ""


def read_password_file(password_file: str) -> str:
    try:
        with open(password_file, "r", encoding="utf-8") as handle:
            password = handle.read().strip()
    except OSError as exc:
        raise RuntimeError(f"Failed to read password file '{password_file}': {exc}") from exc
    if not password:
        raise RuntimeError(f"Password file '{password_file}' is empty")
    return password


def resolve_password(cli_password: str | None, cli_password_file: str | None) -> tuple[str, str]:
    """CLI → COLLOQUIUM_PASSWORD → COLLOQUIUM_PASSWORD_FILE → sidecar → devspace.

    Для argparse см. :func:`default_password_file_for_cli` (явный путь к sidecar в раннерах).
    """
    if cli_password:
        return cli_password, "--password"
    if cli_password_file:
        return read_password_file(cli_password_file), "--password-file"

    env_password = os.environ.get("COLLOQUIUM_PASSWORD")
    if env_password:
        return env_password.strip(), "COLLOQUIUM_PASSWORD"

    env_password_file = os.environ.get("COLLOQUIUM_PASSWORD_FILE")
    if env_password_file:
        return read_password_file(env_password_file), "COLLOQUIUM_PASSWORD_FILE"

    pair = _existing_sidecar()
    if pair:
        return read_password_file(str(pair[0])), pair[1]

    return "devspace", "default"


def _iter_set_cookie_headers(headers: Any) -> list[str]:
    out: list[str] = []
    if hasattr(headers, "get_all"):
        out.extend(headers.get_all("Set-Cookie") or [])
    else:
        v = headers.get("Set-Cookie")
        if v:
            out.append(v)
    return out


def session_cookie_from_login(
    base_url: str,
    username: str,
    password: str,
    *,
    timeout: float = 30.0,
) -> str:
    """POST /api/login → cookie session_id (stdlib)."""
    login_url = base_url.rstrip("/") + "/api/login"
    body = json.dumps({"username": username, "password": password}).encode("utf-8")
    req = Request(
        login_url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Login HTTP {resp.status}")
            jar = SimpleCookie()
            for item in _iter_set_cookie_headers(resp.headers):
                jar.load(item)
            if "session_id" not in jar:
                raise RuntimeError("Login OK but Set-Cookie has no session_id")
            return jar["session_id"].value
    except HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:800]
        raise RuntimeError(f"Login failed HTTP {e.code}: {detail}") from e
    except URLError as e:
        raise RuntimeError(f"Login request failed: {e}") from e


def api_root_from_base(base_url: str) -> str:
    return base_url.rstrip("/") + "/api"


def login_base_from_api_root(api_root: str) -> str:
    """http://host:8008/api → http://host:8008"""
    r = api_root.rstrip("/")
    if r.endswith("/api"):
        return r[:-4] or r
    return r
