# cqds_client.py — Global MCP state, URL resolution, and ColloquiumClient
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx  # type: ignore[import]

from cqds_helpers import LOGGER

# ---------------------------------------------------------------------------
# Auth token resolution (MCP_AUTH_TOKEN)
# ---------------------------------------------------------------------------

def _read_mcp_json_token() -> str | None:
    """Walk up from cwd/script dir for .vscode/mcp.json and .cursor/mcp.json."""
    script_path = str(Path(__file__).resolve())
    seen: set[Path] = set()
    candidates: list[Path] = []
    for base in (Path.cwd(), Path(__file__).resolve().parent):
        for parent in (base, *base.parents):
            for rel in (".vscode/mcp.json", ".cursor/mcp.json"):
                p = parent / rel
                if p not in seen:
                    seen.add(p)
                    candidates.append(p)
    for path in candidates:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        token = (data.get("env") or {}).get("MCP_AUTH_TOKEN")
        if token:
            return str(token)
        for key in ("servers", "mcpServers"):
            block = data.get(key) or {}
            if not isinstance(block, dict):
                continue
            for server_name, server_cfg in block.items():
                if not isinstance(server_cfg, dict):
                    continue
                args = server_cfg.get("args") or []
                if not (
                    any(script_path in str(a) for a in args)
                    or server_name in ("cqds", "colloquium")
                ):
                    continue
                token = (server_cfg.get("env") or {}).get("MCP_AUTH_TOKEN")
                if token:
                    return str(token)
    return None


def _resolve_mcp_auth_token() -> str:
    """Resolve MCP_AUTH_TOKEN: env var → mcp.json → built-in default."""
    token = os.environ.get("MCP_AUTH_TOKEN") or _read_mcp_json_token()
    return token or "Grok-xAI-Agent-The-Best"


# ---------------------------------------------------------------------------
# Module-level shared state
# ---------------------------------------------------------------------------

# Resolved once at startup; used by process-management and docker-control handlers.
_MCP_AUTH_TOKEN: str = _resolve_mcp_auth_token()
_DEFAULT_MCP_SERVER_URL: str = os.environ.get("MCP_SERVER_URL", "http://localhost:8084").rstrip("/")
_PROJECT_MCP_URL_CACHE: dict[int, str] = {}
_PROCESS_GUID_TO_MCP_URL: dict[str, str] = {}
# Active project set by cq_select_project — used as implicit routing default.
_ACTIVE_PROJECT_ID: int | None = None
# Optional host-rewrite for Docker-internal URLs when copilot_mcp_tool runs on the host.
# Set MCP_HOST_REMAP="nginx-router=localhost" in the MCP server env (e.g. mcp.json 'env').
_MCP_HOST_REMAP: dict[str, str] = {}
for _mcp_remap_pair in os.environ.get("MCP_HOST_REMAP", "").split(","):
    if "=" in _mcp_remap_pair:
        _k, _v = _mcp_remap_pair.split("=", 1)
        _MCP_HOST_REMAP[_k.strip()] = _v.strip()


def set_active_project_id(pid: int | None) -> None:
    global _ACTIVE_PROJECT_ID
    _ACTIVE_PROJECT_ID = pid


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def _normalize_mcp_server_url(url: Any) -> str | None:
    if url is None:
        return None
    val = str(url).strip()
    if not val:
        return None
    if not (val.startswith("http://") or val.startswith("https://")):
        val = "http://" + val
    return val.rstrip("/")


def _apply_mcp_host_remap(url: str | None) -> str | None:
    """Rewrite Docker-internal hostnames to host-accessible ones via MCP_HOST_REMAP."""
    if not url or not _MCP_HOST_REMAP:
        return url
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in _MCP_HOST_REMAP:
        return url
    new_host = _MCP_HOST_REMAP[host]
    port_part = f":{parsed.port}" if parsed.port else ""
    return parsed._replace(netloc=f"{new_host}{port_part}").geturl()


def _cache_project_mcp_urls(projects: list[dict[str, Any]]) -> None:
    for project in projects:
        try:
            pid = int(project.get("id"))
        except Exception:
            continue
        norm = _normalize_mcp_server_url(project.get("mcp_server_url"))
        _PROJECT_MCP_URL_CACHE[pid] = norm or _DEFAULT_MCP_SERVER_URL


