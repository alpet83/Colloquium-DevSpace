from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SecurityPolicy:
    allowed_roots: list[str]
    max_file_size_bytes: int
    max_payload_bytes: int
    max_changed_lines: int
    max_response_bytes: int
    allow_binary: bool
    allow_create: bool
    follow_symlinks: bool


def default_data_dir() -> Path:
    env = (os.environ.get("TEXT_EDITOR_DATA_DIR") or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return (Path.home() / ".mcp_text_editor").resolve()


def _default_policy(data_dir: Path) -> SecurityPolicy:
    roots = _default_allowed_roots_from_cwd_workspace()[0]
    return SecurityPolicy(
        allowed_roots=roots,
        max_file_size_bytes=2 * 1024 * 1024,
        max_payload_bytes=512 * 1024,
        max_changed_lines=500,
        max_response_bytes=512 * 1024,
        allow_binary=False,
        allow_create=True,
        follow_symlinks=False,
    )


def _default_allowed_roots_from_cwd_workspace() -> tuple[list[str], dict[str, Any]]:
    cwd = Path.cwd().resolve()
    candidates = sorted(cwd.glob("*.code-workspace"))
    for ws in candidates:
        roots = _workspace_roots(ws)
        if roots:
            return roots, {"source": "cwd_workspace", "workspace_file": str(ws.resolve())}
    return [str(cwd)], {"source": "cwd_fallback", "workspace_file": None}


def workspace_discovery_debug() -> dict[str, Any]:
    cwd = Path.cwd().resolve()
    candidates = sorted(cwd.glob("*.code-workspace"))
    env_keys = [
        "VSCODE_CWD",
        "VSCODE_PID",
        "VSCODE_IPC_HOOK_CLI",
        "PWD",
        "INIT_CWD",
        "WORKSPACE_FILE",
    ]
    env_hints: dict[str, str] = {}
    for key in env_keys:
        value = (os.environ.get(key) or "").strip()
        if value:
            env_hints[key] = value
    return {
        "cwd": str(cwd),
        "workspace_candidates": [str(p.resolve()) for p in candidates],
        "env_hints": env_hints,
    }

def _policy_to_json(policy: SecurityPolicy) -> dict[str, Any]:
    return {
        "allowed_roots": policy.allowed_roots,
        "max_file_size_bytes": policy.max_file_size_bytes,
        "max_payload_bytes": policy.max_payload_bytes,
        "max_changed_lines": policy.max_changed_lines,
        "max_response_bytes": policy.max_response_bytes,
        "allow_binary": policy.allow_binary,
        "allow_create": policy.allow_create,
        "follow_symlinks": policy.follow_symlinks,
    }


def _merge_roots(groups: list[list[str]]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for roots in groups:
        for root in roots:
            r = str(Path(root).expanduser().resolve())
            if r not in seen:
                seen.add(r)
                out.append(r)
    return out


def _read_policy_raw(data_dir: Path) -> dict[str, Any]:
    policy_path = data_dir / "security_policy.json"
    if not policy_path.exists():
        return {}
    try:
        raw = json.loads(policy_path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _active_workspaces_from_raw(raw: dict[str, Any]) -> list[str]:
    active = raw.get("_active_workspaces")
    out: list[str] = []
    seen: set[str] = set()
    if isinstance(active, list):
        for item in active:
            p = str(item or "").strip()
            if not p:
                continue
            rp = str(Path(p).expanduser().resolve())
            if rp not in seen:
                seen.add(rp)
                out.append(rp)
        return out
    # legacy migration: derive from _bindings keys.
    bindings = raw.get("_bindings")
    if isinstance(bindings, dict):
        for key in bindings.keys():
            k = str(key or "").strip()
            if not k or k == "__cwd__":
                continue
            rp = str(Path(k).expanduser().resolve())
            if rp not in seen:
                seen.add(rp)
                out.append(rp)
    return out


def _roots_from_active_workspaces(active_workspaces: list[str]) -> list[str]:
    groups: list[list[str]] = []
    for ws in active_workspaces:
        ws_path = Path(ws).expanduser().resolve()
        if not ws_path.exists():
            continue
        try:
            roots = _workspace_roots(ws_path)
        except Exception:
            continue
        if not roots:
            roots = [str(ws_path.parent.resolve())]
        groups.append(roots)
    return _merge_roots(groups)


def load_policy(data_dir: Path) -> SecurityPolicy:
    policy_path = data_dir / "security_policy.json"
    defaults = _default_policy(data_dir)
    if not policy_path.exists():
        roots, meta = _default_allowed_roots_from_cwd_workspace()
        defaults = SecurityPolicy(
            allowed_roots=roots,
            max_file_size_bytes=defaults.max_file_size_bytes,
            max_payload_bytes=defaults.max_payload_bytes,
            max_changed_lines=defaults.max_changed_lines,
            max_response_bytes=defaults.max_response_bytes,
            allow_binary=defaults.allow_binary,
            allow_create=defaults.allow_create,
            follow_symlinks=defaults.follow_symlinks,
        )
        data_dir.mkdir(parents=True, exist_ok=True)
        raw = _policy_to_json(defaults)
        ws_file = str(meta.get("workspace_file") or "").strip()
        raw["_active_workspaces"] = [ws_file] if ws_file else []
        if ws_file:
            raw["_bindings"] = {ws_file: list(roots)}
        else:
            raw["_bindings"] = {"__cwd__": list(roots)}
        raw["_meta"] = meta
        policy_path.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return defaults
    raw: dict[str, Any] = json.loads(policy_path.read_text(encoding="utf-8"))
    active_workspaces = _active_workspaces_from_raw(raw)
    roots_from_workspaces = _roots_from_active_workspaces(active_workspaces)
    # legacy fallback for old policies that only had _bindings.
    bindings = raw.get("_bindings")
    roots_from_bindings: list[str] = []
    if not roots_from_workspaces and isinstance(bindings, dict):
        groups: list[list[str]] = []
        for _k, v in bindings.items():
            if isinstance(v, list):
                groups.append([str(x) for x in v if str(x).strip()])
        roots_from_bindings = _merge_roots(groups)
    roots_raw = [str(Path(p).expanduser().resolve()) for p in raw.get("allowed_roots", defaults.allowed_roots)]
    roots = roots_from_workspaces or roots_from_bindings or roots_raw
    return SecurityPolicy(
        allowed_roots=roots,
        max_file_size_bytes=int(raw.get("max_file_size_bytes", defaults.max_file_size_bytes)),
        max_payload_bytes=int(raw.get("max_payload_bytes", defaults.max_payload_bytes)),
        max_changed_lines=int(raw.get("max_changed_lines", defaults.max_changed_lines)),
        max_response_bytes=int(raw.get("max_response_bytes", defaults.max_response_bytes)),
        allow_binary=bool(raw.get("allow_binary", defaults.allow_binary)),
        allow_create=bool(raw.get("allow_create", defaults.allow_create)),
        follow_symlinks=bool(raw.get("follow_symlinks", defaults.follow_symlinks)),
    )


def save_policy(
    data_dir: Path,
    policy: SecurityPolicy,
    *,
    meta: dict[str, Any] | None = None,
    bindings: dict[str, list[str]] | None = None,
    active_workspaces: list[str] | None = None,
) -> None:
    policy_path = data_dir / "security_policy.json"
    data_dir.mkdir(parents=True, exist_ok=True)
    raw = _policy_to_json(policy)
    if meta:
        raw["_meta"] = meta
    if bindings is not None:
        raw["_bindings"] = bindings
    if active_workspaces is not None:
        raw["_active_workspaces"] = active_workspaces
    policy_path.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def assign_workspace_allowed_roots(data_dir: Path, workspace_file: Path) -> SecurityPolicy:
    path = workspace_file.expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(str(path))
    prev = load_policy(data_dir)
    raw = _read_policy_raw(data_dir)
    active_workspaces = _active_workspaces_from_raw(raw)
    path_s = str(path)
    if path_s not in active_workspaces:
        active_workspaces.append(path_s)
    merged_roots = _roots_from_active_workspaces(active_workspaces)
    # compatibility snapshot for older tooling expecting bindings map.
    bindings: dict[str, list[str]] = {}
    for ws in active_workspaces:
        r = _roots_from_active_workspaces([ws])
        if r:
            bindings[ws] = r
    updated = SecurityPolicy(
        allowed_roots=merged_roots,
        max_file_size_bytes=prev.max_file_size_bytes,
        max_payload_bytes=prev.max_payload_bytes,
        max_changed_lines=prev.max_changed_lines,
        max_response_bytes=prev.max_response_bytes,
        allow_binary=prev.allow_binary,
        allow_create=prev.allow_create,
        follow_symlinks=prev.follow_symlinks,
    )
    save_policy(
        data_dir,
        updated,
        meta={
            "source": "multi_workspace" if len(bindings) > 1 else "assigned_workspace",
            "workspace_file": str(path),
            "bindings_count": len(active_workspaces),
        },
        bindings=bindings,
        active_workspaces=active_workspaces,
    )
    return updated


def _workspace_roots(workspace_file: Path) -> list[str]:
    raw: dict[str, Any] = json.loads(workspace_file.read_text(encoding="utf-8"))
    folders = raw.get("folders", [])
    roots: list[str] = []
    if isinstance(folders, list):
        for item in folders:
            if isinstance(item, dict):
                rel = str(item.get("path") or "").strip()
                if not rel:
                    continue
                roots.append(str((workspace_file.parent / rel).resolve()))
            elif isinstance(item, str) and item.strip():
                roots.append(str((workspace_file.parent / item.strip()).resolve()))
    return roots


def policy_meta(data_dir: Path) -> dict[str, Any]:
    raw = _read_policy_raw(data_dir)
    meta = raw.get("_meta")
    if isinstance(meta, dict):
        return meta
    # Legacy policy compatibility: old files had no _meta.
    bindings = raw.get("_bindings")
    if isinstance(bindings, dict) and bindings:
        return {"source": "legacy_bindings", "workspace_file": ""}
    if raw:
        return {"source": "legacy_policy", "workspace_file": ""}
    return {}


def policy_bindings(data_dir: Path) -> dict[str, list[str]]:
    raw = _read_policy_raw(data_dir)
    out: dict[str, list[str]] = {}
    active_workspaces = _active_workspaces_from_raw(raw)
    for ws in active_workspaces:
        roots = _roots_from_active_workspaces([ws])
        if roots:
            out[ws] = roots
    if out:
        return out
    # legacy fallback
    bindings_raw = raw.get("_bindings")
    if not isinstance(bindings_raw, dict):
        return out
    for k, v in bindings_raw.items():
        if isinstance(v, list):
            out[str(k)] = [str(x) for x in v if str(x).strip()]
    return out


def policy_active_workspaces(data_dir: Path) -> list[str]:
    raw = _read_policy_raw(data_dir)
    return _active_workspaces_from_raw(raw)

