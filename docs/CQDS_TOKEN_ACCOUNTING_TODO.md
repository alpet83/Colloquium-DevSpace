# CQDS Token Accounting Todo — Implementation Roadmap

## Overview
Add cost tracking to CQDS chats. Show users and admins how many tokens each conversation consumed and estimated USD cost.

**Timeline**: 4 phases, 7-10 hours total
**Status**: Phase 1 pending, Phases 2-4 not started
**Owner**: DevOps/Backend

---

## Phase 1: Database Schema Extension (30 min)

### Goal
Extend PostgreSQL schema to store token counts and pricing.

### Tasks
1. **Connect to PostgreSQL** (via docker-compose exec):
   ```bash
   cd P:\opt\docker\cqds
   docker-compose exec postgres psql -U postgres
   ```

2. **Execute Schema SQL**:
   ```sql
   -- Add tracking columns to posts table
   ALTER TABLE posts ADD COLUMN IF NOT EXISTS output_tokens INTEGER DEFAULT 0;
   ALTER TABLE posts ADD COLUMN IF NOT EXISTS model_name TEXT DEFAULT 'unknown';
   ALTER TABLE posts ADD COLUMN IF NOT EXISTS input_tokens INTEGER DEFAULT 0;
   
   -- Create pricing table
   CREATE TABLE IF NOT EXISTS token_pricing (
       model_name TEXT PRIMARY KEY,
       input_cost_per_1k DECIMAL(10, 8) NOT NULL,
       output_cost_per_1k DECIMAL(10, 8) NOT NULL,
       created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
   );
   
   -- Populate pricing (adjust rates as needed)
   INSERT INTO token_pricing VALUES
       ('gpt5c', 0.003, 0.006),
       ('claude4s', 0.003, 0.015),
       ('claude4o', 0.002, 0.010),
       ('grok4f', 0.001, 0.002),
       ('grok4c', 0.001, 0.002),
       ('nemotron3s', 0.0005, 0.001)
   ON CONFLICT DO NOTHING;
   ```

3. **Verify**:
   ```sql
   \d posts
   SELECT * FROM token_pricing;
   ```

### Definition of Done
- [ ] All 3 columns added to posts table
- [ ] token_pricing table created
- [ ] 6 LLM models have pricing entries
- [ ] Query returns correct schema

---

## Phase 2: Data Collection (1-2 hours)

### Goal
Capture token usage from each LLM response and write to database.

### Where
File: `P:\opt\docker\cqds\agent\llm_api.py` (or `llm_interactor.py` if LLM calls are centralized there)

### Changes
1. **After LLM API call**, extract token metadata:
   ```python
   # Pseudo-code
   response = await call_llm_api(messages, model=model_name)
   
   # Extract usage info
   output_tokens = response.get('usage', {}).get('output_tokens', 0)
   input_tokens = response.get('usage', {}).get('input_tokens', 0)
   model_name = response.get('model_name', model_name)
   ```

2. **Insert into posts table** (after creating the post row):
   ```python
   db.execute("""
       UPDATE posts 
       SET output_tokens = %s, 
           input_tokens = %s, 
           model_name = %s
       WHERE id = %s
   """, (output_tokens, input_tokens, model_name, post_id))
   ```

3. **Log for debugging**:
   ```python
   logger.debug(f"Chat {chat_id} post {post_id}: {input_tokens} in, {output_tokens} out, model={model_name}")
   ```

### Testing
- Run a test chat (ask a simple question)
- Check posts table: `SELECT * FROM posts WHERE chat_id = 1 ORDER BY id DESC LIMIT 1;`
- Verify columns are populated with non-zero values

### Definition of Done
- [ ] Data collection code merged
- [ ] At least 3 test chats have token data in posts table
- [ ] No errors in logs for token insertion
- [ ] Model names correctly recorded

---

## Phase 3: /chat/stats Endpoint (2-3 hours)

### Goal
Implement query endpoint to return per-chat cost metrics.

### Where
File: `P:\opt\docker\cqds\agent\routes\chat_routes.py` (route already stubbed at line ~250)

### Implementation
Replace stub with actual logic:

```python
@router.get("/chat/{chat_id}/stats")
async def get_chat_stats(chat_id: int, db: Database = Depends(get_db)):
    """
    Returns token usage and estimated cost for a specific chat.
    """
    # Query token usage from posts
    usage_row = await db.fetchrow("""
        SELECT
            COUNT(*) as total_messages,
            COALESCE(SUM(input_tokens), 0) as total_input_tokens,
            COALESCE(SUM(output_tokens), 0) as total_output_tokens,
            ARRAY_AGG(DISTINCT model_name) FILTER (WHERE model_name IS NOT NULL) as models_used,
            MIN(timestamp) as first_message,
            MAX(timestamp) as last_message
        FROM posts
        WHERE chat_id = %s
    """, chat_id)
    
    if not usage_row:
        return {"error": "Chat not found"}
    
    # Calculate cost from pricing table
    cost_row = await db.fetchrow("""
        SELECT
            COALESCE(SUM(
                p.output_tokens * tp.output_cost_per_1k / 1000.0 +
                p.input_tokens * tp.input_cost_per_1k / 1000.0
            ), 0) as total_cost_usd
        FROM posts p
        LEFT JOIN token_pricing tp ON p.model_name = tp.model_name
        WHERE p.chat_id = %s
    """, chat_id)
    
    # Build response
    return {
        "chat_id": chat_id,
        "total_messages": usage_row['total_messages'],
        "total_input_tokens": int(usage_row['total_input_tokens']),
        "total_output_tokens": int(usage_row['total_output_tokens']),
        "estimated_cost_usd": float(cost_row['total_cost_usd']),
        "models_used": list(usage_row['models_used']) if usage_row['models_used'] else [],
        "duration_minutes": (usage_row['last_message'] - usage_row['first_message']) / 60 if usage_row['last_message'] else 0,
        "status": "ready"
    }
```

