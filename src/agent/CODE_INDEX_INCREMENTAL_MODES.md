# Code Index Incremental Modes

`project_routes._build_project_index_sync()` supports two incremental modes via env:

- `CORE_INDEX_INCREMENTAL_MODE=fast` (default)
  - Works only with `code_only=true` entries from `attached_files`.
  - Dirty-set is computed against code-base fingerprints only.
  - Fastest mode for normal operation.

- `CORE_INDEX_INCREMENTAL_MODE=refresh`
  - Runs `scan_project_files()` before dirty computation.
  - Recomputes dirty-set from all active files (`code_only=false`), then keeps
    only code-base ids for partial repack.
  - Stores extra `all_file_fingerprints` in cache for subsequent refresh rounds.
  - Useful when non-code files should also refresh fingerprint state by `mtime`.

Related env knobs:

- `CORE_INDEX_ENABLE_INCREMENTAL` — on/off toggle for incremental path.
- `CORE_INDEX_INCREMENTAL_MAX_REVISION` — force full rebuild after N incremental revisions.
- `CORE_INDEX_DIRTY_USE_SIZE` — include `size_bytes` in dirty detection.
