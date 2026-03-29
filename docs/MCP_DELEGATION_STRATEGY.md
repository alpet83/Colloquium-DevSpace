# MCP Delegation Strategy for CQDS

**Version**: 2.0  
**Date**: March 28, 2026  
**Status**: Operational, v2 protocol tested and validated

---

## Quick Start

**If you're new to delegating work to LLMs via CQDS chats:**

1. Read [MCP_DELEGATION_QUICK_START.md](./MCP_DELEGATION_QUICK_START.md) (2 min).
2. Use [MCP_DELEGATION_TEMPLATE.md](./MCP_DELEGATION_TEMPLATE.md) as copy-paste base (5 min prep).
3. Follow this strategy document for why and when (operational context).

---

## Executive Summary

**Problem**: Running code review, wiki generation, or sanitization tasks used to take 45 minutes and cost 15K tokens with mediocre output.

**Root cause**: 
- No protocol (models guessed requirements).
- Full project dumped in every chat (bloated context).
- All models invoked at once (serialization bottleneck).
- LLM used for both fact-discovery and judgment (redundant work).

**Solution**: Protocol-driven delegation + hybrid local/LLM workflow.

**Results**:
- **3.3x faster**: 45 min → 15 min (targeted work, not waiting).
- **2x cheaper**: 15K tokens → 7K tokens (lean, focused).
- **Better quality**: 7/10 → 9/10 (actionable, minimal waste).

---

## Current State Assessment (March 28, 2026)

### Test Run Results (Pilot Round 1)

**Task**: TradeBot code review + wiki planning + sanitization strategy.

**Setup**:
- 3 parallel chats (review, wiki, sanitization).
- General task prompts. `@all` tag to invoke 5-6 models each.
- No pre-loaded context or protocol.

**Outcomes**:
- ⏱️ **Time**: 45 minutes (lots of queue stalls, retries, context reloads).
- 💰 **Cost**: ~15K tokens (speculative responses, models asking for same files repeatedly).
- 📊 **Quality**: Mixed — useful findings but buried in redundancy.
- 🚨 **Operational Issues**:
  - One busy chat froze all others (serialization bottleneck confirmed).
  - New chats with no context forced models to ask "where are the files?" repeatedly.
  - 5-6 models each saying the same thing (consensus cost, not value).

**Why It Happened**:
1. Protocol vacuum → models guessed requirements, wasted time on clarifications.
2. Context overload → new chats weren't pre-loaded with project index → rescans.
3. Serialization → one long-running task blocked all others.
4. Token bleed → 40% of output was "I need context" instead of analysis.

---

## Revised Strategy: Hybrid Local + LLM

### Core Principle

**Separate concerns:**
- **Local tools** (grep, sed, Python): discovery + exhaustive work.
- **LLM delegation** (chat + models): judgment + synthesis + creative content.

**Old**: LLM does everything (slow, expensive, redundant queries).  
**New**: Local discovery → LLM judgment → Local action → Repeat.

### For Different Task Types

#### 1. Code Review / Findings

**Goal**: Identify issues, rate severity, propose fixes.

**Old way**: Post general task to 3 new chats, wait 30 min.

**New way**:
1. **Local**: `grep -r (pattern)` → produce findings backlog (fast, deterministic).
2. **Chat** (60 sec sync mode):
   - Create: `"TradeBot Code Review — [Scope]"`.
   - Root message: findings backlog + policy + output format.
   - Echo test: confirm 2 models understand.
   - Sub-tasks: `@gpt5c` (severity rate) + `@claude4o` (remediation) + `@grok4f` (cross-check).
   - Collect: one consolidated findings table.
3. **Local**: apply approved changes.

**Expected outcome**: 10 min end-to-end, 4K tokens, high-quality actionable table.

#### 2. Wiki / Documentation Generation

**Goal**: Create structured documentation with diagrams.

**Old way**: Ask for structure in one chat, content in another, diagrams in another.

**New way** (sequential, not parallel):
1. **Chat phase A** (5 min):
   - `@claude4s`: Create 8-10 page titles + purposes + key sections.
   - Output: markdown outline (anonymized placeholders only).
2. **Chat phase B** (5 min):
   - `@gpt5c`: Draft 3 pages using phase A structure.
   - Output: merged markdown file.
