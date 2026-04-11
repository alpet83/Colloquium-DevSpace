# colloquium_httpx_client.py — HTTP API Colloquium без cqds_helpers / пакета mcp (venv ядра).
from __future__ import annotations

import base64
import json
import re
from typing import Any

import httpx  # type: ignore[import]


class ColloquiumHttpxClient:
    """Подмножество ColloquiumClient для скриптов в /app/venv без MCP."""

    def __init__(self, base_url: str, username: str, password: str) -> None:
        self._base = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._client = httpx.AsyncClient(
            base_url=self._base,
            follow_redirects=True,
            timeout=30.0,
        )
        self._logged_in = False

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _ensure_login(self) -> None:
        if self._logged_in:
            return
        resp = await self._client.post(
            "/api/login",
            json={"username": self._username, "password": self._password},
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Colloquium login failed: {resp.status_code} {resp.text}")
        self._logged_in = True

    async def select_project(self, project_id: int) -> dict:
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
        request_timeout: float | None = None,
    ) -> list[dict]:
        await self._ensure_login()
        params: dict[str, Any] = {"project_id": project_id}
        if modified_since is not None:
            params["modified_since"] = modified_since
        if file_ids is not None:
            params["file_ids"] = ",".join(str(i) for i in file_ids)
        if include_size:
            params["include_size"] = 1
        req_timeout = (
            httpx.Timeout(float(request_timeout) + 15.0)
            if request_timeout is not None
            else None
        )
        resp = await self._client.get(
            "/api/project/file_index",
            params=params,
            timeout=req_timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []

    async def read_file(self, file_id: int) -> str:
        await self._ensure_login()
        resp = await self._client.get(
            "/api/chat/file_contents",
            params={"file_id": file_id},
        )
        resp.raise_for_status()
        ct = resp.headers.get("content-type", "")
        if "application/json" in ct:
            try:
                return json.dumps(resp.json(), ensure_ascii=False, indent=2)
            except json.JSONDecodeError:
                return resp.text
        return resp.text

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
        await self._ensure_login()
        resp = await self._client.get(
            "/api/chat/get",
            params={"chat_id": chat_id, "wait_changes": 1 if wait else 0},
            timeout=timeout + 5.0,
        )
        resp.raise_for_status()
        return resp.json()

    async def exec_command(self, project_id: int, command: str, timeout: int = 30) -> dict:
        await self._ensure_login()
        resp = await self._client.post(
            "/api/project/exec",
            json={"project_id": project_id, "command": command, "timeout": timeout},
            timeout=httpx.Timeout(timeout + 15.0),
        )
        resp.raise_for_status()
        return resp.json()

    async def query_db(
        self,
        project_id: int,
        query: str,
        *,
        allow_write: bool = False,
        timeout: int = 30,
    ) -> dict:
        q = str(query or "").strip()
        if not q:
            raise ValueError("query must be non-empty")
        ql = q.lower().lstrip()
        if not allow_write:
            if not (ql.startswith("select") or ql.startswith("with") or ql.startswith("explain")):
                raise ValueError("Only read-only SQL is allowed")
            if re.search(
                r"\b(insert|update|delete|drop|alter|truncate|create|grant|revoke|comment)\b",
                ql,
            ):
                raise ValueError("Mutating SQL keywords are not allowed")
        encoded = base64.b64encode(q.encode("utf-8")).decode("ascii")
        command = (
            "PYTHONPATH=/app/agent /app/venv/bin/python - <<'PY'\n"
            "import base64, json\n"
            "from managers.db import Database\n"
            f"q = base64.b64decode('{encoded}').decode('utf-8')\n"
            "db = Database.get_database()\n"
            "rows = db.fetch_all(q)\n"
            "print(json.dumps({'status': 'success', 'rows': [list(r) for r in rows]}, ensure_ascii=False))\n"
            "PY"
        )
        exec_timeout = min(max(int(timeout), 1), 300)
        return await self.exec_command(project_id, command, exec_timeout)
