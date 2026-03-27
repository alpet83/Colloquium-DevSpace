# TTL Checklist (file links)

## Preconditions
- Core container restarted with latest code.
- Project id is known (examples below use `project_id=1`).
- Test file exists in project working dir (example: `test_async_indexing.py`).

## 1) Verify DB columns
Run SQL:

```sql
SELECT column_name
FROM information_schema.columns
WHERE table_schema='public' AND table_name='attached_files'
ORDER BY ordinal_position;
```

Expected:
- Columns include `missing_ttl` and `missing_checked_ts`.

## 2) Get baseline row for target file
Run SQL:

```sql
SELECT id, file_name, missing_ttl, missing_checked_ts
FROM attached_files
WHERE file_name LIKE '%test_async_indexing.py'
ORDER BY id
LIMIT 1;
```

Expected:
- Row exists.
- `missing_ttl` is at max (default `3`).
- Save `id` for later checks.

## 3) Baseline visibility through file index
Call:
- `cq_list_files(project_id=1, file_ids='<saved_id>')`

Expected:
- Entry is returned.

## 4) Simulate temporary missing file
Call (inside project):
- `cq_exec(project_id=1, command='mv test_async_indexing.py test_async_indexing.py.ttltest.bak')`

Expected:
- Command succeeds.
- File is absent on disk.

## 5) Trigger probe and validate hidden behavior
Call:
- `cq_list_files(project_id=1, file_ids='<saved_id>')`

Expected:
- Empty result (degraded link hidden from index).

Optional SQL check:

```sql
SELECT id, file_name, missing_ttl, missing_checked_ts
FROM attached_files
WHERE id=<saved_id>;
```

Expected:
- `missing_ttl` is unchanged or decreased depending on probe cooldown.
- No row deletion.

## 6) Restore file
Call:
- `cq_exec(project_id=1, command='mv test_async_indexing.py.ttltest.bak test_async_indexing.py')`

Expected:
- File exists again.

## 7) Force rescan + rebuild
Call:
- `cq_rebuild_index(project_id=1, timeout=30)`

Expected:
- Rebuild returns index.
- Pre-scan is executed before build.

## 8) Confirm TTL recovery and stable file id
Call:
- `cq_list_files(project_id=1, file_ids='<saved_id>')`
- SQL:

```sql
SELECT id, file_name, missing_ttl
FROM attached_files
WHERE id=<saved_id>;
```

Expected:
- Entry is visible again.
- Same `id` as baseline (no id churn).
- `missing_ttl` restored to max (`3` by default).

## 9) Inspect logs
Look in:
- `logs/fileman/YYYY-MM-DD/fileman_HHMM.log`
- `logs/projectman/YYYY-MM-DD/projectman_HHMM.log`

Expected markers:
- `TTL-degrade link ... ttl=X->Y`
- `TTL-recover link ... ttl=Y->MAX`
- `GET /project/code_index ... scan_files=...`

## 10) Check project status API
Call:
- `GET /api/project/status?project_id=1`

Expected:
- JSON includes top-level `status` and `problems` array.
- On issues, `problems` contains structured records with `code`, `severity`, `message`, `details`.
- Frontend can show warning icon if `problems.length > 0`.
