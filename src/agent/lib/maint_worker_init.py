# maint_worker_init.py — минимальный globals для подпроцессов maint-пула (без uvicorn/server_init).
from __future__ import annotations

import globals as g
from managers.db import Database
from managers.files import FileManager

_inited = False


def ensure_maint_worker_globals() -> None:
    """FileManager + project_registry; без replication/роутеров/вложенного maint child."""
    global _inited
    if _inited and g.file_manager is not None:
        return
    Database.get_database()
    if not isinstance(getattr(g, "project_registry", None), dict):
        g.project_registry = {}
    g.file_manager = FileManager(notify_heavy_ops=False)
    _inited = True
