# API таблицы config (только admin).
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

import globals as g
from managers.config_store import ConfigStore, validate_config_key
from managers.runtime_config import invalidate_runtime_config_cache

router = APIRouter()
_store: Optional[ConfigStore] = None


def _store() -> ConfigStore:
    global _store
    if _store is None:
        _store = ConfigStore()
    return _store


def _require_admin(request: Request) -> int:
    user_id = g.check_session(request)
    if g.user_manager.get_user_role(user_id) != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return user_id


class ConfigCreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(..., min_length=1, max_length=256)
    value: Optional[str] = None
    fallback: Optional[str] = None


class ConfigPatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: Optional[str] = None
    fallback: Optional[str] = None


@router.get("/admin/config")
async def list_config(request: Request):
    _require_admin(request)
    return {"items": _store().list_entries()}


@router.get("/admin/config/{key:path}")
async def get_config(request: Request, key: str):
    _require_admin(request)
    try:
        validate_config_key(key)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    entry = _store().get_entry(key)
    if entry is None:
        raise HTTPException(status_code=404, detail="Unknown key")
    return entry


@router.post("/admin/config")
async def create_config(request: Request, body: ConfigCreateBody):
    _require_admin(request)
    try:
        validate_config_key(body.key)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    try:
        entry = _store().create(body.key, value=body.value, fallback=body.fallback)
    except KeyError:
        raise HTTPException(status_code=409, detail="Key already exists") from None
    invalidate_runtime_config_cache()
    return entry


@router.patch("/admin/config/{key:path}")
async def patch_config(request: Request, key: str, body: ConfigPatchBody):
    _require_admin(request)
    try:
        validate_config_key(key)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    patch = body.model_dump(exclude_unset=True)
    try:
        entry = _store().apply_patch(key, patch)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if entry is None:
        raise HTTPException(status_code=404, detail="Unknown key")
    invalidate_runtime_config_cache([key])
    return entry


@router.delete("/admin/config/{key:path}")
async def delete_config(request: Request, key: str):
    _require_admin(request)
    try:
        validate_config_key(key)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    ok = _store().delete(key)
    if not ok:
        raise HTTPException(status_code=404, detail="Unknown key")
    invalidate_runtime_config_cache([key])
    return {"ok": True}
