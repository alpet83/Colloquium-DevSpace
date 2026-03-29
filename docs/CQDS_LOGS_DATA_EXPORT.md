# CQDS Docker Logs & Data Export Architecture

## Overview

CQDS (`Colloquium DevSpace`) already has **comprehensive host data export** configured. All runtime logs, project data, and database files are accessible on the host filesystem.

## Current Volume Configuration

All CQDS services use **bind mounts** (not named volumes) for durability and direct access:

| Service | Host Path | Container Path | Purpose |
|---------|-----------|-----------------|---------|
| **colloquium-core** | `./data/` | `/app/data/` | SQLite DB, indices, cache |
| — | `./logs/` | `/app/logs/` | Core service logs |
| — | `./projects/` | `/app/projects/` | User project files & indices |
| — | `./agent/` | `/app/agent/` | Agent state & config |
| **mcp-sandbox** | `./logs/` | `/app/logs/` | MCP server logs |
| — | `./projects/` | `/app/projects/` | Sandwich packs & indices |
| — | `/app/sandwiches` | (unnamed) | Ephemeral sandwich cache |
| **postgres** | `./data/pgdata/` | `/var/lib/postgresql/data/` | PostgreSQL database |
| — | `p:/opt/data/backups/pg/` | `/backups/pg/` | Database backups (external!) |
| **frontend** | `./logs/` | `/app/logs/` | Frontend server logs |
| **nginx-router** | (config only) | `/etc/nginx/conf.d/` | Nginxconfig (readonly) |

## Directory Structure

```
/app/projects/cqds/
├── data/
│   ├── multichat.db             # Colloquium chat database (read-only after init)
│   └── ...                      # Project caches, indices
├── logs/
│   ├── colloquium_mcp_tool.runtime.log  # My tool invocations (high-volume!)
│   ├── copilot_tool_*.log
│   ├── mcp_*.log                # MCP server operations
│   ├── chat_*.log               # Per-chat processing
│   ├── gpt5n_context.stats      # Actor context measurements
│   ├── gpt5c_context.stats
│   ├── grok4f_context.stats
│   ├── exception.log            # Error backtraces
│   └── ...                      # Agent, interactor logs
├── projects/
│   ├── 35/                      # trading-platform-php
│   ├── 36/                      # alpet-libs-php
│   └── ...
├── agent/
│   ├── server.py
│   └── [dynamically deployed agents]
└── data/pgdata/
    ├── base/                    # PostgreSQL catalogs
    └── [tables, indices]
```

## Log Volume Analysis

### Log Types & Growth

1. **colloquium_mcp_tool.runtime.log** (225 KB observed)
   - **Frequency**: ~1 log entry per tool invocation
   - **Growth**: ~1 entry per 10s with active Copilot usage
   - **Retention**: Rotate daily or weekly

2. **gpt5n/gpt5c/grok4f_context.stats** (2-3 KB each)
   - **Frequency**: One per actor task completion
   - **Growth**: ~100+ entries/day under heavy use
   - **Purpose**: Token accounting, recursion analysis
   - **Retention**: Archive after 30 days

3. **exception.log** (size varies)
   - **Growth**: Only on errors (ideally minimal)
   - **Purpose**: Debugging deployment issues
   - **Retention**: Keep indefinitely (compress old)

4. **mcp_*.log** (500 KB+ observed in exec.stdout)
   - **Frequency**: Per MCP sandbox operation
   - **Growth**: ~1 MB/week typical usage
   - **Purpose**: MCP protocol debugging
   - **Retention**: Rotate weekly

## Querying Logs from Host

### Direct Access (Linux/WSL)

```bash
# Tail real-time logs
tail -f /app/projects/cqds/logs/colloquium_mcp_tool.runtime.log

# Search for specific task
grep "cq_exec\|cq_query_db" /app/projects/cqds/logs/*.log | head -20

# Count occurrences
grep -c "chat_41" /app/projects/cqds/logs/*.log

# Full context stats review
cat /app/projects/cqds/logs/gpt5c_context.stats | tail -10
```

### Windows PowerShell

