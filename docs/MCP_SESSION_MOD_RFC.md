# RFC: MCP session_mod + Filtered op_help (Token-Efficient Delegation)

Status: Draft
Date: 2026-04-15
Owner: MCP Platform / CQDS Integration

## 1. Why this RFC

Current pain points:
- Full contextual help can become too large and expensive in tokens.
- Repeated command execution requires verbose payload repetition.
- High chat interaction frequency is rate-limited/penalized by server policy.

Primary goal:
- Reduce total premium interactions and token volume while preserving safe, explicit editing semantics.

## 2. Non-Negotiable Constraint

`expected_revision` must remain explicit and mandatory for mutation operations.

Rationale:
- It enforces model focus on the exact observed state.
- It prevents stale-context writes.
- It preserves deterministic conflict handling.

Therefore, this RFC explicitly rejects any auto-refresh or implicit update of `expected_revision` for mutation commands.

## 3. Proposal A: `session_mod` as a peer tool to `session_cmd`

### 3.1 Design intent

`session_mod` is a first-class MCP tool (same level as `session_cmd`), not an internal op-contract wrapper.

Goals for the 90% path:
- mutate the last command payload,
- keep request/response shape minimal,
- avoid nested patch envelopes.

### 3.2 Minimal input contract

As a standalone MCP tool, `session_mod` receives a flat argument object (no inner `op: session_mod`).

Example (90% path):

```json
{
  "derived_from": "last_success",
  "expected_revision": 42,
  "line_start": 120,
  "line_end": 122,
  "replacement_text": "..."
}
```

Semantics:
- `derived_from` selects base command:
  - `last_success` (default)
  - command hash/id anchor (explicit earlier command)
- Operation type is inherited from the base command by default.
- Field removal uses explicit `null` (for example, `"response_mode": null`).
- For mutation targets, `expected_revision` is mandatory and explicit.

Optional controls for less frequent cases:
- `run_mode` (`preview` | `execute`)
- `target_op` (only if caller explicitly wants to override inherited operation)

### 3.3 Minimal output contract

Default response (90% path):

```json
{
  "ok": true,
  "executed": true,
  "command_id": "cmd_...",
  "result": {"...": "..."}
}
```

Extended diagnostics only on explicit request (`verbose=true`) or error:
- `base_command_id`
- `resolved_payload`
- `override_diff`

This keeps successful-path responses compact and cheap.

## 4. Proposal B: Filtered `op_help`

Add selective help retrieval to avoid manual-sized responses.

### 4.1 Parameters

- `ops`: string[]
- `sections`: string[] where allowed values are:
  - `contract`
  - `op_args_schema`
  - `templates`
  - `examples`
  - `errors`
  - `constraints`
- `verbosity`: `brief | normal | full`
- `output_mode`: `compact | structured_json`

### 4.2 Recommended defaults

- Default `verbosity = brief`
- Default `sections = ["contract", "templates"]`

This should satisfy most practical calls without token bloat.

## 5. Black-Box client model (GitHub/Copilot context operators)

This system must assume that context assembly and model routing on the client side are opaque.

Implications:
- MCP cannot control or depend on remote cache policy in GitHub infrastructure.
- MCP cannot assume deterministic retention of help payload in model memory.
- The protocol must not rely on model-side memory persistence for correctness.

Protocol rule:
- Server-side safety and determinism must be self-sufficient.
- If required execution context is not explicitly present in request semantics, operation must fail closed and request rehydrate.

## 6. Safety and determinism

- No auto-update of `expected_revision` for mutations.
- `null` removal semantics must be strict and explicit.
- Every `session_mod` execution is logged with:
  - `base_command_id`
  - `derived_command_id`
  - compact override object

## 7. Operational model (simplified)

Default workflow:
1. Select operation.
2. Request focused help (`op_help`) for that operation only.
3. Execute operation(s).
4. Repeat with next focused operation.

Rationale:
- In typical short iterative loops, this keeps payloads small enough without adding heavy cache/version machinery.
- Full help over all operations should be treated as exceptional, not normal flow.

## 8. Optional future optimization (deferred)

Help hash/version and delta delivery are deferred by default.

Decision note:
- These features may optimize transport volume.
- They are not required for correctness.
- They should be introduced only if telemetry shows meaningful gains under real load.

## 9. Success metrics

Target improvements after rollout:
- 25-40% fewer chat interactions per delegated task.
- 20-35% lower median tokens per task.
- 30% lower retry rate caused by malformed payloads.

## 10. Phased rollout

Phase 1:
- `session_mod` preview + execute (minimal contract)
- filtered `op_help`
- simplified iterative workflow as primary path

Phase 2 (optional, telemetry-gated):
- help hash/version cache protocol
- compact default response path hardening

Phase 3:
- telemetry dashboard for interaction/token reduction
- CQDS delegation templates aligned to `session_mod`

## 11. Acceptance criteria

1. A repeated `replace_range` can be re-executed by patching only changed fields, without re-sending full original payload.
2. Mutation via `session_mod` fails if `expected_revision` is absent.
3. `op_help` with filtered sections returns substantially smaller output than full help.
4. Successful `session_mod` default output remains compact (no heavy wrappers).
5. Error path returns corrective minimal example payload.
6. Correctness and safety do not depend on model-side memory or cache behavior.
