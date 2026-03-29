# CQDS Delegation Template (Copy-Paste Base)

Use this template to bootstrap new delegation tasks. Adapt the sections for your project.

---

## Full Template (Root Message)

```
PROJECT: [ProjectName]
TASK: [ConciseName]
SCOPE: [What's in, what's out, file count, time limit]

CONTEXT:
- Project index: @attach#[ID_of_index_file]
- Key files: @attach#[ID1], @attach#[ID2], @attach#[ID3]
- Documentation: [Link to policy/checklist if external]

BOUNDARIES:
- File list limited to: [pattern or list of names]
- Do NOT modify: [runtime configs, secrets, etc]
- Time budget: [e.g., 10 minutes total]
- Output format: [table/checklist/markdown/diagram/bullet list]

POLICY/RULES:
- [Key rule 1 from project]
- [Key rule 2]
- Severity scale: critical | high | medium | low
- Anonymization: Replace real values with [HOST_CORE], [ACCOUNT_A], [SERVICE_API], etc.

EXPECTED OUTPUT:
1. [Specific table/list structure]
2. [Specific checklist format]
3. [Summary format or conclusion]
```

---

## Example 1: Code Review (Fast)

```
PROJECT: TradeBot
TASK: Security Review — sigsys-ts Signals Service
SCOPE: Backend files only (TS modules in /backend/src), focus on auth/validation/secrets. 
Exclude: migrations, tests, dist/.
Time: 10 min.

CONTEXT:
- Project index: @attach#42
- Key auth files: @attach#50, @attach#51, @attach#52
- Security policy: [Link to security_audit.md]

BOUNDARIES:
- Analyze only: @attach#50-52
- Do NOT modify config files or .env.
- Time budget: 10 minutes
- Output format: Findings table (severity | file | issue | impact | fix)

POLICY/RULES:
- No hardcoded secrets allowed (env vars only).
- JWT_SECRET must be validated at startup.
- Admin checks must use UserExternalService.isAdmin().
- All external API calls require JWT auth guard.

EXPECTED OUTPUT:
1. Findings table (CSV-like format)
2. Severity summary (count by critical/high/medium/low)
3. Top 3 recommendations for immediate action
```

**Echo test**:
```
@gpt5c @claude4o — confirm you read above and understand:
1. Scope is *backend only, auth/validation focus*?
2. Output format is *table with severity/file/issue/impact/fix*?
1-line each.
```

**Sub-tasks**:
```
@gpt5c: Analyze @attach#50-52 for security/logic issues.
Output: table (severity | file | issue | impact | fix).
Time: 5 min.

@claude4o: Review @gpt5c findings. Rate confidence + suggest testing checklist (5 steps).
Time: 5 min.

@grok4f: Cross-check both outputs. False positives? Missed patterns?
Output: bullet list (confirmed | hypothesis | false_positive).
```

---

## Example 2: Wiki Generation (Creative)

```
PROJECT: TradeBot
TASK: Architecture Documentation (Anonymized)
SCOPE: Create 8-10 page wiki covering system design, deployment, API contracts.
All real hostnames/IPs replaced with placeholders.
Time: 15 min (sequential phases).

CONTEXT:
- Architecture policy: [Link to anonymization policy]
- Example pages: [Link to template structure]

BOUNDARIES:
- Use only anonymized placeholders: [HOST_CORE], [SERVICE_API], [EXCHANGE_FEED], etc.
- Do NOT include: deployment credentials, real URLs, secret keys.
- Phases: A (structure) → B (content) → C (diagrams).
- Time per phase: 5 min.

POLICY/RULES:
- All infrastructure references anonymized.
- Diagrams use Mermaid syntax (valid + renderable).
- Focus on decision rationale, not implementation details.

EXPECTED OUTPUT:
1. Phase A: Markdown outline (page titles + sections + purposes).
2. Phase B: 3-5 draft pages (Overview, Architecture, API Contracts).
3. Phase C: 3 Mermaid diagrams (components, data flow, deploy cycle).
```

**Echo test**:
```
@claude4s @gpt5c — confirm:
1. Output is *anonymized* (no real IPs/hosts)?
2. Format is *3 phases A→B→C, each 5 min*?
1-line each.
```

**Sub-tasks**:
```
@claude4s: Create wiki structure (8-10 page titles + sections + purposes).
Output: markdown outline with anonymization rules applied.
Time: 5 min.

@gpt5c: Draft 3-5 pages (Overview, Architecture, API Contracts).
Use @claude4s structure. All infrastructure anonymized.
Time: 10 min.

@claude4o: Generate 3 Mermaid diagrams (components, data flow, deploy cycle).
Validate syntax + anonymization.
Time: 5 min.
```

