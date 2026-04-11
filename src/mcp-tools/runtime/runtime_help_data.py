from __future__ import annotations

# Actions for cq_process_ctl; each key is the fragment after # in tool_ref cq_process_ctl#<key>
PROCESS_ACTIONS = {
    "spawn": {
        "summary": "Start a new subprocess and return process_guid.",
        "args": {
            "project_id": "int (required for host=false)",
            "command": "string | string[]",
            "engine": "bash|python (host=false only, default bash)",
            "cwd": "string (optional)",
            "env": "object (optional)",
            "timeout": "seconds, default 3600; host spawn has no upper cap; sandbox spawn capped by CQDS_PROCESS_MAX_LIFETIME_SEC (default 86400s). Host: lifecycle JSON lines → CQDS_HOST_PROC_LOG_FILE (CQDS_HOST_PROC_LOG=0 off)",
        },
        "returns": {
            "process_guid": "uuid",
            "pid": "number (host only when available)",
            "status": "spawned|ok",
        },
        "examples": [
            {
                "host": False,
                "action": "spawn",
                "args": {
                    "project_id": 1,
                    "command": "sleep 5",
                    "engine": "bash",
                    "timeout": 60,
                },
            },
            {
                "host": True,
                "action": "spawn",
                "args": {
                    "command": ["python", "-c", "print('hi')"],
                    "timeout": 30,
                },
            },
        ],
    },
    "io": {
        "summary": "Read stdout/stderr tails and optionally write to stdin.",
        "args": {
            "process_guid": "uuid (required)",
            "input": "string (optional)",
            "read_timeout_ms": "int, default 5000",
            "max_bytes": "int, default 65536",
            "project_id": "int (optional for host=false)",
        },
        "returns": {
            "stdout_fragment": "string",
            "stderr_fragment": "string",
            "alive": "bool",
            "returncode": "int|null",
        },
    },
    "wait": {
        "summary": "Poll process state until output appears or process finishes.",
        "args": {
            "process_guid": "uuid (required)",
            "wait_timeout_ms": "int, default 30000",
            "wait_condition": "any_output|finished",
            "project_id": "int (optional for host=false)",
        },
    },
    "status": {
        "summary": "Get process status and runtime metrics.",
        "args": {
            "process_guid": "uuid (required)",
            "project_id": "int (optional for host=false)",
        },
    },
    "kill": {
        "summary": "Stop process by signal.",
        "args": {
            "process_guid": "uuid (required)",
            "signal": "SIGTERM|SIGKILL",
            "project_id": "int (optional for host=false)",
        },
    },
    "list": {
        "summary": "List tracked processes.",
        "args": {
            "project_id": "int (optional for host=false)",
        },
    },
}

PROCESS_CTL_OVERVIEW = {
    "tool": "cq_process_ctl",
    "summary": (
        "Process control: host=false → mcp-sandbox; host=true → MCP host. "
        "Prefer batch (requests[]) for 2+ steps — fewer tool rounds under rate limits."
    ),
    "request_shape": {
        "single": "host (bool, default false), action, args",
        "batch": "requests: [{host?, action, args}, …]; stop_on_error: bool",
        "action": "spawn | io | wait | status | kill | list",
    },
    "response_shape": (
        "Single: ok, batch:false, host, action, hint, legacy_result. "
        "Batch: ok, batch:true, all_ok, count, results[] (each step like single or ok:false + error)."
    ),
}

# Short lines for catalog / overview table
def action_index() -> list[dict[str, str]]:
    return [
        {
            "action": name,
            "summary": block["summary"],
            "see_tool_ref": f"cq_process_ctl#{name}",
        }
        for name, block in PROCESS_ACTIONS.items()
    ]