```powershell
# Tail logs in real-time
Get-Content "p:\opt\docker\cqds\logs\colloquium_mcp_tool.runtime.log" -Wait

# Search for patterns
Select-String "cq_exec" "p:\opt\docker\cqds\logs\*" | Select-Object -Last 20

# Count by actor
@('gpt5n', 'gpt5c', 'grok4f') | % { $count = (Select-String $_ "p:\opt\docker\cqds\logs\*" | Measure-Object).Count; Write-Host "$_`: $count matches" }
```

## Data Backup & Recovery

### PostgreSQL Backups

CQDS is configured to backup PostgreSQL to `p:/opt/data/backups/pg/` (external to container, on host).

```bash
# List backups
ls -la p:/opt/data/backups/pg/

# Manual backup (while running)
docker exec cqds-postgres pg_dump -U cqds cqds > cqds-backup-$(date +%Y%m%d-%H%M%S).sql

# Restore from backup
docker exec -i cqds-postgres psql -U cqds cqds < cqds-backup-YYYYMMDD-HHMMSS.sql
```

### Project Data Preservation

User project files are stored in `./projects/` as bind mounts:

```bash
# Backup all projects
tar -czf cqds-projects-backup-$(date +%Y%m%d).tar.gz ./projects/

# Restore projects
tar -xzf cqds-projects-backup-YYYYMMDD.tar.gz
```

## Monitoring & Cleanup

### Check Disk Usage

```bash
# Total CQDS footprint
du -sh /app/projects/cqds/

# Breakdown by component
du -sh /app/projects/cqds/data/postgreaql/data/
du -sh /app/projects/cqds/logs/
du -sh /app/projects/cqds/projects/
du -sh /app/projects/cqds/agent/
```

### Archive Old Logs

```bash
# Compress logs older than 30 days
find /app/projects/cqds/logs -name "*.log" -mtime +30 -exec gzip {} \;

# Delete context.stats older than 90 days
find /app/projects/cqds/logs -name "*context.stats" -mtime +90 -delete
```

### Log Rotation Policy (Recommended)

Create `/etc/logrotate.d/cqds`:

```
/app/projects/cqds/logs/*.log {
    daily
    rotate 14
    compress
    missingok
    notifempty
    create 0644 root root
}
```

## Integration with Copilot Workflows

### MCP Tool Logging

Every `cq_*` tool call is logged to `colloquium_mcp_tool.runtime.log`:

```
[2026-03-28 14:25:33.102] cq_exec(project_id=35) → command="grep -n ..."
[2026-03-28 14:25:33.847] cq_exec result: 3 matches
[2026-03-28 14:25:34.102] cq_readline_index(project_id=36) → etag="abc123"
```

**Use for:**
- Audit trail of tool usage
- Performance analysis (response times)
- Billing/cost attribution (per tool, per project)
- Troubleshooting MCP connectivity issues

### Actor Context Measurement

Each actor task logs context statistics:

```json
{
  "chat_id": 41,
  "actor": "gpt5c",
  "post_id": 128,
  "pre_prompt_tokens": 3241,
  "posts_tokens": 5900,
  "index_tokens": 426,
  "total_tokens": 9401,
  "rql_recursion_level": 4,
  "duration_ms": 3847
}
```

**Metrics tracked:**
- Token budget utilization
- Recursion depth (health check for infinite loops)
- Response latency
- Actor assignment frequency

## Example: Analyzing Chat #41 Performance

```bash
# Log analysis command
grep "chat_41\|gpt5c\|gpt5n\|grok4f" /app/projects/cqds/logs/colloquium_mcp_tool.runtime.log | head -50

# Or via cq_grep filter (from CQDS chat)
@cqds /grep "chat_41" /app/logs/*.log --context=3
```

**Expected output:**
- Tool invocations for index rebuild
- Context measurements per actor
- RQL recursion progression (should not exceed 5)
- Actor response times (~4-5s typical)

## Secrets in CQDS

**Important**: CQDS does NOT use `pass` integration (unlike trading-platform-php).

- Database credentials: Environment variables only (no plaintext logs)
- MCP auth token: `MCP_AUTH_TOKEN` env var
- API keys: Store in external secure vault (e.g., HashiCorp Vault)

Logs are **NOT encrypted**; assume they could be read by any system user who accesses host filesystem.

## Next Steps

1. **Configure log rotation** on host (via logrotate or cron)
2. **Monitor disk usage**: Set alerts if logs exceed 10 GB
3. **Archive logs periodically**: Weekly compress + move to NAS/S3
4. **Collect metrics**: Pre-process context.stats for dashboards
5. **Backup verification**: Test restore process monthly

