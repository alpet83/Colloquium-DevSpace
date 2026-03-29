# Runbooks

Practical operation and maintenance flows.

## Deployment

- Windows deployment: [DEPLOY-MSWIN](../DEPLOY-MSWIN.md)
- Generic deployment: [DEPLOY](../DEPLOY.md)
- Deployment overview: [DEPLOY-README](../DEPLOY-README.md)

## MCP and Multi-Agent Flows

- MCP quick start: [MCP_DELEGATION_QUICK_START](../MCP_DELEGATION_QUICK_START.md)
- Delegation strategy: [MCP_DELEGATION_STRATEGY](../MCP_DELEGATION_STRATEGY.md)
- Delegation template: [MCP_DELEGATION_TEMPLATE](../MCP_DELEGATION_TEMPLATE.md)
- Interactive flow upgrade: [LLM-interactive-upgrade](../LLM-interactive-upgrade.md)

## Logging and Diagnostics

- Logs export and diagnostics: [CQDS_LOGS_DATA_EXPORT](../CQDS_LOGS_DATA_EXPORT.md)
- Token accounting todo: [CQDS_TOKEN_ACCOUNTING_TODO](../CQDS_TOKEN_ACCOUNTING_TODO.md)
- TTL checklist: [ttl-checklist](../ttl-checklist.md)

## Recovery and Safety Checklist

1. Validate active `mcp.json` paths after any script move.
2. For runtime sync, update both repository and runtime copies.
3. Avoid lossy shell redirection for markdown restoration.
4. Re-open docs in at least two viewers after encoding-sensitive changes.