CQ_HELP_SELF = {
    "tool_ref": "cq_help",
    "summary": (
        "Manuals for cqds_runtime. Prefer batch (requests[]) on any *_ctl tool when doing 2+ steps. "
        "Colloquium: cq_chat_ctl, cq_project_ctl, cq_files_ctl, cq_exec_ctl; host: cq_process_ctl, cq_docker_ctl."
    ),
    "parameters": {
        "tool_ref": (
            "Optional. Examples: empty → catalog; 'cq_process_ctl' → overview; "
            "'cq_process_ctl#spawn' → one action; 'cq_help' → this contract."
        ),
        "include_examples": "boolean, default true (omit examples when false to save tokens)",
    },
}

DOCKER_CTL_OVERVIEW = {
    "tool": "cq_docker_ctl",
    "summary": (
        "Docker on MCP host: compose (project dir or cqds default), cli (docker ps etc.), "
        "cqds_ctl, exec, inspect, logs. Prefer batch requests[] for multiple steps."
    ),
    "request_shape": {
        "single": "action: compose|cli|cqds_ctl|exec|inspect|logs; args: object",
        "batch": "requests: [{action, args}, …]; stop_on_error: bool",
    },
}

DOCKER_CTL_ACTIONS = {
    "compose": {
        "summary": (
            "docker compose <sub>. Without compose_cwd: cwd=cqds root, -f docker-compose.yml if present (legacy). "
            "With compose_cwd/working_directory: cwd=that dir; omit compose_files for auto-discovery "
            "(docker-compose.yml + docker-compose.override.yml). Optional compose_files → extra -f (paths relative to that cwd or cqds root)."
        ),
        "args": {
            "compose_command": "up|down|stop|start|restart|pull|ps|build (alias subcommand)",
            "compose_cwd": "optional absolute project dir (alias working_directory); enables override.yml discovery when compose_files omitted",
            "working_directory": "alias of compose_cwd",
            "services": "optional string[]",
            "detach": "bool, default true for up",
            "build": "bool for up --build",
            "profiles": "optional string[] → --profile",
            "compose_files": "optional -f list; if set, no implicit default file (only listed files)",
            "remove_orphans": "bool for down",
            "volumes": "bool for down -v",
            "all": "bool for ps -a",
            "timeout_sec": "default 600, max 7200",
        },
    },
    "cli": {
        "summary": "Raw docker CLI: docker <argv…> (no compose). Works from any cwd; optional cwd for subprocess.",
        "args": {
            "argv": "required string[], e.g. [\"ps\", \"-a\"] or [\"container\", \"ls\"]",
            "docker_args": "alias of argv",
            "cwd": "optional working_directory for subprocess",
            "working_directory": "alias of cwd",
            "timeout_sec": "default 120, max 7200",
        },
    },
    "cqds_ctl": {
        "summary": "scripts/cqds_ctl.py — status, restart, rebuild, clear-logs.",
        "args": {
            "command": "status|restart|rebuild|clear-logs",
            "services": "optional string[]",
            "timeout": "10–600, default 90",
            "wait": "bool, status only",
        },
    },
    "exec": {
        "summary": "docker exec one container (same as legacy cq_docker_exec one item).",
        "args": {
            "container": "required",
            "command": "string (sh -c) or argv[]",
            "workdir": "optional",
            "user": "optional",
            "env": "optional object",
            "stdin": "optional UTF-8",
            "interactive": "bool",
            "timeout_sec": "1–600, default 120",
        },
    },
    "inspect": {
        "summary": "docker inspect target (container/image id).",
        "args": {
            "target": "required",
            "format": "optional go template",
            "timeout_sec": "default 120",
        },
    },
    "logs": {
        "summary": "docker logs (no --follow; sync snapshot).",
        "args": {
            "container": "required",
            "tail": "1–10000, default 200",
            "since": "optional duration string",
            "timestamps": "bool",
            "timeout_sec": "default 120",
        },
    },
}


def docker_action_index() -> list[dict[str, str]]:
    return [
        {
            "action": name,
            "summary": block["summary"],
            "see_tool_ref": f"cq_docker_ctl#{name}",
        }
        for name, block in DOCKER_CTL_ACTIONS.items()
    ]


# --- Colloquium unified ctl (delegates to cqds_* legacy handlers; same args as old cq_* tools) ---

