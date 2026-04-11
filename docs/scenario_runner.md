# Scenario Runner Draft (Phase 1 Cache Telemetry)

Date: 2026-04-02  
Scope: CQDS core API + MCP runtime tools (`cq_project_ctl`, `cq_chat_ctl`)

## Purpose

- Execute repeatable, read-only interactive chat scenarios on real project code.
- Produce statistically useful baseline for Phase 1 cache telemetry (`context_cache_metrics`).
- Reuse the same credentials and endpoint config as `mcp-tools`.

## Non-Goals

- No file/code modifications.
- No patch/apply/edit actions.
- No migration or side-effect execution.

## Runtime Inputs

- Core URL / credentials: same source as MCP runtime config.
- `target_project_id` (or project selector policy).
- `actor/model profile` (prefer low-cost model variants for volume).
- Run budget:
  - max scenarios per run
  - max turns per scenario
  - max runtime
  - optional max token budget

## Read-Only Prompt Contract

All test prompts must include a guard clause:

`Analysis only. Review real code and explain findings. Do not propose or execute file/code modifications.`

## Scenario Classes

Each class should run against real modules/files in selected project:

1) `module_risk_review`
- Turn 1: risk review for module X.
- Turn 2: clarify highest-impact risk and evidence.

2) `dependency_flow_review`
- Turn 1: explain dependency/data flow for component X.
- Turn 2: ask likely failure points under load.

3) `test_gap_review`
- Turn 1: identify test gaps in area X.
- Turn 2: ask minimal read-only validation checklist.

4) `regression_hypothesis`
- Turn 1: review recent architecture area, list regression hypotheses.
- Turn 2: rank hypotheses by severity and confidence.

## MCP Protocol (Per Scenario)

1. `cq_project_ctl` action=`select_project` args=`{project_id}`
2. `cq_chat_ctl` action=`create_chat` args=`{description}`
3. `cq_chat_ctl` action=`send_message` turn 1 (read-only prompt)
4. `cq_chat_ctl` action=`wait_reply`
5. `cq_chat_ctl` action=`send_message` turn 2 (follow-up prompt)
6. `cq_chat_ctl` action=`wait_reply`
7. `cq_chat_ctl` action=`get_history`
8. Persist scenario result metadata and DB telemetry snapshot

Notes:
- `wait_reply` is synchronization barrier; do not merge turns without it.
- `requests[]` batching is allowed for independent calls, but keep barriers intact.

## Result Record (Runner Output)

Each scenario run emits one JSON object:

```json
{
  "run_id": "2026-04-02T12:00Z-batchA",
  "scenario_id": "module_risk_review",
  "project_id": 1,
  "chat_id": 1234,
  "model": "cheap-model-profile",
  "turn_count": 2,
  "status": "ok",
  "error": "",
  "started_at": 1712059200,
  "finished_at": 1712059230
}
```

## Telemetry Query Hooks (Phase 1)

After each scenario (or once per batch), query `context_cache_metrics`:

- mode distribution (`FULL` vs `DELTA_SAFE`)
- reason breakdown
- p50/p95 `build_context_ms`
- p50/p95 `sent_tokens`
- provider error split by mode

## Suggested Daily Gate

- total interactions >= 300/day
- each scenario class >= 50 interactions/day
- no unexplained provider_error spike after `DELTA_SAFE`
- stable reason distribution (no sudden jump in unknown/invalid paths)

## Implementation Notes

- Start with a simple Python runner invoking MCP tools sequentially.
- Persist run artifacts into `/app/logs/cache_phase1_runner/`.
- Add retry policy for transient API errors (bounded retries).
- Never retry irreversible operations (not applicable in read-only protocol).
