# core_routes.py — общий статус ядра (uptime, maint, пул, фон).
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request

import globals as g
from lib.background_task_registry import get_background_task_registry
from lib.core_status_snapshot import build_core_status_payload

router = APIRouter()


def _session_id_or_401(request: Request) -> str:
    sid = request.cookies.get("session_id")
    if not sid:
        raise HTTPException(status_code=401, detail="No session")
    return str(sid)


@router.post("/core/background_tasks")
async def api_background_task_create(request: Request, body: dict[str, Any] = Body(...)):
    """Создать placeholder задачи (pending) в реестре текущей сессии."""
    g.check_session(request)
    sid = _session_id_or_401(request)
    kind = str(body.get("kind") or "").strip()
    if not kind:
        raise HTTPException(status_code=400, detail="kind required")
    raw_meta = body.get("meta")
    if raw_meta is not None and not isinstance(raw_meta, dict):
        raise HTTPException(status_code=400, detail="meta must be an object")
    meta = raw_meta if isinstance(raw_meta, dict) else None
    task_id = get_background_task_registry().create(sid, kind, meta)
    return {"ok": True, "task_id": task_id, "status": "pending"}


@router.get("/core/background_tasks/{task_id}")
async def api_background_task_get(request: Request, task_id: str):
    """Прочитать задачу по task_id (без удаления)."""
    g.check_session(request)
    sid = _session_id_or_401(request)
    rec = get_background_task_registry().get(sid, str(task_id).strip())
    if rec is None:
        raise HTTPException(status_code=404, detail="task not found")
    return {"ok": True, "task": rec}


@router.delete("/core/background_tasks/{task_id}")
async def api_background_task_consume(request: Request, task_id: str):
    """Извлечь задачу по task_id и удалить из реестра (после таймаута клиента и т.п.)."""
    g.check_session(request)
    sid = _session_id_or_401(request)
    rec = get_background_task_registry().pop(sid, str(task_id).strip())
    if rec is None:
        raise HTTPException(status_code=404, detail="task not found")
    return {"ok": True, "task": rec}


@router.get("/core/status")
async def api_core_status(request: Request):
    """Сводка процесса ядра (внутренний путь без префикса /api).

    Снаружи: GET /api/core/status — nginx (rewrite ^/api/(.*)$ /$1) отдаёт сюда /core/status.
    Прямой uvicorn без nginx: GET /core/status (как /project/status для project_router).
    """
    g.check_session(request)
    import server

    return build_core_status_payload(server.get_maint_child_state())