CHAT_CTL_OVERVIEW = {
    "tool": "cq_chat_ctl",
    "summary": (
        "Chats API via one tool. Maps to legacy cq_list_chats, cq_send_message, … "
        "Prefer batch requests[] for several chat operations."
    ),
    "request_shape": {
        "single": "action + args (flat payload per legacy tool)",
        "batch": "requests: [{action, args}, …]; stop_on_error",
    },
    "legacy_prefix": "cq_",
}

CHAT_CTL_ACTIONS = {
    "list_chats": {"summary": "List chats; obtain chat_id.", "payload": "{}"},
    "create_chat": {"summary": "Create chat; optional description in args.", "payload": "description?"},
    "send_message": {"summary": "Post text; use cq_set_sync_mode (project ctl) to wait for reply.", "payload": "chat_id, message"},
    "wait_reply": {"summary": "Long-poll up to ~15s for new AI messages.", "payload": "chat_id"},
    "get_history": {"summary": "Snapshot history without waiting.", "payload": "chat_id"},
    "chat_stats": {"summary": "Usage stats; optional since_seconds.", "payload": "chat_id, since_seconds?"},
    "copilot_chat_check": {
        "summary": "Scan Copilot chat storage for invalid text blocks; optional auto_recovery backup+fix; root auto-detected when omitted.",
        "payload": "root? (default: auto-detect), auto_recovery?",
    },
}

PROJECT_CTL_OVERVIEW = {
    "tool": "cq_project_ctl",
    "summary": (
        "Projects, DB, status, grep paging. fetch_result continues cq_start_grep (files ctl) "
        "via handle | chunk_continuation | host_grep_job_id."
    ),
    "request_shape": {
        "single": "action + args",
        "batch": "requests: [{action, args}, …]; stop_on_error",
    },
    "legacy_prefix": "cq_",
}

PROJECT_CTL_ACTIONS = {
    "fetch_result": {
        "summary": "Paging / chunk / host async grep continuation (see cq_start_grep).",
        "payload": "handle | chunk_continuation | host_grep_job_id (+ paging fields)",
    },
    "list_projects": {"summary": "List projects with ids.", "payload": "{}"},
    "select_project": {"summary": "Set active project on server.", "payload": "project_id"},
    "query_db": {"summary": "Read-only SQL by default; allow_write on local/private only.", "payload": "project_id, query, allow_write?, timeout?"},
    "set_sync_mode": {"summary": "cq_send_message wait timeout (0 = off).", "payload": "timeout"},
    "project_status": {"summary": "Health, scan, index cache, problems[].", "payload": "project_id"},
}

FILES_CTL_OVERVIEW = {
    "tool": "cq_files_ctl",
    "summary": (
        "Files, index, grep, direct replace. Chat-based edits (edit/patch/undo) need chat_id. "
        "Mechanical edits: read_file, replace. Background index needs runtime index worker (enabled)."
    ),
    "request_shape": {
        "single": "action + args",
        "batch": "requests: [{action, args}, …]; stop_on_error",
    },
    "legacy_prefix": "cq_",
}

FILES_CTL_ACTIONS = {
    "edit_file": {"summary": "Post <code_file> XML via chat.", "payload": "chat_id, path, content"},
    "patch_file": {"summary": "Post <patch> unified diff via chat.", "payload": "chat_id, path, diff"},
    "undo_file": {"summary": "Post <undo> via chat.", "payload": "chat_id, file_id, time_back?"},
    "list_files": {"summary": "File index; filters modified_since, file_ids; as_tree option.", "payload": "project_id, …"},
    "get_index": {"summary": "Entity index (chat or cached project).", "payload": "chat_id | project_id"},
    "rebuild_index": {"summary": "Build sandwiches index; background=true queues worker.", "payload": "project_id, background?, timeout?"},
    "get_code_index": {"summary": "Deprecated alias of rebuild_index.", "payload": "same as rebuild_index"},
    "grep_entity": {"summary": "Search entity CSV rows in index.", "payload": "project_id, patterns/pattern, match_field?, …"},
    "read_file": {"summary": "Read by DB file_id.", "payload": "file_id"},
    "start_grep": {"summary": "smart_grep chunk or host_fs; then fetch_result.", "payload": "query + search_mode, project_id?, host_path?, …"},
    "grep_logs": {"summary": "Regex scan log sources in project.", "payload": "project_id, query, log_masks, …"},
    "replace": {"summary": "Direct replace by file_id (no chat).", "payload": "project_id, file_id, old, new, is_regex?, …"},
}