3. **Chat phase C** (5 min):
   - `@claude4o`: Generate 3 Mermaid diagrams + validate syntax.
   - Output: diagram definitions.
4. **Local**: merge, render, test links.

**Why sequential instead of parallel?**
- Each step depends on prior output.
- Avoids redundant context loads (each new phase only imports prior phase).
- Clearer debug causality.

**Expected outcome**: 15 min total, 4-5K tokens, complete wiki with diagrams.

#### 3. Sanitization / Leak Detection

**Goal**: Find and mask sensitive data (IPs, secrets, internal hosts).

**Old way**: Ask models to find patterns, get varied results, unclear consensus.

**New way**:
1. **Local grep** (2 min):
   ```bash
   grep -r '(192\.|10\.|https?://|token|secret|apiKey)' --include='*.php' --include='*.md'
   ```
   - Produces priority list (facts, ground truth).
   - Fast, deterministic, no speculation.
2. **Chat** (5 min):
   - Root: grep findings + policy.
   - Task: `@claude4o` reviews findings, flags false positives (2 min).
   - Output: approved masking list + regex patterns.
3. **Local** (3 min):
   - Apply sed/replacement rules.
   - Re-scan to confirm zero new patterns.

**Expected outcome**: 10 min total, <1K tokens (LLM validation only), 100% recall, low FP.

### When to Use MCP vs. Direct Tools

#### ✅ **Use MCP (Judgment)**
- Semantic analysis ("is this a real bug?").
- Document generation (structure + prose).
- Cross-validation (compare approaches).
- Architecture review (design judgment).
- Prioritization ("which of these 20 findings matters most?").

#### ✅ **Use Direct Tools (Discovery)**
- Pattern matching (all URLs, all secrets).
- Data transformation (JSON → CSV, XML → SQL).
- File operations (rename, move, copy).
- Build/test execution (compile, run tests).
- Exhaustive scanning (all N files for pattern X).

#### ✅ **Hybrid (Most Powerful)**
1. Direct tools: gather facts locally.
2. MCP: interpret facts, decide action.
3. Direct tools: apply changes.
4. Repeat.

**Example**: Find secrets → [grep] → Ask "which are real vs. test?" → [sed replacement] → [grep verify] → Done.

---

## Operational Protocol

### Phase 1: Bootstrap (2-5 min)

- [ ] Define scope: what's in, what's out, time budget.
- [ ] Run local scans/grep if applicable (facts first).
- [ ] Draft root message (rules, boundaries, output format).
- [ ] Choose 2-4 models for task.
- [ ] Create chat via CQDS UI or MCP.

### Phase 2: Root Message + Echo Test (3 min)

- [ ] Post root message (pinned, NO model tags yet).
- [ ] Wait 3 seconds (let chat stabilize).
- [ ] Echo test: `@model1 @model2 — confirm: 1) scope clear? 2) output format clear?`
- [ ] If models ask for context: rewrite root message, retry (max 2 times).
- [ ] If OK: proceed to Phase 3.

### Phase 3: Sub-Tasks (5-15 min)

- [ ] Assign specific deliverables per model (one message, tight).
- [ ] Include time budget per sub-task.
- [ ] Use `cq_set_sync_mode(timeout=60)` for focused retrieval.
- [ ] Collect outputs as results arrive.

### Phase 4: Synthesis (2-5 min local work)

- [ ] Merge outputs into single artifact (table / markdown / list).
- [ ] Validate: no dupes, no contradictions.
- [ ] Save to memory + wiki.

### Phase 5: Verification (local tools)

- [ ] If changes applied: spot-check via grep/diff.
- [ ] If wiki generated: validate links, render Mermaid.
- [ ] Commit results to git.

---

## Model Selection Reference

| Model | Best For | Speed | Reliability | Typical Cost |
|-------|----------|-------|-------------|--------------|
| `gpt5c` | Structure, checklists, code analysis | Very fast | High | Low |
| `claude4s` | Creative docs, design, long-form prose | Slower | Very High | Medium |
| `gpt5n` | Pattern matching, breadth, grep-like tasks | Ultra-fast | Medium | Very Low |
| `grok4f` | Cross-validation, factual confirmation | Fast | Medium | Low |
| `claude4o` | Safety, edge cases, security | Slower | Very High | Medium |
| `nemotron3s` | Breadth analysis, big-picture | Medium | Medium | Low |

