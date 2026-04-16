from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from .basic_logger import make_logger
from .config import (
    SecurityPolicy,
    assign_workspace_allowed_roots,
    load_policy,
    policy_active_workspaces,
    policy_bindings,
    policy_meta,
)
from .constants import (
    DEFAULT_MAX_VIEW_LINES,
    DEFAULT_RECENT_OPS_LIMIT,
    DEFAULT_WRAP_WIDTH,
    HARD_MAX_VIEW_LINES,
    LINT_SUCCESS,
    MAX_NUMBERED_LINES,
    MAX_RECENT_OPS_LIMIT,
    MAX_WRAPPED_SEGMENTS_PER_LINE,
    RESPONSE_MODES,
    SAVED_TO_DISK,
)
from .errors import EditorError, bad_request
from .profiles import Profile, ProfileRegistry, run_formatter, run_syntax_check
from .storage import Storage
from .telemetry import TelemetryStore


def _wrap_text(value: str, width: int) -> str | list[str]:
    if width <= 0 or len(value) <= width:
        return value
    chunks = [value[i : i + width] for i in range(0, len(value), width)]
    return chunks[:MAX_WRAPPED_SEGMENTS_PER_LINE]


class EditorService:
    SESSION_OPS = [
        "get_view",
        "move_cursor",
        "replace_range",
        "replace_regex",
        "apply_patch",
        "search_indexed",
        "diagnostics",
        "format_range",
        "undo",
        "redo",
        "save_revision",
    ]
    ADMIN_OPS = ["op_help", "help_selftest", "assign_workspace", "policy_show", "telemetry_report", "cleanup_stale_sessions"]

    def __init__(self, storage: Storage, policy: SecurityPolicy):
        self.storage = storage
        self.policy = policy
        profiles_dir = Path(
            os.environ.get("TEXT_EDITOR_PROFILES_DIR") or (Path(__file__).resolve().parent / "profiles")
        )
        self.profiles = ProfileRegistry(profiles_dir)
        self.telemetry = TelemetryStore(self.storage.data_dir)
        self.log = make_logger(self.storage.data_dir, "text_editor_service")
        self._last_success_cmd_by_session: dict[str, dict[str, Any]] = {}
        self._success_cmd_by_id: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _command_id_for(request: dict[str, Any]) -> str:
        blob = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return "cmd_" + hashlib.md5(blob.encode("utf-8")).hexdigest()[:16]

    @classmethod
    def _op_catalog(cls) -> dict[str, dict[str, Any]]:
        return {
            "get_view": {
                "requires_session": True,
                "mutation": False,
                "requires_expected_revision": False,
                "op_args_schema": {
                    "required": {},
                    "optional": {
                        "cursor_line": {"type": "int", "min": 1},
                        "max_view_lines": {"type": "int", "min": 1, "max": HARD_MAX_VIEW_LINES, "default": DEFAULT_MAX_VIEW_LINES},
                        "wrap_width": {"type": "int", "min": 1, "default": DEFAULT_WRAP_WIDTH},
                    },
                },
                "templates": {
                    "basic": {"op": "get_view", "op_args": {"cursor_line": 1, "max_view_lines": 40}},
                },
            },
            "move_cursor": {
                "requires_session": True,
                "mutation": False,
                "requires_expected_revision": False,
                "op_args_schema": {
                    "required": {},
                    "optional": {"line": {"type": "int", "min": 1}, "col": {"type": "int", "min": 1}},
                },
                "templates": {"jump": {"op": "move_cursor", "op_args": {"line": 120, "col": 1}}},
            },
            "replace_range": {
                "requires_session": True,
                "mutation": True,
                "requires_expected_revision": True,
                "op_args_schema": {
                    "required": {"line_start": {"type": "int", "min": 1}, "line_end": {"type": "int", "min": 1}},
                    "optional": {"replacement_lines": {"type": "string[]"}, "replacement_text": {"type": "string"}},
                },
                "templates": {
                    "dry_run": {"op": "replace_range", "dry_run": True, "expected_revision": 3, "op_args": {"line_start": 10, "line_end": 12, "replacement_lines": ["new"]}},
                    "apply": {"op": "replace_range", "expected_revision": 3, "op_args": {"line_start": 10, "line_end": 12, "replacement_lines": ["new"]}},
                },
            },
            "replace_regex": {
                "requires_session": True,
                "mutation": True,
                "requires_expected_revision": True,
                "op_args_schema": {
                    "required": {"pattern": {"type": "string"}, "replacement": {"type": "string"}},
                    "optional": {"ignore_case": {"type": "bool", "default": False}, "max_replacements": {"type": "int", "default": 0}},
                },
                "templates": {
                    "dry_run": {"op": "replace_regex", "dry_run": True, "expected_revision": 3, "op_args": {"pattern": "\\bTODO\\b", "replacement": "DONE", "max_replacements": 5}},
                    "apply": {"op": "replace_regex", "expected_revision": 3, "op_args": {"pattern": "\\bTODO\\b", "replacement": "DONE"}},
                },
            },
            "apply_patch": {
                "requires_session": True,
                "mutation": True,
                "requires_expected_revision": True,
                "op_args_schema": {"required": {"patch_text": {"type": "string"}}, "optional": {}},
                "templates": {
                    "dry_run": {"op": "apply_patch", "dry_run": True, "expected_revision": 3, "op_args": {"patch_text": "@@ -1,1 +1,1 @@\n-old\n+new"}},
                    "apply": {"op": "apply_patch", "expected_revision": 3, "op_args": {"patch_text": "@@ -1,1 +1,1 @@\n-old\n+new"}},
                },
            },
            "search_indexed": {
                "requires_session": True,
                "mutation": False,
                "requires_expected_revision": False,
                "op_args_schema": {"required": {"query": {"type": "string"}}, "optional": {}},
                "templates": {"basic": {"op": "search_indexed", "op_args": {"query": "TODO"}}},
            },
            "diagnostics": {"requires_session": True, "mutation": False, "requires_expected_revision": False, "op_args_schema": {"required": {}, "optional": {}}, "templates": {"basic": {"op": "diagnostics"}}},
            "format_range": {
                "requires_session": True,
                "mutation": True,
                "requires_expected_revision": True,
                "op_args_schema": {"required": {}, "optional": {"line_start": {"type": "int", "min": 1}, "line_end": {"type": "int", "min": 1}}},
                "templates": {"apply": {"op": "format_range", "expected_revision": 3, "op_args": {"line_start": 1, "line_end": 50}}},
            },
            "undo": {"requires_session": True, "mutation": True, "requires_expected_revision": True, "op_args_schema": {"required": {}, "optional": {"target_revision": {"type": "int", "min": 1}}}, "templates": {"step": {"op": "undo", "expected_revision": 3}}},
            "redo": {"requires_session": True, "mutation": True, "requires_expected_revision": True, "op_args_schema": {"required": {}, "optional": {}}, "templates": {"step": {"op": "redo", "expected_revision": 4}}},
            "save_revision": {"requires_session": True, "mutation": True, "requires_expected_revision": True, "op_args_schema": {"required": {}, "optional": {}}, "templates": {"apply": {"op": "save_revision", "expected_revision": 5}}},
            "op_help": {"requires_session": False, "mutation": False, "requires_expected_revision": False, "op_args_schema": {"required": {}, "optional": {"op": {"type": "string"}, "ops": {"type": "string[]"}}}, "templates": {"by_ops": {"op": "op_help", "op_args": {"ops": ["replace_range", "save_revision"]}}}},
            "help_selftest": {"requires_session": False, "mutation": False, "requires_expected_revision": False, "op_args_schema": {"required": {}, "optional": {}}, "templates": {"basic": {"op": "help_selftest"}}},
            "assign_workspace": {"requires_session": False, "mutation": True, "requires_expected_revision": False, "op_args_schema": {"required": {"workspace_file": {"type": "string"}}, "optional": {}}, "templates": {"basic": {"op": "assign_workspace", "op_args": {"workspace_file": "P:/opt/docker/cqds/my.code-workspace"}}}},
            "policy_show": {"requires_session": False, "mutation": False, "requires_expected_revision": False, "op_args_schema": {"required": {}, "optional": {}}, "templates": {"basic": {"op": "policy_show"}}},
            "telemetry_report": {"requires_session": False, "mutation": False, "requires_expected_revision": False, "op_args_schema": {"required": {}, "optional": {}}, "templates": {"basic": {"op": "telemetry_report"}}},
            "cleanup_stale_sessions": {"requires_session": False, "mutation": True, "requires_expected_revision": False, "op_args_schema": {"required": {}, "optional": {"stale_after_days": {"type": "int", "min": 1, "default": 30}}}, "templates": {"basic": {"op": "cleanup_stale_sessions", "op_args": {"stale_after_days": 30}}}},
        }

    def open_session(self, arguments: dict[str, Any]) -> dict[str, Any]:
        path = str(arguments.get("path") or "").strip()
        if not path:
            raise bad_request("invalid_request", "path is required")
        self.log.info("open_session_begin path=%s", path)
        profile_id = str(arguments.get("profile_id") or "").strip() or None
        profile_auto = bool(arguments.get("profile_auto", True))
        response_mode_default = str(arguments.get("response_mode_default") or "viewport")
        if response_mode_default not in RESPONSE_MODES:
            response_mode_default = "viewport"

        info = self.storage.open_session(path, display_path=path, profile_id=str(profile_id) if profile_id else None)
        session_id = str(info["session_id"])
        resolved_profile = self.profiles.resolve(Path(str(info["canonical_path"])), profile_id=profile_id, profile_auto=profile_auto)
        with self.storage.session_conn(session_id) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO session_meta(key,value) VALUES('response_mode_default',?)",
                (response_mode_default,),
            )
            conn.execute(
                "INSERT OR REPLACE INTO session_meta(key,value) VALUES('resolved_profile_id',?)",
                (resolved_profile.profile_id,),
            )
            current = self.storage.current_revision(conn)
            recent_ops = self._recent_ops(
                conn,
                include=bool(arguments.get("include_recent_ops", True)),
                limit=int(arguments.get("recent_ops_limit", DEFAULT_RECENT_OPS_LIMIT)),
            )
        payload = {
            "ok": True,
            "session_id": session_id,
            "current_revision": current,
            "previous_revision": max(1, current - 1),
            "session_defaults": {
                "response_mode_default": response_mode_default,
                "resolved_profile_id": resolved_profile.profile_id,
                "default_max_view_lines": DEFAULT_MAX_VIEW_LINES,
                "hard_max_view_lines": HARD_MAX_VIEW_LINES,
                "allowed_ops": list(self.SESSION_OPS),
            },
            "capabilities_guide": self._capabilities(str(arguments.get("capabilities_hint") or "")),
            "recent_ops": recent_ops,
        }
        payload["telemetry"] = self.telemetry.append(
            tool="session_open",
            op="session_open",
            request_obj=arguments,
            response_obj=payload,
            used_help=False,
            used_capabilities_guide=bool(arguments.get("capabilities_hint")),
        )
        self.log.info("open_session_ok session_id=%s revision=%s profile=%s", session_id, current, resolved_profile.profile_id)
        return payload

    def _recent_ops(self, conn, *, include: bool, limit: int) -> list[dict[str, Any]]:
        if not include:
            return []
        safe_limit = max(1, min(limit, MAX_RECENT_OPS_LIMIT))
        rows = conn.execute(
            """
            SELECT revision, op, changed_lines, created_at
            FROM revision_meta
            ORDER BY revision DESC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "revision": int(r["revision"]),
                    "op": str(r["op"]),
                    "changed_lines": int(r["changed_lines"]),
                    "ts": int(r["created_at"]),
                }
            )
        return out

    @staticmethod
    def _capabilities(raw: str) -> dict[str, str]:
        if not raw.strip():
            return {}
        keys = [p.strip().lower() for p in raw.split(",") if p.strip()]
        guide = {
            "navigation": "Use get_view/move_cursor/response_mode=numbered_lines.",
            "search": "Use search_indexed with query and optional line range.",
            "replace": "Use replace_range(line_start,line_end,replacement_lines).",
            "patch": "Use apply_patch (planned) or replace_range for v1.",
            "validation": "Use diagnostics; lint success sets LINT_SUCCESS.",
            "undo": "Use undo() or undo(target_revision).",
            "redo": "Use redo() for one-step redo branch.",
            "save": "Use save_revision to persist active revision to disk.",
        }
        return {k: guide[k] for k in keys if k in guide}

    @staticmethod
    def _op_help_sections() -> dict[str, str]:
        doc_path = Path(__file__).resolve().parent / "OP_HELP.md"
        if not doc_path.exists():
            return {}
        lines = doc_path.read_text(encoding="utf-8").splitlines()
        sections: dict[str, list[str]] = {}
        current: str | None = None
        for line in lines:
            m = re.match(r"^##\s+op:([a-zA-Z0-9_]+)\s*$", line.strip())
            if m:
                current = m.group(1)
                sections[current] = []
                continue
            if current is not None:
                sections[current].append(line)
        return {k: "\n".join(v).strip() for k, v in sections.items()}

    def _op_help_payload(self, target_ops: list[str]) -> dict[str, Any]:
        catalog = self._op_catalog()
        sections = self._op_help_sections()
        if not target_ops:
            return {
                "available_ops": sorted(catalog.keys()),
                "tip": "Use op_args.op or op_args.ops[] to fetch inline markdown help sections.",
                "source": "OP_HELP.md",
            }
        help_by_op: dict[str, dict[str, Any]] = {}
        missing: list[str] = []
        for op_name in target_ops:
            if op_name in catalog:
                help_by_op[op_name] = {
                    "markdown": sections.get(op_name, f"No markdown section in OP_HELP.md for {op_name}."),
                    "op_args_schema": catalog[op_name]["op_args_schema"],
                    "requires_session": bool(catalog[op_name]["requires_session"]),
                    "requires_expected_revision": bool(catalog[op_name]["requires_expected_revision"]),
                    "mutation": bool(catalog[op_name]["mutation"]),
                    "templates": catalog[op_name]["templates"],
                    "errors": self._error_catalog_for_op(op_name),
                    "doc_status": "ok" if op_name in sections else "missing_markdown",
                }
            else:
                missing.append(op_name)
        return {
            "requested_ops": target_ops,
            "help_by_op": help_by_op,
            "missing_ops": missing,
            "available_ops": sorted(catalog.keys()),
            "source": "OP_HELP.md",
        }

    @staticmethod
    def _error_catalog_for_op(op_name: str) -> list[dict[str, Any]]:
        common = [
            {
                "code": "invalid_request",
                "class": "validation",
                "message": "Invalid or missing input fields.",
                "hint": "Check op_args_schema and required fields.",
            }
        ]
        by_op: dict[str, list[dict[str, Any]]] = {
            "replace_range": [
                {"code": "invalid_request", "class": "validation", "message": "Invalid line range.", "hint": "Ensure line_start>=1 and line_end>=line_start."},
                {"code": "revision_mismatch", "class": "concurrency", "message": "expected_revision does not match current.", "hint": "Retry using current_revision from latest response."},
            ],
            "replace_regex": [
                {"code": "invalid_request", "class": "validation", "message": "Regex pattern is required.", "hint": "Pass non-empty op_args.pattern."}
            ],
            "apply_patch": [
                {"code": "patch_context_mismatch", "class": "validation", "message": "Unified diff context mismatch.", "hint": "Refresh get_view and rebuild patch against current content."}
            ],
            "save_revision": [
                {"code": "invalid_request", "class": "validation", "message": "expected_revision is required for mutation.", "hint": "Pass explicit expected_revision."}
            ],
            "session_mod": [
                {"code": "invalid_request", "class": "validation", "message": "derived_from source missing or unsupported.", "hint": "Use derived_from=last_success or a known command_id."}
            ],
        }
        return by_op.get(op_name, common)

    @staticmethod
    def _to_structured_help(help_payload: dict[str, Any]) -> dict[str, Any]:
        help_by_op = help_payload.get("help_by_op")
        if not isinstance(help_by_op, dict):
            return help_payload
        structured: dict[str, Any] = {}
        for op_name, card in help_by_op.items():
            if not isinstance(card, dict):
                continue
            schema = card.get("op_args_schema") if isinstance(card.get("op_args_schema"), dict) else {"required": {}, "optional": {}}
            required = schema.get("required") if isinstance(schema.get("required"), dict) else {}
            optional = schema.get("optional") if isinstance(schema.get("optional"), dict) else {}
            properties: dict[str, Any] = {}
            required_keys: list[str] = []
            for key, meta in required.items():
                properties[key] = meta
                required_keys.append(key)
            for key, meta in optional.items():
                properties[key] = meta
            structured[op_name] = {
                "contract": {
                    "requires_session": bool(card.get("requires_session")),
                    "requires_expected_revision": bool(card.get("requires_expected_revision")),
                    "mutation": bool(card.get("mutation")),
                },
                "input_schema": {
                    "type": "object",
                    "required": required_keys,
                    "properties": properties,
                },
                "templates": card.get("templates", {}),
                "errors": card.get("errors", []),
                "doc_status": card.get("doc_status", "unknown"),
            }
        return {
            "requested_ops": help_payload.get("requested_ops", []),
            "available_ops": help_payload.get("available_ops", []),
            "missing_ops": help_payload.get("missing_ops", []),
            "format": "structured_json",
            "ops": structured,
        }

    @staticmethod
    def _filter_help_card(card: dict[str, Any], sections: list[str], verbosity: str) -> dict[str, Any]:
        if not sections:
            return card
        out: dict[str, Any] = {}
        mapping = {
            "contract": ["requires_session", "requires_expected_revision", "mutation"],
            "op_args_schema": ["op_args_schema"],
            "templates": ["templates"],
            "examples": ["templates"],
            "errors": ["errors"],
            "constraints": ["requires_expected_revision", "mutation"],
        }
        for section in sections:
            for key in mapping.get(section, []):
                if key in card:
                    out[key] = card[key]
        if "markdown" in card and ("contract" in sections or "constraints" in sections):
            md = str(card["markdown"])
            out["markdown"] = md[:320] if verbosity == "brief" else md
        if "doc_status" in card:
            out["doc_status"] = card["doc_status"]
        return out or card

    def _record_last_success_command(self, payload: dict[str, Any], request: dict[str, Any]) -> None:
        sid = str(payload.get("session_id") or request.get("session_id") or "").strip()
        op = str(request.get("op") or "").strip()
        if not sid or not op:
            return
        if op in self.ADMIN_OPS:
            return
        command_id = self._command_id_for(request)
        self._last_success_cmd_by_session[sid] = {
            "command_id": command_id,
            "session_id": sid,
            "op": op,
            "op_args": dict(request.get("op_args") or {}),
            "response_mode": request.get("response_mode"),
            "response_as": request.get("response_as"),
            "dry_run": bool(request.get("dry_run", False)),
            "confirm": bool(request.get("confirm", False)),
            "auto_sync": bool(request.get("auto_sync", True)),
            "current_revision": payload.get("current_revision"),
        }
        self._success_cmd_by_id[command_id] = dict(self._last_success_cmd_by_session[sid])

    def execute_mod(self, arguments: dict[str, Any]) -> dict[str, Any]:
        derived_from = str(arguments.get("derived_from") or "last_success").strip()
        sid = str(arguments.get("session_id") or "").strip()
        if not sid:
            raise bad_request(
                "invalid_request",
                "session_id is required for session_mod MVP",
                hint="Pass session_id and derive from last successful command in that session.",
            )
        if derived_from == "last_success":
            base = self._last_success_cmd_by_session.get(sid)
        else:
            base = self._success_cmd_by_id.get(derived_from)
            if base and str(base.get("session_id") or "") != sid:
                raise bad_request("invalid_request", "derived_from command_id belongs to another session", session_id=sid)
        if not base:
            raise bad_request("invalid_request", "No last successful command for session_id", session_id=sid)
        target_op = str(arguments.get("target_op") or base.get("op") or "").strip()
        resolved: dict[str, Any] = {
            "session_id": sid,
            "op": target_op,
            "op_args": dict(base.get("op_args") or {}),
        }
        for top_key in ("response_mode", "response_as", "dry_run", "confirm", "auto_sync"):
            if top_key in base and base.get(top_key) is not None:
                resolved[top_key] = base[top_key]

        control_keys = {"derived_from", "target_op", "run_mode", "verbose", "session_id"}
        for key, value in arguments.items():
            if key in control_keys:
                continue
            if key == "op_args" and isinstance(value, dict):
                for ak, av in value.items():
                    if av is None:
                        resolved["op_args"].pop(ak, None)
                    else:
                        resolved["op_args"][ak] = av
                continue
            if key in resolved and key != "op_args":
                if value is None:
                    resolved.pop(key, None)
                else:
                    resolved[key] = value
            else:
                if value is None:
                    resolved["op_args"].pop(key, None)
                else:
                    resolved["op_args"][key] = value

        is_mutation = target_op in {"replace_range", "replace_regex", "apply_patch", "format_range", "undo", "redo", "save_revision"}
        if is_mutation and "expected_revision" not in arguments:
            raise bad_request(
                "invalid_request",
                "expected_revision is required for mutation in session_mod",
                hint="Pass explicit expected_revision in THIS session_mod call; server does not auto-fill it.",
                example_payload={"derived_from": "last_success", "session_id": sid, "expected_revision": base.get("current_revision"), "target_op": target_op},
            )
        if is_mutation and arguments.get("expected_revision") is not None:
            resolved["expected_revision"] = int(arguments["expected_revision"])

        run_mode = str(arguments.get("run_mode") or "execute").strip().lower()
        if run_mode == "preview":
            return {
                "ok": True,
                "executed": False,
                "derived_from": derived_from,
                "base_command_id": str(base.get("command_id") or ""),
                "command_id": self._command_id_for(resolved),
                "resolved_payload": resolved,
            }
        result = self.execute(resolved)
        self._record_last_success_command(result, resolved)
        return {
            "ok": True,
            "executed": True,
            "derived_from": derived_from,
            "base_command_id": str(base.get("command_id") or ""),
            "command_id": self._command_id_for(resolved),
            "result": result,
        }

    def _help_selftest_payload(self) -> dict[str, Any]:
        catalog = self._op_catalog()
        sections = self._op_help_sections()
        missing_markdown = sorted([op for op in catalog.keys() if op not in sections])
        unknown_in_markdown = sorted([op for op in sections.keys() if op not in catalog])
        session_open_ops = list(self.SESSION_OPS)
        session_ops_from_catalog = sorted([op for op, meta in catalog.items() if bool(meta.get("requires_session"))])
        mismatch_open_vs_catalog = sorted(set(session_open_ops) ^ set(session_ops_from_catalog))
        return {
            "ok": True,
            "op": "help_selftest",
            "checks": {
                "catalog_total_ops": len(catalog),
                "session_ops_count": len(self.SESSION_OPS),
                "admin_ops_count": len(self.ADMIN_OPS),
                "missing_markdown_sections": missing_markdown,
                "unknown_markdown_sections": unknown_in_markdown,
                "session_open_vs_catalog_mismatch": mismatch_open_vs_catalog,
            },
            "status": "ok" if not missing_markdown and not unknown_in_markdown and not mismatch_open_vs_catalog else "needs_attention",
        }

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        op = str(arguments.get("op") or "").strip()
        if not op:
            raise bad_request("invalid_request", "op is required")
        self.log.info("execute_begin op=%s session_id=%s", op, str(arguments.get("session_id") or ""))
        op_args = arguments.get("op_args") or {}
        if not isinstance(op_args, dict):
            raise bad_request("invalid_request", "op_args must be object")
        if op == "cleanup_stale_sessions":
            stale_after_days = int(op_args.get("stale_after_days", 30))
            stats = self.storage.cleanup_stale_sessions(stale_after_days=stale_after_days)
            payload = {
                "ok": True,
                "op": "cleanup_stale_sessions",
                "stale_after_days": stale_after_days,
                "stats": stats,
            }
            payload["telemetry"] = self.telemetry.append(
                tool="session_cmd",
                op=op,
                request_obj=arguments,
                response_obj=payload,
                used_help=False,
                used_capabilities_guide=False,
            )
            self.log.info("execute_admin_ok op=%s checked=%s", op, int(stats.get("checked", 0)))
            return payload
        if op == "op_help":
            target = str(op_args.get("op") or "").strip()
            ops = op_args.get("ops")
            sections_raw = op_args.get("sections")
            sections = [str(s).strip() for s in sections_raw] if isinstance(sections_raw, list) else []
            verbosity = str(op_args.get("verbosity") or "brief").strip().lower()
            if verbosity not in {"brief", "normal", "full"}:
                verbosity = "brief"
            output_mode = str(op_args.get("output_mode") or "hybrid").strip().lower()
            if output_mode not in {"hybrid", "structured_json"}:
                output_mode = "hybrid"
            target_ops: list[str] = []
            if target:
                target_ops.append(target)
            if isinstance(ops, list):
                for item in ops:
                    name = str(item).strip()
                    if name:
                        target_ops.append(name)
            # keep order, remove duplicates
            seen: set[str] = set()
            dedup_ops: list[str] = []
            for item in target_ops:
                if item not in seen:
                    seen.add(item)
                    dedup_ops.append(item)
            payload = {
                "ok": True,
                "op": "op_help",
                "target_op": target or None,
                "help": self._op_help_payload(dedup_ops),
            }
            if sections:
                hb = payload["help"].get("help_by_op") or {}
                filtered: dict[str, Any] = {}
                for name, card in hb.items():
                    if isinstance(card, dict):
                        filtered[name] = self._filter_help_card(card, sections, verbosity)
                payload["help"]["help_by_op"] = filtered
            payload["help"]["filters"] = {
                "sections": sections or ["contract", "templates"],
                "verbosity": verbosity,
                "output_mode": output_mode,
            }
            if output_mode == "structured_json":
                payload["help"] = self._to_structured_help(payload["help"])
            payload["telemetry"] = self.telemetry.append(
                tool="session_cmd",
                op=op,
                request_obj=arguments,
                response_obj=payload,
                used_help=True,
                used_capabilities_guide=False,
            )
            self.log.info("execute_admin_ok op=%s target_op=%s", op, target)
            return payload
        if op == "help_selftest":
            payload = self._help_selftest_payload()
            payload["telemetry"] = self.telemetry.append(
                tool="session_cmd",
                op=op,
                request_obj=arguments,
                response_obj=payload,
                used_help=True,
                used_capabilities_guide=False,
            )
            self.log.info("execute_admin_ok op=%s status=%s", op, payload.get("status"))
            return payload
        if op == "assign_workspace":
            workspace_file = str(op_args.get("workspace_file") or "").strip()
            if not workspace_file:
                raise bad_request("invalid_request", "assign_workspace.workspace_file is required")
            updated = assign_workspace_allowed_roots(self.storage.data_dir, Path(workspace_file))
            self.policy = load_policy(self.storage.data_dir)
            self.storage.policy = self.policy
            payload = {
                "ok": True,
                "op": "assign_workspace",
                "workspace_file": str(Path(workspace_file).expanduser().resolve()),
                "allowed_roots": list(updated.allowed_roots),
            }
            payload["telemetry"] = self.telemetry.append(
                tool="session_cmd",
                op=op,
                request_obj=arguments,
                response_obj=payload,
                used_help=False,
                used_capabilities_guide=False,
            )
            self.log.info("execute_admin_ok op=%s roots_count=%s", op, len(updated.allowed_roots))
            return payload
        if op == "policy_show":
            meta = policy_meta(self.storage.data_dir)
            bindings = policy_bindings(self.storage.data_dir)
            active_workspaces = policy_active_workspaces(self.storage.data_dir)
            payload = {
                "ok": True,
                "op": "policy_show",
                "allowed_roots": list(self.policy.allowed_roots),
                "max_file_size_bytes": self.policy.max_file_size_bytes,
                "max_payload_bytes": self.policy.max_payload_bytes,
                "max_changed_lines": self.policy.max_changed_lines,
                "max_response_bytes": self.policy.max_response_bytes,
                "policy_source": str(meta.get("source") or "unknown"),
                "workspace_file": str(meta.get("workspace_file") or ""),
                "bindings_count": len(bindings),
                "binding_workspaces": sorted(bindings.keys()),
                "active_workspaces": active_workspaces,
                "workspace_context": {
                    "active_workspaces": active_workspaces,
                    "workspace_file": str(meta.get("workspace_file") or ""),
                    "policy_source": str(meta.get("source") or "unknown"),
                    "active_roots_count": len(self.policy.allowed_roots),
                },
            }
            payload["telemetry"] = self.telemetry.append(
                tool="session_cmd",
                op=op,
                request_obj=arguments,
                response_obj=payload,
                used_help=False,
                used_capabilities_guide=False,
            )
            self.log.info("execute_admin_ok op=%s roots_count=%s", op, len(self.policy.allowed_roots))
            return payload
        if op == "telemetry_report":
            payload = {
                "ok": True,
                "op": "telemetry_report",
                "report": self.telemetry.report(),
            }
            payload["telemetry"] = self.telemetry.append(
                tool="session_cmd",
                op=op,
                request_obj=arguments,
                response_obj=payload,
                used_help=False,
                used_capabilities_guide=False,
            )
            self.log.info("execute_admin_ok op=%s entries=%s", op, int(payload["report"].get("entries", 0)))
            return payload

        sid = str(arguments.get("session_id") or "").strip()
        if not sid:
            raise bad_request(
                "invalid_request",
                "session_id is required",
                hint="Use session_open first, then pass returned session_id.",
                example_payload={"op": "get_view", "session_id": "<sid>"},
            )

        response_mode = str(arguments.get("response_mode") or arguments.get("response_as") or "viewport")
        if response_mode not in RESPONSE_MODES:
            raise bad_request(
                "invalid_request",
                f"unknown response_mode '{response_mode}'",
                hint=f"Allowed response_mode values: {sorted(RESPONSE_MODES)}",
                example_payload={"op": "get_view", "session_id": sid, "response_mode": "numbered_lines"},
            )
        dry_run = bool(arguments.get("dry_run", False))

        with self.storage.session_conn(sid) as conn:
            current = self.storage.current_revision(conn)
            expected = arguments.get("expected_revision")
            is_mutation = op in {"replace_range", "replace_regex", "apply_patch", "format_range", "undo", "redo", "save_revision"}
            if is_mutation:
                if expected is None:
                    raise bad_request(
                        "invalid_request",
                        "expected_revision is required for mutation",
                        hint="Read current_revision from previous response and pass it as expected_revision.",
                        example_payload={"op": op, "session_id": sid, "expected_revision": current},
                    )
                if int(expected) != current:
                    raise EditorError(
                        "concurrency",
                        "revision_mismatch",
                        f"Expected revision {expected}, actual {current}",
                        details={"expected_revision": int(expected), "actual_revision": current, "session_id": sid},
                    )
            # optional auto-sync when source file changed externally
            auto_sync = bool(arguments.get("auto_sync", True))
            if is_mutation:
                synced = self._maybe_external_sync(sid, conn, current, auto_sync)
                if synced is not None:
                    current = synced
            lines = self.storage.read_snapshot(conn, "current_revision")
            previous_lines = self.storage.read_snapshot(conn, "previous_revision")

            payload: dict[str, Any]
            if op == "get_view":
                payload = self._view_payload(lines, current, max(1, current - 1), response_mode, op_args)
            elif op == "move_cursor":
                line = int(op_args.get("line", 1))
                col = int(op_args.get("col", 1))
                conn.execute("INSERT OR REPLACE INTO session_meta(key,value) VALUES('cursor_line',?)", (str(max(1, line)),))
                conn.execute("INSERT OR REPLACE INTO session_meta(key,value) VALUES('cursor_col',?)", (str(max(1, col)),))
                payload = self._view_payload(lines, current, max(1, current - 1), response_mode, op_args)
            elif op == "search_indexed":
                payload = self._search_payload(conn, lines, op_args, current)
            elif op == "diagnostics":
                payload = self._diagnostics_payload(conn, lines, current, sid)
            elif op == "format_range":
                payload = self._format_range(sid, conn, lines, current, response_mode, op_args, dry_run)
            elif op == "replace_range":
                payload = self._replace_range(sid, conn, lines, op_args, response_mode, dry_run)
            elif op == "replace_regex":
                payload = self._replace_regex(sid, conn, lines, op_args, response_mode, dry_run)
            elif op == "apply_patch":
                payload = self._apply_patch(sid, conn, lines, op_args, response_mode, dry_run)
            elif op == "undo":
                payload = self._undo(sid, conn, lines, previous_lines, op_args, response_mode, dry_run)
            elif op == "redo":
                payload = self._redo(sid, conn, lines, previous_lines, response_mode, dry_run)
            elif op == "save_revision":
                payload = self._save_revision(sid, conn, lines, response_mode, dry_run)
            else:
                raise bad_request("unknown_op", f"Unknown op: {op}")
            payload["session_id"] = sid
            payload["command_id"] = self._command_id_for(arguments)
            payload["telemetry"] = self.telemetry.append(
                tool="session_cmd",
                op=op,
                request_obj=arguments,
                response_obj=payload,
                used_help=(op == "help"),
                used_capabilities_guide=False,
            )
            self.log.info("execute_ok op=%s session_id=%s current_revision=%s", op, sid, int(payload.get("current_revision") or current))
            self._record_last_success_command(payload, arguments)
            return payload

    def _view_payload(
        self,
        lines: list[str],
        current_revision: int,
        previous_revision: int,
        response_mode: str,
        op_args: dict[str, Any],
    ) -> dict[str, Any]:
        requested_max_lines = int(op_args.get("max_view_lines", DEFAULT_MAX_VIEW_LINES))
        max_lines = max(1, min(requested_max_lines, HARD_MAX_VIEW_LINES))
        cursor_line = max(1, int(op_args.get("cursor_line", 1)))
        total = len(lines)
        if total == 0:
            return {
                "ok": True,
                "current_revision": current_revision,
                "previous_revision": previous_revision,
                "total_lines": 0,
                "view": [],
            }
        cursor_line = min(cursor_line, total)
        half = max_lines // 2
        start = max(1, cursor_line - half)
        end = min(total, start + max_lines - 1)
        start = max(1, end - max_lines + 1)
        wrap_width = int(op_args.get("wrap_width", DEFAULT_WRAP_WIDTH))
        view_slice = lines[start - 1 : end]
        total_candidate_lines = len(view_slice)
        max_out = min(max_lines, MAX_NUMBERED_LINES)
        out_slice = view_slice[:max_out]
        truncated = (total_candidate_lines > len(out_slice)) or (requested_max_lines > max_lines)
        if response_mode == "numbered_lines":
            payload: list[dict[str, Any]] = []
            for offset, line in enumerate(out_slice, start=start):
                payload.append({"line_num": offset, "text": _wrap_text(line, wrap_width)})
            return {
                "ok": True,
                "current_revision": current_revision,
                "previous_revision": previous_revision,
                "response_mode": response_mode,
                "viewport_start": start,
                "viewport_end": end,
                "total_lines": total,
                "returned_lines": len(payload),
                "total_candidate_lines": total_candidate_lines,
                "truncated": truncated,
                "view": payload,
            }
        return {
            "ok": True,
            "current_revision": current_revision,
            "previous_revision": previous_revision,
            "response_mode": response_mode,
            "viewport_start": start,
            "viewport_end": end,
            "total_lines": total,
            "returned_lines": len(out_slice),
            "total_candidate_lines": total_candidate_lines,
            "truncated": truncated,
            "view": out_slice,
        }

    def _search_payload(self, conn, lines: list[str], op_args: dict[str, Any], current_revision: int) -> dict[str, Any]:
        query = str(op_args.get("query") or "")
        if not query:
            raise bad_request("invalid_request", "search_indexed.query is required")
        hits: list[dict[str, Any]] = []
        for idx, line in enumerate(lines, start=1):
            if query in line:
                first_rev = self._first_revision_for_text(conn, line)
                hits.append({"line_num": idx, "text": line, "first_revision": first_rev})
        return {
            "ok": True,
            "current_revision": current_revision,
            "previous_revision": max(1, current_revision - 1),
            "query": query,
            "hit_count": len(hits),
            "hits": hits[:MAX_NUMBERED_LINES],
            "truncated": len(hits) > MAX_NUMBERED_LINES,
        }

    @staticmethod
    def _first_revision_for_text(conn, text: str) -> int | None:
        row = conn.execute(
            """
            SELECT MIN(h.revision) AS first_rev
            FROM revision_history h
            JOIN text_lines t ON t.idx = h.added_idx
            WHERE h.added_idx >= 0 AND t.text = ?
            """,
            (text,),
        ).fetchone()
        if row is None:
            return None
        value = row["first_rev"]
        return int(value) if value is not None else None

    def _resolve_profile(self, conn, sid: str) -> Profile:
        pid = self.storage.get_meta(conn, "resolved_profile_id", "plain") or "plain"
        info = self.storage.get_session_info(sid)
        return self.profiles.resolve(Path(str(info["canonical_path"])), profile_id=pid, profile_auto=False)

    def _diagnostics_payload(self, conn, lines: list[str], current_revision: int, sid: str) -> dict[str, Any]:
        issues: list[dict[str, Any]] = []
        for i, line in enumerate(lines, start=1):
            if line.endswith(" ") or line.endswith("\t"):
                issues.append({"line": i, "code": "trailing_spaces", "message": "Trailing spaces"})
        brackets = 0
        for line in lines:
            brackets += line.count("(") - line.count(")")
        profile = self._resolve_profile(conn, sid)
        suffix = ".txt"
        info = self.storage.get_session_info(sid)
        suffix = Path(str(info["canonical_path"])).suffix or ".txt"
        syntax_ok, syntax_msg = run_syntax_check(profile, "\n".join(lines), suffix)
        if not syntax_ok:
            issues.append({"line": 1, "code": "syntax_check_failed", "message": syntax_msg or "Syntax check failed"})
        lint_ok = not issues and brackets == 0 and syntax_ok
        if lint_ok:
            conn.execute(
                "UPDATE revision_meta SET op=op, source=source WHERE revision=?",
                (current_revision,),
            )
            conn.execute(
                "UPDATE revision_history SET flags = (flags | ?) WHERE revision=?",
                (LINT_SUCCESS, current_revision),
            )
        return {
            "ok": True,
            "current_revision": current_revision,
            "previous_revision": max(1, current_revision - 1),
            "issues": issues,
            "lint_success": lint_ok,
            "bracket_balance_ok": brackets == 0,
            "profile_id": profile.profile_id,
        }

    def _format_range(
        self,
        sid: str,
        conn,
        lines: list[str],
        current_revision: int,
        response_mode: str,
        op_args: dict[str, Any],
        dry_run: bool,
    ) -> dict[str, Any]:
        profile = self._resolve_profile(conn, sid)
        if not profile.format_cmd:
            return {
                "ok": True,
                "current_revision": current_revision,
                "previous_revision": max(1, current_revision - 1),
                "formatted": False,
                "message": "No formatter configured for this profile.",
            }
        line_start = int(op_args.get("line_start", 1))
        line_end = int(op_args.get("line_end", len(lines)))
        if line_start < 1 or line_end < line_start:
            raise bad_request("invalid_request", "invalid line range")
        start_idx = min(line_start - 1, len(lines))
        end_idx = min(line_end, len(lines))
        original_block = lines[start_idx:end_idx]
        info = self.storage.get_session_info(sid)
        suffix = Path(str(info["canonical_path"])).suffix or ".txt"
        ok, formatted_content, fmt_msg = run_formatter(profile, "\n".join(original_block), suffix)
        if not ok:
            raise EditorError(
                "validation",
                "format_failed",
                "Formatter command failed",
                hint="Check profile format_cmd and formatter output.",
                details={"profile_id": profile.profile_id, "message": fmt_msg[:500]},
            )
        new_block = formatted_content.splitlines()
        new_lines = lines[:start_idx] + new_block + lines[end_idx:]
        if dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "current_revision": current_revision,
                "previous_revision": max(1, current_revision - 1),
                "preview_changed_lines": abs(len(new_lines) - len(lines)),
                "formatted": new_lines != lines,
            }
        if new_lines == lines:
            return {
                "ok": True,
                "current_revision": current_revision,
                "previous_revision": max(1, current_revision - 1),
                "formatted": False,
                "changed": False,
            }
        cur, prev = self.storage.write_revision(
            sid,
            "format_range",
            lines,
            new_lines,
            response_mode=response_mode,
        )
        payload = self._view_payload(new_lines, cur, prev, response_mode, {})
        payload["formatted"] = True
        payload["compact_diff"] = self._compact_diff(lines, new_lines)
        return {
            **payload,
        }

    def _replace_range(
        self,
        sid: str,
        conn,
        lines: list[str],
        op_args: dict[str, Any],
        response_mode: str,
        dry_run: bool,
    ) -> dict[str, Any]:
        line_start = int(op_args.get("line_start", 1))
        line_end = int(op_args.get("line_end", line_start))
        replacement = op_args.get("replacement_lines")
        if isinstance(replacement, list):
            new_block = [str(x) for x in replacement]
        else:
            new_block = str(op_args.get("replacement_text", "")).splitlines()
        if line_start < 1 or line_end < line_start:
            raise bad_request("invalid_request", "invalid line range")
        old = list(lines)
        start_idx = min(line_start - 1, len(lines))
        end_idx = min(line_end, len(lines))
        new_lines = lines[:start_idx] + new_block + lines[end_idx:]
        if dry_run:
            return {
                "ok": True,
                "current_revision": self.storage.current_revision(conn),
                "previous_revision": max(1, self.storage.current_revision(conn) - 1),
                "dry_run": True,
                "preview_changed_lines": abs(len(new_lines) - len(old)),
            }
        cur, prev = self.storage.write_revision(sid, "replace_range", old, new_lines, response_mode=response_mode)
        payload = self._view_payload(new_lines, cur, prev, response_mode, {})
        payload["compact_diff"] = self._compact_diff(old, new_lines)
        return payload

    def _replace_regex(
        self,
        sid: str,
        conn,
        lines: list[str],
        op_args: dict[str, Any],
        response_mode: str,
        dry_run: bool,
    ) -> dict[str, Any]:
        pattern = str(op_args.get("pattern") or "")
        repl = str(op_args.get("replacement") or "")
        if not pattern:
            raise bad_request("invalid_request", "replace_regex.pattern is required")
        flags = re.MULTILINE
        if bool(op_args.get("ignore_case", False)):
            flags |= re.IGNORECASE
        regex = re.compile(pattern, flags)
        whole = "\n".join(lines)
        max_repl = int(op_args.get("max_replacements", 0))
        count = 0 if max_repl <= 0 else max_repl
        new_whole, n = regex.subn(repl, whole, count=count)
        new_lines = new_whole.split("\n")
        if dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "current_revision": self.storage.current_revision(conn),
                "previous_revision": max(1, self.storage.current_revision(conn) - 1),
                "preview_replacements": n,
            }
        cur, prev = self.storage.write_revision(
            sid,
            "replace_regex",
            lines,
            new_lines,
            response_mode=response_mode,
        )
        payload = self._view_payload(new_lines, cur, prev, response_mode, {})
        payload["compact_diff"] = self._compact_diff(lines, new_lines)
        payload["replacements"] = n
        return payload

    def _apply_patch(
        self,
        sid: str,
        conn,
        lines: list[str],
        op_args: dict[str, Any],
        response_mode: str,
        dry_run: bool,
    ) -> dict[str, Any]:
        patch_text = str(op_args.get("patch_text") or "")
        if not patch_text:
            raise bad_request("invalid_request", "apply_patch.patch_text is required")
        new_lines = self._apply_unified_diff(lines, patch_text)
        if dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "current_revision": self.storage.current_revision(conn),
                "previous_revision": max(1, self.storage.current_revision(conn) - 1),
                "preview_changed_lines": abs(len(new_lines) - len(lines)),
            }
        cur, prev = self.storage.write_revision(sid, "apply_patch", lines, new_lines, response_mode=response_mode)
        payload = self._view_payload(new_lines, cur, prev, response_mode, {})
        payload["compact_diff"] = self._compact_diff(lines, new_lines)
        return payload

    @staticmethod
    def _apply_unified_diff(lines: list[str], patch_text: str) -> list[str]:
        src = list(lines)
        out = list(src)
        patch_lines = patch_text.splitlines()
        i = 0
        # very small parser for unified hunks used by LLM patch flows
        while i < len(patch_lines):
            line = patch_lines[i]
            if not line.startswith("@@"):
                i += 1
                continue
            m = re.match(r"@@ -(\d+),?(\d*) \+(\d+),?(\d*) @@", line)
            if not m:
                raise bad_request("patch_context_mismatch", f"Invalid hunk header: {line}")
            old_start = int(m.group(1))
            new_block: list[str] = []
            j = i + 1
            old_cursor = old_start - 1
            while j < len(patch_lines) and not patch_lines[j].startswith("@@"):
                h = patch_lines[j]
                if h.startswith(" "):
                    if old_cursor >= len(src) or src[old_cursor] != h[1:]:
                        raise bad_request("patch_context_mismatch", f"Context mismatch near line {old_cursor+1}")
                    new_block.append(h[1:])
                    old_cursor += 1
                elif h.startswith("-"):
                    if old_cursor >= len(src) or src[old_cursor] != h[1:]:
                        raise bad_request("patch_context_mismatch", f"Delete mismatch near line {old_cursor+1}")
                    old_cursor += 1
                elif h.startswith("+"):
                    new_block.append(h[1:])
                else:
                    # tolerate metadata
                    pass
                j += 1
            old_end = old_cursor
            out[old_start - 1 : old_end] = new_block
            src = list(out)
            i = j
        return out

    def _undo(self, sid: str, conn, lines: list[str], previous_lines: list[str], op_args: dict[str, Any], response_mode: str, dry_run: bool) -> dict[str, Any]:
        target = op_args.get("target_revision")
        if target is not None:
            target_rev = int(target)
            current = self.storage.current_revision(conn)
            if target_rev < 1 or target_rev >= current:
                raise bad_request(
                    "invalid_request",
                    "target_revision must be >=1 and less than current_revision",
                    current_revision=current,
                    target_revision=target_rev,
                )
            snapshot = self.storage.snapshot_lines(conn, target_rev)
            if snapshot is None:
                raise EditorError(
                    "validation",
                    "revision_not_available_after_compaction",
                    f"Snapshot for revision {target_rev} not available",
                    hint="Retry with a newer target_revision.",
                    details={"target_revision": target_rev, "current_revision": current},
                )
            if dry_run:
                return {
                    "ok": True,
                    "dry_run": True,
                    "current_revision": current,
                    "previous_revision": max(1, current - 1),
                    "redo_revision": max(1, current - 1),
                    "target_revision": target_rev,
                }
            cur, prev = self.storage.write_revision(sid, "undo", lines, snapshot, response_mode=response_mode)
            payload = self._view_payload(snapshot, cur, prev, response_mode, {})
            payload["redo_revision"] = prev
            payload["target_revision"] = target_rev
            return payload
        if dry_run:
            current = self.storage.current_revision(conn)
            return {
                "ok": True,
                "dry_run": True,
                "current_revision": current,
                "previous_revision": max(1, current - 1),
                "redo_revision": max(1, current - 1),
            }
        cur, prev = self.storage.write_revision(sid, "undo", lines, previous_lines, response_mode=response_mode)
        payload = self._view_payload(previous_lines, cur, prev, response_mode, {})
        payload["redo_revision"] = prev
        return payload

    def _redo(self, sid: str, conn, lines: list[str], previous_lines: list[str], response_mode: str, dry_run: bool) -> dict[str, Any]:
        # one-step redo by swapping snapshots again
        if dry_run:
            current = self.storage.current_revision(conn)
            return {"ok": True, "dry_run": True, "current_revision": current, "previous_revision": max(1, current - 1)}
        cur, prev = self.storage.write_revision(sid, "redo", lines, previous_lines, response_mode=response_mode)
        return self._view_payload(previous_lines, cur, prev, response_mode, {})

    def _save_revision(self, sid: str, conn, lines: list[str], response_mode: str, dry_run: bool) -> dict[str, Any]:
        info = self.storage.get_session_info(sid)
        target = Path(str(info["canonical_path"]))
        body = "\n".join(lines)
        if dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "current_revision": self.storage.current_revision(conn),
                "previous_revision": max(1, self.storage.current_revision(conn) - 1),
            }
        target.write_text(body, encoding="utf-8")
        conn.execute(
            "UPDATE revision_history SET flags = (flags | ?) WHERE revision=?",
            (SAVED_TO_DISK, self.storage.current_revision(conn)),
        )
        now = int(time.time())
        self.storage._set_meta(conn, "source_mtime_ns", str(target.stat().st_mtime_ns))
        self.storage._set_meta(conn, "source_size_bytes", str(target.stat().st_size))
        self.storage._set_meta(conn, "source_hash", self.storage.source_markers(target)["source_hash"])
        self.storage._set_meta(conn, "source_checked_at", str(now))
        return {
            "ok": True,
            "current_revision": self.storage.current_revision(conn),
            "previous_revision": max(1, self.storage.current_revision(conn) - 1),
            "saved_to_disk": True,
            "path": str(target),
        }

    @staticmethod
    def _compact_diff(old_lines: list[str], new_lines: list[str]) -> list[str]:
        old = [f"{line}\n" for line in old_lines]
        new = [f"{line}\n" for line in new_lines]
        diff = list(difflib.unified_diff(old, new, fromfile="before", tofile="after", n=2))
        return [line.rstrip("\n") for line in diff[:120]]

    def _maybe_external_sync(self, sid: str, conn, current_revision: int, auto_sync: bool) -> int | None:
        info = self.storage.get_session_info(sid)
        path = Path(str(info["canonical_path"]))
        markers = self.storage.source_markers(path)
        old_mtime = self.storage.get_meta(conn, "source_mtime_ns", "0")
        old_size = self.storage.get_meta(conn, "source_size_bytes", "0")
        old_hash = self.storage.get_meta(conn, "source_hash", "")
        drift = (
            markers["source_mtime_ns"] != str(old_mtime)
            or markers["source_size_bytes"] != str(old_size)
            or markers["source_hash"] != str(old_hash)
        )
        if not drift:
            return None
        if not auto_sync:
            raise EditorError(
                "source_sync",
                "source_changed_externally",
                "Source file changed externally",
                hint="Retry with auto_sync=true or force_sync policy.",
                details={"session_id": sid, "current_revision": current_revision},
            )
        disk_lines = path.read_text(encoding="utf-8").splitlines()
        current_lines = self.storage.read_snapshot(conn, "current_revision")
        cur, _prev = self.storage.write_revision(
            sid,
            "external_sync",
            current_lines,
            disk_lines,
            response_mode="minimal",
        )
        with self.storage.session_conn(sid) as sconn:
            self.storage.update_source_markers(sconn, markers)
        return cur

