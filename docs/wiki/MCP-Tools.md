# MCP Tools

This page tracks MCP scripts and config locations after path migration.

## Script Locations

### Repository paths

- CQDS bridge server: [src/mcp-tools/copilot_mcp_tool.py](../../src/mcp-tools/copilot_mcp_tool.py)
- Git Bash server: [src/mcp-tools/mcp_gitbash_stdio.py](../../src/mcp-tools/mcp_gitbash_stdio.py)

### Runtime paths

- `/opt/docker/cqds/mcp-tools/copilot_mcp_tool.py`
- `/opt/docker/cqds/mcp-tools/mcp_gitbash_stdio.py`

## MCP Config Files

### Repository

- [src/.cursor/mcp.json](../../src/.cursor/mcp.json)

### Runtime

- `/opt/docker/cqds/.cursor/mcp.json`
- `/opt/docker/cqds/.vscode/mcp.json`

## Related Documentation

- [copilot_mcp_tool](../copilot_mcp_tool.md)
- [MCP_DELEGATION_QUICK_START](../MCP_DELEGATION_QUICK_START.md)
- [MCP_DELEGATION_STRATEGY](../MCP_DELEGATION_STRATEGY.md)
- [MCP_DELEGATION_TEMPLATE](../MCP_DELEGATION_TEMPLATE.md)

## Notes

- Keep script path references consistent with `mcp-tools` in all `mcp.json` files.
- Avoid shell redirection for document restore flows when Cyrillic text is involved.
- Prefer byte-safe restore strategy (`git show` via programmatic bytes decode/encode) for docs recovery.

## Project-Scoped MCP Route

- `projects.mcp_server_url` is now the runtime route source for MCP process APIs.
- Core processors (`shell_code`, `cmd`) resolve MCP endpoint from selected project and fallback to `MCP_SERVER_URL`.
- MCP tool resolves route from `cq_list_projects` payload and caches it per project.
- `cq_process_spawn` stores `process_guid -> mcp_server_url` mapping for follow-up `cq_process_io/status/wait/kill`.