async def _resolve_project_mcp_server_url(
    client: "ColloquiumClient", project_id: int | None
) -> str:
    """Return the MCP server URL for project_id, applying MCP_HOST_REMAP."""
    effective_id = project_id if project_id is not None else _ACTIVE_PROJECT_ID
    if effective_id is None:
        return _apply_mcp_host_remap(_DEFAULT_MCP_SERVER_URL) or _DEFAULT_MCP_SERVER_URL
    cached = _PROJECT_MCP_URL_CACHE.get(effective_id)
    if cached:
        return _apply_mcp_host_remap(cached) or cached
    projects = await client.list_projects()
    _cache_project_mcp_urls(projects)
    url = _PROJECT_MCP_URL_CACHE.get(effective_id, _DEFAULT_MCP_SERVER_URL)
    return _apply_mcp_host_remap(url) or url


# ---------------------------------------------------------------------------
# Colloquium HTTP client
# ---------------------------------------------------------------------------

class ColloquiumClient:
    """Async HTTP client for Colloquium-DevSpace."""

    def __init__(self, base_url: str, username: str, password: str) -> None:
        self._base = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._client = httpx.AsyncClient(
            base_url=self._base,
            follow_redirects=True,
            timeout=30.0,
            event_hooks={
                "request": [self._log_request],
                "response": [self._log_response],
            },
        )
        self._logged_in = False
        self._sync_timeout: int = 0

    def is_local_or_private_endpoint(self) -> bool:
        """Allow elevated DB actions only for local/private Colloquium endpoints."""
        try:
            host = (urlparse(self._base).hostname or "").strip().lower()
        except Exception:
            return False

        if not host:
            return False

        if host in {"localhost", "127.0.0.1", "::1", "host.docker.internal"}:
            return True

        try:
            ip = ipaddress.ip_address(host)
            return ip.is_loopback or ip.is_private
        except ValueError:
            return host.endswith(".local") or host.endswith(".lan")

    async def _log_request(self, request: httpx.Request) -> None:
        LOGGER.info("HTTP -> %s %s", request.method, request.url)

    async def _log_response(self, response: httpx.Response) -> None:
        request = response.request
        LOGGER.info("HTTP <- %s %s status=%s", request.method, request.url, response.status_code)

    async def _ensure_login(self) -> None:
        if self._logged_in:
            return
        resp = await self._client.post(
            "/api/login",
            json={"username": self._username, "password": self._password},
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Colloquium login failed: {resp.status_code} {resp.text}"
            )
        self._logged_in = True

    async def list_chats(self) -> list[dict]:
        await self._ensure_login()
        resp = await self._client.get("/api/chat/list")
        resp.raise_for_status()
        return resp.json()

    async def create_chat(self, description: str = "MCP Session") -> int:
        await self._ensure_login()
        resp = await self._client.post(
            "/api/chat/create", json={"description": description}
        )
        resp.raise_for_status()
        return resp.json()["chat_id"]

    async def post_message(self, chat_id: int, message: str) -> dict:
        await self._ensure_login()
        resp = await self._client.post(
            "/api/chat/post", json={"chat_id": chat_id, "message": message}
        )
        resp.raise_for_status()
        return resp.json()

    async def get_reply(self, chat_id: int, wait: bool = True, timeout: float = 15.0) -> dict:
        """Poll for chat changes. Returns the raw API response."""
        await self._ensure_login()
        resp = await self._client.get(
            "/api/chat/get",
            params={"chat_id": chat_id, "wait_changes": 1 if wait else 0},
            timeout=timeout + 5.0,
        )
        resp.raise_for_status()
        return resp.json()

    async def get_history(self, chat_id: int) -> dict:
        """Fetch current chat history snapshot (no waiting)."""
        await self._ensure_login()
        resp = await self._client.get(
            "/api/chat/get",
            params={"chat_id": chat_id, "wait_changes": 0},
            timeout=15.0,
        )
        resp.raise_for_status()
        return resp.json()

    async def get_chat_stats(self, chat_id: int, since_seconds: int | None = None) -> dict:
        """Fetch aggregated usage stats for a chat via /api/chat/stats (with legacy fallback)."""
        await self._ensure_login()
        params: dict[str, Any] = {"chat_id": chat_id}
        if since_seconds is not None and since_seconds > 0:
            params["since_seconds"] = since_seconds
        resp = await self._client.get("/api/chat/stats", params=params, timeout=30.0)
        if resp.status_code == 404:
            resp = await self._client.get("/api/chat/get_stats", params=params, timeout=30.0)
        resp.raise_for_status()
        return resp.json()

    async def list_projects(self) -> list[dict]:
        await self._ensure_login()
        resp = await self._client.get("/api/project/list")
        resp.raise_for_status()
        return resp.json()

    async def select_project(self, project_id: int) -> dict:
        """Set the active project context for the current authenticated session."""
        await self._ensure_login()
        resp = await self._client.post(
            "/api/project/select",
            json={"project_id": project_id},
        )
        resp.raise_for_status()
        return resp.json()

    async def list_files(
        self,
        project_id: int,
        modified_since: int | None = None,
        file_ids: list[int] | None = None,
        include_size: bool = False,
    ) -> list[dict]:
        """Return file index (no content) via /api/project/file_index."""
        await self._ensure_login()
        params: dict[str, Any] = {"project_id": project_id}
        if modified_since is not None:
            params["modified_since"] = modified_since
        if file_ids is not None:
            params["file_ids"] = ",".join(str(i) for i in file_ids)
        if include_size:
            params["include_size"] = 1
        resp = await self._client.get("/api/project/file_index", params=params)
        resp.raise_for_status()
        return resp.json()

    async def get_index(self, chat_id: int | None = None, project_id: int | None = None) -> dict:
        """Return the rich entity index for a chat or cached project index."""
        await self._ensure_login()
        params: dict[str, Any] = {}
        if chat_id is not None:
            params["chat_id"] = chat_id
        if project_id is not None:
            params["project_id"] = project_id
        resp = await self._client.get("/api/chat/index", params=params)
        resp.raise_for_status()
        return resp.json()

    async def get_project_status(self, project_id: int) -> dict:
        """Fetch health status and diagnostics for a project."""
        await self._ensure_login()
        resp = await self._client.get(
            "/api/project/status",
            params={"project_id": project_id},
        )
        resp.raise_for_status()
        return resp.json()

    async def get_code_index(self, project_id: int, timeout: int = 300) -> dict:
        """Build and return the rich entity index for a project on demand."""
        await self._ensure_login()
        http_timeout = httpx.Timeout(float(max(timeout, 30) + 30))
        resp = await self._client.get(
            "/api/project/code_index",
            params={"project_id": project_id, "timeout": timeout},
            timeout=http_timeout,
        )
        resp.raise_for_status()
        return resp.json()

    async def read_file(self, file_id: int) -> str:
        """Fetch raw file contents by DB file_id."""
        await self._ensure_login()
        resp = await self._client.get(
            "/api/chat/file_contents",
            params={"file_id": file_id},
        )
        resp.raise_for_status()
        ct = resp.headers.get("content-type", "")
        if "application/json" in ct:
            return json.dumps(resp.json(), ensure_ascii=False, indent=2)
        return resp.text

    async def exec_command(self, project_id: int, command: str, timeout: int = 30) -> dict:
        """Execute a shell command in a project sandbox via /api/project/exec."""
        await self._ensure_login()
        resp = await self._client.post(
            "/api/project/exec",
            json={"project_id": project_id, "command": command, "timeout": timeout},
            timeout=httpx.Timeout(timeout + 15.0),
        )
        resp.raise_for_status()
        return resp.json()

    async def smart_grep(
        self,
        project_id: int,
        query: str,
        mode: str = "code",
        profile: str = "all",
        time_strict: str | None = None,
        is_regex: bool = False,
        case_sensitive: bool = False,
        max_results: int = 100,
        context_lines: int = 0,
        include_glob: list[str] | None = None,
    ) -> dict:
        """Search occurrences in project file sets via /api/project/smart_grep."""
        await self._ensure_login()
        payload: dict[str, Any] = {
            "project_id": project_id,
            "query": query,
            "mode": mode,
            "profile": profile,
            "is_regex": is_regex,
            "case_sensitive": case_sensitive,
            "max_results": max_results,
            "context_lines": context_lines,
        }
        if time_strict:
            payload["time_strict"] = time_strict
        if include_glob:
            payload["include_glob"] = include_glob
        resp = await self._client.post("/api/project/smart_grep", json=payload)
        resp.raise_for_status()
        return resp.json()

    async def replace_file(
        self,
        project_id: int,
        file_id: int,
        old: str,
        new: str,
        is_regex: bool = False,
        case_sensitive: bool = True,
        max_replacements: int = 0,
    ) -> dict:
        """Replace text in one file via /api/project/replace."""
        await self._ensure_login()
        resp = await self._client.post(
            "/api/project/replace",
            json={
                "project_id": project_id,
                "file_id": file_id,
                "old": old,
                "new": new,
                "is_regex": is_regex,
                "case_sensitive": case_sensitive,
                "max_replacements": max_replacements,
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def aclose(self) -> None:
        await self._client.aclose()
