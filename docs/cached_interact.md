# Cached LLM Interact Design

Date: 2026-03-27
Scope: /app (local working tree at P:/opt/docker/cqds)

## Goals

- Reduce repeated full-context sends during interactive chat.
- Keep correctness when context head changes (index/file changes, old post edits, hidden legacy posts).
- Support majority-path updates where 9/10 changes are tail appends.
- Keep fallback path: full resend of pre-prompt + index + sandwiches whenever cache assumptions break.

## Current Pipeline (as implemented)

### Request and replication chain

1. POST message enters chat route and lands in PostManager.add_post/process_post.
2. process_post may trigger replication via ReplicationManager.check_start_replication.
3. ReplicationManager.replicate_to_llm calls _broadcast.
4. _broadcast builds content blocks via collect_blocks.
5. collect_blocks order:
   - assemble_posts(chat)
   - assemble_files(attached_files)
   - assemble_spans()
6. For each target actor, _recursive_replicate calls interact(ci, rql).
7. interact builds context in build_context and sends single prompt through llm_connection.make_payload(prompt=context).

### Current ordering behavior and implications

Posts:
- scan_history loads posts ordered by p.id ASC (oldest -> newest).
- assemble_posts iterates reversed(history.values()) (newest -> oldest).
- Relevance is reduced by count while iterating, so newest posts keep highest effective priority.

Files and spans:
- Files are appended after posts.
- Spans are appended after files.

Practical effect:
- The assembled block sequence is currently: latest posts first, then file blocks, then span blocks.
- This matches the historical intent of focusing models on fresh conversational tail before full file payload.

### Important technical observation

In build_context loop, token limit check references total_tokens + block_tokens, but total_tokens is never incremented inside the loop.
This means the incremental stop condition does not progress correctly by accumulated block weight.

Action note:
- This should be fixed before hard rollout of cache logic, otherwise any cache gain is masked by unstable trimming behavior.

Checklist:
- [ ] Add total_tokens accumulation during filtered block pass.
- [ ] Add unit test for token-limit trimming order (posts first, then fresh files).

## Why cache must be hybrid (incremental + full fallback)

Context head mutability events:
- File index changed.
- Early history edited/deleted.
- Legacy visibility policy changed (hide old posts on demand).
- Pre-prompt changed.

These events invalidate assumptions about immutable prefix.
For these cases, patching old sent body is unsafe and a full resend is required.

Tail-dominant events:
- New post appended.
- New mention/attach in latest exchange.

These can be handled with delta-safe flow if signatures match.

## Proposed Cache State

Key:
- actor_id
- chat_id
- session_id
- provider/model
- pre_prompt_hash

State object (ContextCacheState):
- provider_session_id (optional, if provider supports session/prompt-cache)
- mode: FULL or DELTA_SAFE
- last_post_id
- pre_prompt_hash
- index_hash
- head_hash
- tail_hash
- last_file_rev
- last_span_rev
- updated_at
- expires_at

Storage:
- Primary: process memory registry + named lock + TTL/LRU.
- Optional persistence: sqlite metadata table for restart diagnostics.

## Invalidation Rules (force FULL)

- pre_prompt_hash changed
- index_hash changed
- edit/delete targets post_id <= cached last_post_id
- visibility policy changed for old messages
- model/provider/reasoning config changed
- cache TTL expired
- provider session rejected or diverged

Checklist:
- [ ] Implement invalidate(chat_id, actor_id, reason).
- [ ] Emit reason-coded metrics for each forced full rebuild.

## Delta-safe Rules

Allow DELTA_SAFE only when all are true:
- New post id strictly greater than cached last_post_id.
- No index/file/spans revision rollback.
- No pre-prompt change.
- No old-history mutation marker.

Checklist:
- [ ] Add deterministic signature function for head/tail.
- [ ] Add strict monotonic check by post_id.

## Prompt Contract for Incremental Updates

Rationale:
- Re-sending a post/file block with an already known numeric id can mean edit/replace.
- Many modern models will infer this from repeated id naturally, but explicit instruction improves consistency.

Recommended pre-prompt addition (concise):

"Context may arrive incrementally. If a block with an existing id appears again, treat it as the latest authoritative revision of that id. Prefer newer revisions over older ones. If revision intent is ambiguous, ask a short clarification before applying irreversible assumptions."

Optional stronger variant:

"When two blocks share same id, keep only the latest occurrence in working memory for reasoning."

Checklist:
- [ ] Add this contract to base pre-prompt.
- [ ] Add model-specific override where needed.

## Suggested Implementation Phases

Phase 1: Instrumentation only
- [x] Add context fingerprints and logging of FULL vs DELTA_SAFE decision.
- [x] No behavior change in transport.
- Status (2026-03-27): implemented in agent/llm_interactor.py.

Phase 2: Local context cache
- Cache head snapshot and reuse it for tail appends.
- Keep outgoing request full for provider compatibility.

Phase 3: Provider-aware optimization
- If provider supports session/prompt-cache, send smaller incremental payloads.
- Else remain on full-send transport with cached local assembly.

Phase 4: Invalidation hooks
- Wire edit/delete/index-change/visibility toggles to cache invalidator.

Phase 5: Rollout and guardrails
- Feature flag per user/session.
- Automatic fallback to FULL on any cache error.

## Metrics to Track

- cache_hit_rate
- delta_safe_rate
- full_rebuild_rate
- avg_context_build_ms
- avg_sent_tokens
- provider_error_rate_after_delta

Checklist:
- [ ] Add telemetry table or structured log sink for cache metrics.
- [ ] Add dashboard counters for FULL fallback reasons.

## Open Questions

- Should cache key include project_id explicitly in addition to chat_id?
- Should hidden-old-post policy be represented as dedicated visibility_revision integer?
- Is provider session lifetime stable enough to reuse across browser sessions?

## Next Practical Step

Validate Phase 1 logs on live chats for 1-2 days, then enable Phase 2 (local context cache reuse).