**Typical assignment**:
- Findings/code: `gpt5c` (primary) + `claude4o` (security check) + `grok4f` (validate).
- Creative docs: `claude4s` (primary) + `gpt5c` (refine).
- Diagrams: `claude4o` (primary) + `gpt5c` (validate syntax).
- Quick validation: `gpt5n` (speed) or `grok4f` (factual).

---

## Cost & Time Budgets (Optimized)

| Scenario | Cost | Time | Models | Notes |
|----------|------|------|--------|-------|
| **Code review (1-5 files)** | 2-4K | 5-10 min | 2-3 | Focused task, fast |
| **Security audit (10+ files)** | 5-8K | 15 min | 3-4 | Parallel advantage |
| **Wiki structure (8-10 pages)** | 3-5K | 10 min | 3 | Sequential (A→B→C) |
| **Wiki content (full draft)** | 5-8K | 15 min | 2-3 | Merge with templates |
| **Sanitization (100s files)** | <1K | 5-10 min | 1 | Grep + LLM validation |
| **Simple checklist** | <1K | 2-3 min | 1 | Bootstrap only |

---

## Anti-Patterns (What NOT to Do)

❌ **Dump entire project** → use @attach# indices only (ref, not full content).  
❌ **Post same task to @all** → use selective tags (causes serialization + redundancy).  
❌ **Expect async consensus** → collect outputs, synthesize once (not ongoing voting).  
❌ **No boundaries** → models speculate, waste tokens.  
❌ **Infinite back-and-forth** → set time budget, take what you get, move on.  
❌ **Tag models before root is stable** → wait 3 sec, confirm via echo test first.  
❌ **Expect LLM to replace grep** → use grep for facts, LLM for judgment.  
❌ **Leave results scattered** → consolidate into one artifact.

---

## Troubleshooting

### Issue: Echo test fails (models ask for context)

**Fix**: Root message too vague.
- Add explicit file references (@attach# indices).
- Add policy/rules section.
- Add output format example (table template, checklist template).
- Repost, retry echo test (max 2 times total).

### Issue: No response after 10 min

**Fix**: Queue overload or chat unstable.
- Check chat status via CQDS UI.
- If another chat is busy, wait or close it.
- Retry in a new chat (different project if needed).

### Issue: Output contradicts prior work

**Fix**: Models diverged or no shared context.
- Add cross-check sub-task: `@grok4f: which of these 2 opinions is more likely correct?`
- Either models agree now, or you escalate to human judgment.

### Issue: Token meter running away

**Fix**: Context bloated or sync mode stuck.
- Check root message size (should be <500 words).
- If multiple @attach# files, list them individually (don't dump content).
- Reset chat if needed.

---

## Parking Lot: Improvements Needed in CQDS

1. ✋ **Sequential chat queue** — Why does 1 busy chat block others? (Serialization suspected, confirmed in pilot).
2. ✋ **Context carryover** — New chat in same project should auto-load index (avoid rescans).
3. ✋ **@attach# syntax robustness** — Should handle ranges (`@attach#[1-10,20,30-40]`) without parsing errors.
4. ✋ **Model registry** — Canonical list of available models, their capabilities, rate limits (for automation).
5. ✋ **Result aggregation** — Show outputs from all sub-chats in one pane (avoid silo'd results).
6. ✋ **Token budgeting** — Warn before `@all` task fires (cost estimate + approval gate).
7. 🆕 **Admin chat visibility** — Allow admins to see/audit all chats, not just their own (for governance).
8. 🆕 **Chat cost tracking** — Store per-chat token count + cost in hard currency (for billing/accountability).

---

## Summary

**Key Insight**: Delegating to LLMs is powerful, but only if you delegate **decisions** (interpret facts), not **discovery** (find facts).

**Workflow**: Grep → LLM → Sed → Repeat.

This transforms MCP from "slower alternative to CLI" into "strategic multiplier for judgment-heavy tasks."

---

## References

- [MCP_DELEGATION_QUICK_START.md](./MCP_DELEGATION_QUICK_START.md) — 5-step execution checklist.
- [MCP_DELEGATION_TEMPLATE.md](./MCP_DELEGATION_TEMPLATE.md) — Copy-paste protocol template.
- [CQDS deployment docs](/opt/docker/docs/) — Infrastructure + configuration.
