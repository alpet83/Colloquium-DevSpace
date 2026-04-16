# text_editor op help

Этот файл является источником встроенной справки `op_help`.
Каждая операция описывается отдельным якорным разделом:

- заголовок вида `## op:<name>`
- содержимое: короткая схема и пример вызова

## op:get_view

Requires session: yes  
Mutation: no

`op_args`:
- `cursor_line`: int >= 1 (optional)
- `max_view_lines`: int 1..120 (optional)
- `wrap_width`: int >= 1 (optional)

Example:
```json
{
  "session_id": "<sid>",
  "op": "get_view",
  "response_mode": "numbered_lines",
  "op_args": { "cursor_line": 120, "max_view_lines": 40, "wrap_width": 100 }
}
```

## op:move_cursor

Requires session: yes  
Mutation: no

`op_args`:
- `line`: int >= 1 (optional)
- `col`: int >= 1 (optional)

Example:
```json
{
  "session_id": "<sid>",
  "op": "move_cursor",
  "op_args": { "line": 200, "col": 1 }
}
```

## op:replace_range

Requires session: yes  
Mutation: yes  
Requires expected_revision: yes

`op_args`:
- `line_start`: int >= 1 (required)
- `line_end`: int >= line_start (required)
- `replacement_lines`: string[] (recommended)
- `replacement_text`: string (alternative to replacement_lines)

Example:
```json
{
  "session_id": "<sid>",
  "expected_revision": 12,
  "op": "replace_range",
  "response_mode": "minimal",
  "op_args": { "line_start": 10, "line_end": 12, "replacement_lines": ["a", "b"] }
}
```

## op:replace_regex

Requires session: yes  
Mutation: yes  
Requires expected_revision: yes

`op_args`:
- `pattern`: string regex (required)
- `replacement`: string (required)
- `ignore_case`: bool (optional, default false)
- `max_replacements`: int (optional, <=0 means unlimited)

Example:
```json
{
  "session_id": "<sid>",
  "expected_revision": 12,
  "op": "replace_regex",
  "dry_run": true,
  "response_mode": "minimal",
  "op_args": { "pattern": "\\bTODO\\b", "replacement": "DONE", "max_replacements": 5 }
}
```

## op:apply_patch

Requires session: yes  
Mutation: yes  
Requires expected_revision: yes

`op_args`:
- `patch_text`: unified diff text (required)

Example:
```json
{
  "session_id": "<sid>",
  "expected_revision": 12,
  "op": "apply_patch",
  "response_mode": "minimal",
  "op_args": { "patch_text": "@@ -1,1 +1,1 @@\n-old\n+new" }
}
```

## op:search_indexed

Requires session: yes  
Mutation: no

`op_args`:
- `query`: string (required)

Example:
```json
{
  "session_id": "<sid>",
  "op": "search_indexed",
  "op_args": { "query": "TODO" }
}
```

## op:diagnostics

Requires session: yes  
Mutation: no

`op_args`: none

Example:
```json
{
  "session_id": "<sid>",
  "op": "diagnostics"
}
```

## op:format_range

Requires session: yes  
Mutation: yes  
Requires expected_revision: yes

`op_args`:
- `line_start`: int >= 1 (optional)
- `line_end`: int >= line_start (optional)

Example:
```json
{
  "session_id": "<sid>",
  "expected_revision": 12,
  "op": "format_range",
  "op_args": { "line_start": 1, "line_end": 120 }
}
```

## op:undo

Requires session: yes  
Mutation: yes  
Requires expected_revision: yes

`op_args`:
- `target_revision`: int >= 1 and < current_revision (optional)

Example:
```json
{
  "session_id": "<sid>",
  "expected_revision": 12,
  "op": "undo"
}
```

## op:redo

Requires session: yes  
Mutation: yes  
Requires expected_revision: yes

`op_args`: none

Example:
```json
{
  "session_id": "<sid>",
  "expected_revision": 13,
  "op": "redo"
}
```

## op:save_revision

Requires session: yes  
Mutation: yes  
Requires expected_revision: yes

`op_args`: none

Example:
```json
{
  "session_id": "<sid>",
  "expected_revision": 12,
  "op": "save_revision",
  "response_mode": "minimal"
}
```

## op:op_help

Requires session: no  
Mutation: no

`op_args`:
- `op`: string (optional)
- `ops`: string[] (optional)

Example:
```json
{
  "op": "op_help",
  "op_args": { "ops": ["replace_range", "save_revision"] }
}
```

## op:help_selftest

Requires session: no  
Mutation: no (admin readonly)

`op_args`: none

Example:
```json
{ "op": "help_selftest" }
```

## op:assign_workspace

Requires session: no  
Mutation: admin

`op_args`:
- `workspace_file`: path to `.code-workspace` (required)

Example:
```json
{
  "op": "assign_workspace",
  "op_args": { "workspace_file": "P:/opt/docker/cqds/cqds-cursor.code-workspace" }
}
```

## op:policy_show

Requires session: no  
Mutation: no (admin readonly)

`op_args`: none

Example:
```json
{ "op": "policy_show" }
```

## op:telemetry_report

Requires session: no  
Mutation: no (admin readonly)

`op_args`: none

Example:
```json
{ "op": "telemetry_report" }
```

## op:cleanup_stale_sessions

Requires session: no  
Mutation: admin

`op_args`:
- `stale_after_days`: int >= 1 (optional, default 30)

Example:
```json
{
  "op": "cleanup_stale_sessions",
  "op_args": { "stale_after_days": 30 }
}
```