EXEC_CTL_OVERVIEW = {
    "tool": "cq_exec_ctl",
    "summary": "Shell in project workspace (cq_exec) or temp script (spawn_script). Same as legacy cq_exec / cq_spawn_script.",
    "request_shape": {
        "single": "action + args",
        "batch": "requests: [{action, args}, …]; stop_on_error",
    },
    "legacy_prefix": "cq_",
}

EXEC_CTL_ACTIONS = {
    "exec": {
        "summary": "Bash in project dir; command string or batch array/object.",
        "payload": "project_id, command, timeout?, continue_on_error?",
    },
    "spawn_script": {
        "summary": "bash/python temp script from command lines.",
        "payload": "project_id, commands[], engine?, script_name?, keep_file?, timeout?",
    },
}


def _ctl_action_index(actions: dict[str, dict[str, str]], base: str) -> list[dict[str, str]]:
    return [
        {"action": name, "summary": block["summary"], "see_tool_ref": f"{base}#{name}"}
        for name, block in actions.items()
    ]


COLLOQUIUM_CTLS: list[dict[str, object]] = [
    {"base": "cq_chat_ctl", "overview": CHAT_CTL_OVERVIEW, "actions": CHAT_CTL_ACTIONS},
    {"base": "cq_project_ctl", "overview": PROJECT_CTL_OVERVIEW, "actions": PROJECT_CTL_ACTIONS},
    {"base": "cq_files_ctl", "overview": FILES_CTL_OVERVIEW, "actions": FILES_CTL_ACTIONS},
    {"base": "cq_exec_ctl", "overview": EXEC_CTL_OVERVIEW, "actions": EXEC_CTL_ACTIONS},
]


HELP_CATALOG = [
    {
        "tool_ref": "cq_help",
        "one_line": CQ_HELP_SELF["summary"],
        "detail": "cq_help",
    },
    {
        "tool_ref": "cq_process_ctl",
        "one_line": PROCESS_CTL_OVERVIEW["summary"],
        "detail": "cq_process_ctl",
        "fragments": [f"cq_process_ctl#{a}" for a in PROCESS_ACTIONS],
    },
    {
        "tool_ref": "cq_docker_ctl",
        "one_line": DOCKER_CTL_OVERVIEW["summary"],
        "detail": "cq_docker_ctl",
        "fragments": [f"cq_docker_ctl#{a}" for a in DOCKER_CTL_ACTIONS],
    },
    {
        "tool_ref": "cq_chat_ctl",
        "one_line": CHAT_CTL_OVERVIEW["summary"],
        "detail": "cq_chat_ctl",
        "fragments": [f"cq_chat_ctl#{a}" for a in CHAT_CTL_ACTIONS],
    },
    {
        "tool_ref": "cq_project_ctl",
        "one_line": PROJECT_CTL_OVERVIEW["summary"],
        "detail": "cq_project_ctl",
        "fragments": [f"cq_project_ctl#{a}" for a in PROJECT_CTL_ACTIONS],
    },
    {
        "tool_ref": "cq_files_ctl",
        "one_line": FILES_CTL_OVERVIEW["summary"],
        "detail": "cq_files_ctl",
        "fragments": [f"cq_files_ctl#{a}" for a in FILES_CTL_ACTIONS],
    },
    {
        "tool_ref": "cq_exec_ctl",
        "one_line": EXEC_CTL_OVERVIEW["summary"],
        "detail": "cq_exec_ctl",
        "fragments": [f"cq_exec_ctl#{a}" for a in EXEC_CTL_ACTIONS],
    },
]
