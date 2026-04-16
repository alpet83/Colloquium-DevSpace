# Text Editor MCP - Phase 1 Release Notes

Дата: 2026-04-14 (MCP retest)

## Что готово в Phase 1

- Отдельный MCP server entrypoint для `text_editor` (`mcp_text_editor_stdio.py`).
- Session lifecycle: `session_open`, `session_cmd`, deterministic `session_id` от canonical path.
- SQLite storage: `registry.sqlite` + per-session DB, revision history, snapshots.
- Core ops: `get_view`, `move_cursor`, `replace_range`, `replace_regex`, `apply_patch`, `search_indexed`, `diagnostics`, `format_range`, `undo`, `redo`, `save_revision`.
- Response contract: обязательные `current_revision`/`previous_revision` для модификаций, `numbered_lines` + wrap/truncation control.
- Source drift handling: external sync before mutation (`auto_sync`).
- Cleanup: admin-op `cleanup_stale_sessions`.
- Telemetry baseline: request/response bytes, estimated tokens, aggregate report via `telemetry_report`.

## Контракты инструментов

### `session_open`

Вход:

- `path` (required)
- `profile_id?`
- `profile_auto?`
- `response_mode_default?`
- `capabilities_hint?`
- `include_recent_ops?`
- `recent_ops_limit?`

Выход (ключевое):

- `session_id`
- `current_revision`, `previous_revision`
- `session_defaults` (включая `allowed_ops`)
- `capabilities_guide`
- `recent_ops`
- `telemetry`

### `session_cmd`

Общие поля:

- `op` (required)
- `session_id` (required для всех session-bound ops)
- `expected_revision` (required для mutation ops)
- `op_args?`
- `response_mode?` / `response_as?`
- `dry_run?`

Session-bound ops:

- `get_view`, `move_cursor`, `search_indexed`, `diagnostics`
- `replace_range`, `replace_regex`, `apply_patch`, `format_range`
- `undo`, `redo`, `save_revision`

Admin ops (без `session_id`):

- `cleanup_stale_sessions`
- `telemetry_report`
- `assign_workspace` (инициализация `allowed_roots` из `.code/.code-workspace`)
- `policy_show` (read-only текущая policy)
- `op_help` (inline help; source: `mcp-tools/text_editor/OP_HELP.md`; batching via `op_args.ops[]`)

## Error Contract

Все ошибки возвращаются в envelope:

- `class`
- `code`
- `message`
- `retryable`
- `hint?`
- `details`

Ключевые коды Phase 1:

- `revision_mismatch`
- `source_changed_externally`
- `path_not_allowed`
- `file_not_text`
- `revision_not_available_after_compaction`
- `format_failed`

## Тестовый статус

- `test_text_editor_phase1_basic.py`
- `test_text_editor_phase1_ag.py`
- `test_text_editor_phase1_hi_smoke.py`

Текущий локальный прогон: `17 passed`.

CI gate:

- `.github/workflows/text-editor-phase1-tests.yml`

## Что осталось на Phase 2

- Remote FastAPI transport на том же core.
- HTTP/MCP parity tests.
- Command templating (`template_name` / `based_on`) по итогам накопленной telemetry.
- `insert_from_file` / `export_slice`.