---

## Example 3: Sanitization/Leak Detection (Fast + LLM Validation)

```
PROJECT: TradeBot
TASK: Leaked Infrastructure Validation
SCOPE: Review detected patterns (IPs, hostnames, secrets) in 6 key files.
Validate: real leak vs. false positive vs. intentional placeholder?
Time: 10 min (2 min grep + 5 min LLM + 3 min local apply).

CONTEXT:
- Detected patterns (grep results): [inline table or @attach# reference]
- Sanitization policy: [Link to masking rules]

BOUNDARIES:
- LLM validates ONLY (no file modifications).
- Local tools apply replacements.
- Do NOT touch: test data, fixtures, documentation examples.
- Time budget: 5 min LLM validation.

POLICY/RULES:
- Severity: critical (credentials), high (internal IPs), medium (hostnames), low (URLs).
- False negative risk: prefer over-masking.
- Pattern replacement: [192.168.x.x] → [HOST_CORE], etc.

EXPECTED OUTPUT:
1. Validation table (file | line | pattern | is_real_leak | severity | replacement).
2. Approved masking list (pattern → regex → replacement).
3. Confirmation: "All findings validated, ready to apply."
```

**Echo test**:
```
@claude4o — confirm:
1. Validating 6 files for infrastructure leaks?
2. OUTPUT is *table with validation*, NOT file modification?
1 line.
```

**Sub-tasks**:
```
@claude4o: Review detected patterns in [@attach#X, @attach#Y].
For each: is this a real leak, false positive, or intentional?
Output: table (file | line | pattern | is_real_leak | severity | replacement_value).
Time: 5 min.
```

---

## Example 4: Cross-Validation / Troubleshooting

**When outputs contradict or you're unsure:**

```
@grok4f: Two models disagree on [issue X]. 
- Model A says: [claim A].
- Model B says: [claim B].
Which is more likely correct? Why?
Output: 1 paragraph + recommendation (A/B/escalate_to_human).
```

---

## Customization Checklist

**Before posting root message:**

- [ ] Project name filled in.
- [ ] Task name concise (1-3 words).
- [ ] Scope explicitly lists what's in and out.
- [ ] @attach# file IDs are valid references.
- [ ] Boundaries section includes "do not modify" list.
- [ ] Policy/rules are explicit, not vague.
- [ ] Expected output format has concrete structure (table template, checklist format, etc).
- [ ] Time budget is realistic (5-15 min typically).

---

## Automation Hints (For MCP Integration)

```python
# Bootstrap in code
chat_id = cq_create_chat(description="[TaskName]")
cq_send_message(chat_id, root_message)  # NO model tags
time.sleep(3)

# Echo test
cq_send_message(chat_id, "@gpt5c @claude4o — confirm scope + format?")
status = cq_wait_reply(chat_id, timeout=60)

# Sub-tasks (if ok)
cq_send_message(chat_id, sub_tasks)
results = cq_get_history(chat_id)

# Close/cleanup
print(results)
```

---

## Typical Cost/Time (from Pilot)

| Scenario | Cost | Time | Models | Notes |
|----------|------|------|--------|-------|
| Code review (1-5 files) | 2-4K | 5-10 min | 2-3 | Fast, targeted |
| Wiki structure | 3-5K | 10 min | 3 | Sequential (A→B→C) |
| Sanitization (100s files) | <1K | 10 min | 1 | Grep-driven + validation |
| Simple checklist | <1K | 2 min | 1 | Bootstrap only |

---

## Troubleshooting Template Issues

**Q: Echo test failed, model asks for files?**  
A: Root message too vague. Add more detail to boundaries/policy sections. Re-post. Max 2 retries.

**Q: Output is missing one section?**  
A: Models may have interpreted output format differently. For next round, include a literal template.

**Q: Outputs contradict each other?**  
A: Add cross-check sub-task: `@grok4f: which is correct, reason?`

**Q: Cost running away?**  
A: Check root message size (<500 words). Trim. Reset chat if needed.

---

## References

- [MCP_DELEGATION_QUICK_START.md](./MCP_DELEGATION_QUICK_START.md) — 5-step execution flow.
- [MCP_DELEGATION_STRATEGY.md](./MCP_DELEGATION_STRATEGY.md) — Why and when (operational context).
- [CQDS runtime documentation](/opt/docker/docs/) — Infrastructure.
