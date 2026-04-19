# background_task_registry.py — in-memory результаты фоновых задач по session_id (досрочный timeout клиента).
from __future__ import annotations

import os
import threading
import time
import uuid
from typing import Any

# Один реестр на opaque session_id из cookie; изоляция между сессиями.
_MAX_TASKS_PER_SESSION = max(4, int(os.environ.get("BG_TASK_REGISTRY_MAX_PER_SESSION", "64")))
_TTL_SEC = max(60, int(os.environ.get("BG_TASK_REGISTRY_TTL_SEC", "86400")))
_MAX_SESSIONS = max(8, int(os.environ.get("BG_TASK_REGISTRY_MAX_SESSIONS", "4096")))


class BackgroundTaskRegistry:
    """Потокобезопасное хранилище: session_id → {task_id → запись}.

    Использование: эндпоинты создают task_id (pending), тяжёлая работа по завершении
    кладёт результат; клиент после обрыва соединения забирает GET/DELETE.
    """

    __slots__ = ("_lock", "_sessions")

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # session_id → { task_id → record }
        self._sessions: dict[str, dict[str, dict[str, Any]]] = {}

    def _touch_session_order(self, session_id: str) -> None:
        """LRU по числу сессий: при create/pop двигаем session_id в конец dict (Py3.7+ порядок)."""
        if session_id in self._sessions:
            bucket = self._sessions.pop(session_id)
            self._sessions[session_id] = bucket

    def _prune_session_tasks(self, bucket: dict[str, dict[str, Any]]) -> None:
        now = time.time()
        dead = [tid for tid, rec in bucket.items() if now - float(rec.get("updated_at", rec["created_at"])) > _TTL_SEC]
        for tid in dead:
            bucket.pop(tid, None)
        while len(bucket) > _MAX_TASKS_PER_SESSION:
            oldest = min(bucket.items(), key=lambda kv: float(kv[1].get("updated_at", kv[1]["created_at"])))[0]
            bucket.pop(oldest, None)

    def _prune_sessions(self) -> None:
        while len(self._sessions) > _MAX_SESSIONS:
            # удаляем самую старую сессию по первому task (best-effort)
            first_sid = next(iter(self._sessions))
            self._sessions.pop(first_sid, None)

    def create(self, session_id: str, kind: str, meta: dict[str, Any] | None = None) -> str:
        if not session_id:
            raise ValueError("session_id required")
        task_id = uuid.uuid4().hex
        now = time.time()
        rec = {
            "task_id": task_id,
            "kind": str(kind or "unknown"),
            "status": "pending",
            "meta": dict(meta) if isinstance(meta, dict) else {},
            "result": None,
            "error": None,
            "created_at": now,
            "updated_at": now,
        }
        with self._lock:
            self._prune_sessions()
            bucket = self._sessions.setdefault(session_id, {})
            self._prune_session_tasks(bucket)
            bucket[task_id] = rec
            self._touch_session_order(session_id)
        return task_id

    def set_result(self, session_id: str, task_id: str, result: dict[str, Any]) -> bool:
        now = time.time()
        with self._lock:
            bucket = self._sessions.get(session_id)
            if not bucket or task_id not in bucket:
                return False
            rec = bucket[task_id]
            rec["status"] = "ready"
            rec["result"] = dict(result)
            rec["error"] = None
            rec["updated_at"] = now
            self._touch_session_order(session_id)
        return True

    def set_error(self, session_id: str, task_id: str, message: str) -> bool:
        now = time.time()
        with self._lock:
            bucket = self._sessions.get(session_id)
            if not bucket or task_id not in bucket:
                return False
            rec = bucket[task_id]
            rec["status"] = "error"
            rec["error"] = (message or "")[:8000]
            rec["result"] = None
            rec["updated_at"] = now
            self._touch_session_order(session_id)
        return True

    def get(self, session_id: str, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            bucket = self._sessions.get(session_id)
            if not bucket:
                return None
            rec = bucket.get(task_id)
            if rec is None:
                return None
            self._prune_session_tasks(bucket)
            if task_id not in bucket:
                return None
            return dict(rec)

    def pop(self, session_id: str, task_id: str) -> dict[str, Any] | None:
        """Вернуть запись и удалить из реестра (извлечение с удалением)."""
        with self._lock:
            bucket = self._sessions.get(session_id)
            if not bucket:
                return None
            rec = bucket.pop(task_id, None)
            if not bucket:
                self._sessions.pop(session_id, None)
            return dict(rec) if rec else None

    def _meta_project_id(self, rec: dict[str, Any]) -> int | None:
        meta = rec.get("meta")
        if not isinstance(meta, dict):
            return None
        try:
            return int(meta.get("project_id"))
        except (TypeError, ValueError):
            return None

    def has_pending(self, session_id: str, kind: str, project_id: int) -> bool:
        """Есть ли в сессии pending-задача данного ``kind`` с ``meta.project_id`` = ``project_id``."""
        pid = int(project_id)
        k = str(kind or "")
        with self._lock:
            bucket = self._sessions.get(session_id)
            if not bucket:
                return False
            for rec in bucket.values():
                if rec.get("kind") != k or rec.get("status") != "pending":
                    continue
                if self._meta_project_id(rec) == pid:
                    return True
            return False

    def pop_ready_result(self, session_id: str, kind: str, project_id: int) -> dict[str, Any] | None:
        """Готовый ``result`` для ``kind`` и ``meta.project_id``; запись удаляется (try-retrieve)."""
        pid = int(project_id)
        k = str(kind or "")
        with self._lock:
            bucket = self._sessions.get(session_id)
            if not bucket:
                return None
            for tid, rec in list(bucket.items()):
                if rec.get("kind") != k or rec.get("status") != "ready":
                    continue
                if self._meta_project_id(rec) != pid:
                    continue
                res = rec.get("result")
                bucket.pop(tid, None)
                if not bucket:
                    self._sessions.pop(session_id, None)
                else:
                    self._touch_session_order(session_id)
                if isinstance(res, dict):
                    return dict(res)
                return None
            return None


_registry: BackgroundTaskRegistry | None = None
_registry_lock = threading.Lock()


def get_background_task_registry() -> BackgroundTaskRegistry:
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = BackgroundTaskRegistry()
        return _registry