### Testing
```bash
curl -X GET "http://localhost:8000/chat/1/stats"
# Expected:
# {
#   "chat_id": 1,
#   "total_messages": 5,
#   "total_input_tokens": 1250,
#   "total_output_tokens": 890,
#   "estimated_cost_usd": 0.0342,
#   "models_used": ["claude4s", "gpt5c"],
#   "status": "ready"
# }
```

### Definition of Done
- [ ] Endpoint returns 200 with correct JSON schema
- [ ] Cost calculation matches manual verification
- [ ] Works for at least 3 test chats
- [ ] Edge cases handled (empty chats, unknown models, etc.)

---

## Phase 4: Admin Dashboard (4+ hours, Optional)

### Goal
UI for monitoring per-user and per-chat costs.

### Features
1. **Admin page: /admin/billing**
   - Table of all chats with costs
   - Sort by date, cost, model
   - Filter by user or date range

2. **Admin page: /admin/billing/users**
   - Per-user monthly totals
   - Trend chart (costs over time)
   - Top 10 expensive users

3. **Export functionality**
   - CSV download of monthly billing

4. **Alerts** (optional)
   - Notify if chat exceeds $1 threshold
   - Daily admin summary email

### Implementation Plan
1. Add frontend pages (Vue components)
2. Add backend queries for aggregation
3. Add charts (e.g., ChartJS or D3)
4. Add CSV export endpoint
5. Integrate with alert system

### Timeline
- Core pages: 2 hours
- Charts and filtering: 1.5 hours
- Export and alerts: 1 hour

### Definition of Done
- [ ] All admin pages load without errors
- [ ] Data displayed is accurate and up-to-date
- [ ] Export functionality works
- [ ] Performance acceptable (<500ms per page)

---

## Quick Reference

### Pricing Model (Default)
| Model | Input ($/1K) | Output ($/1K) |
|---|---|---|
| gpt5c | $0.003 | $0.006 |
| claude4s | $0.003 | $0.015 |
| claude4o | $0.002 | $0.010 |
| grok4f | $0.001 | $0.002 |
| grok4c | $0.001 | $0.002 |
| nemotron3s | $0.0005 | $0.001 |

### SQL Queries (Handy Reference)
```sql
-- Total cost for a chat
SELECT SUM(p.output_tokens * tp.output_cost_per_1k / 1000 + 
           p.input_tokens * tp.input_cost_per_1k / 1000) as cost_usd
FROM posts p
LEFT JOIN token_pricing tp ON p.model_name = tp.model_name
WHERE p.chat_id = 123;

-- Breakdown by model
SELECT 
    model_name,
    COUNT(*) as num_msgs,
    SUM(input_tokens) as total_input,
    SUM(output_tokens) as total_output,
    SUM(output_tokens * tp.output_cost_per_1k / 1000) as output_cost
FROM posts p
LEFT JOIN token_pricing tp ON p.model_name = tp.model_name
WHERE p.chat_id = 123
GROUP BY model_name;

-- Monthly billing for user
SELECT 
    DATE_TRUNC('month', to_timestamp(timestamp))::date as month,
    SUM(output_tokens * tp.output_cost_per_1k / 1000 + 
        input_tokens * tp.input_cost_per_1k / 1000) as cost_usd
FROM posts p
LEFT JOIN chats c ON p.chat_id = c.id
LEFT JOIN token_pricing tp ON p.model_name = tp.model_name
WHERE c.user_id = 42
GROUP BY DATE_TRUNC('month', to_timestamp(timestamp))
ORDER BY month DESC;
```

### Files to Modify
| Phase | File | Lines | Type |
|---|---|---|---|
| 1 | (PostgreSQL) | SQL | Schema |
| 2 | llm_api.py / llm_interactor.py | ~10-20 | Data collection |
| 3 | chat_routes.py | ~50-70 | Endpoint |
| 4 | (Frontend) | ~200-300 | UI |

---

## Known Issues & Workarounds

### Issue: docker-compose exec -T not available
**Workaround**: Use direct psql connection or check ~/.pgpass for auto-auth

### Issue: Token counts not in response metadata
**Check**: LLM API documentation for the model you're using. Some APIs may return usage in different fields.

### Issue: Cost calculation seems wrong
**Debug**:
1. Verify pricing table has entries: `SELECT * FROM token_pricing;`
2. Check sample post row: `SELECT * FROM posts LIMIT 1;`
3. Calculate manually: `SELECT 1000 * 0.003 / 1000 / 1000;` (should be tiny number)

---

## Deployment Checklist
- [ ] Phase 1 SQL executed and verified
- [ ] Phase 2 code merged to main
- [ ] Phase 2 tested with at least 3 real chats
- [ ] Phase 3 endpoint returns correct data for test chats
- [ ] Admin review & approval from code owner
- [ ] Staging deployment and smoke test
- [ ] Production deployment with rollback plan ready
