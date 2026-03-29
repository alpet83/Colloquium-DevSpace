# MCP Tools

This page tracks MCP scripts and config locations after path migration.

## Script Locations

### Repository paths

- CQDS bridge server: [src/mcp-tools/copilot_mcp_tool.py](../../src/mcp-tools/copilot_mcp_tool.py)
- Git Bash server: [src/mcp-tools/mcp_gitbash_stdio.py](../../src/mcp-tools/mcp_gitbash_stdio.py)

### Runtime paths

- `P:/opt/docker/cqds/mcp-tools/copilot_mcp_tool.py`
- `P:/opt/docker/cqds/mcp-tools/mcp_gitbash_stdio.py`

## MCP Config Files

### Repository

- [src/.cursor/mcp.json](../../src/.cursor/mcp.json)

### Runtime

- `P:/opt/docker/cqds/.cursor/mcp.json`
- `P:/opt/docker/cqds/.vscode/mcp.json`

## Related Documentation

- [copilot_mcp_tool](../copilot_mcp_tool.md)
- [MCP_DELEGATION_QUICK_START](../MCP_DELEGATION_QUICK_START.md)
- [MCP_DELEGATION_STRATEGY](../MCP_DELEGATION_STRATEGY.md)
- [MCP_DELEGATION_TEMPLATE](../MCP_DELEGATION_TEMPLATE.md)

## Notes

- Keep script path references consistent with `mcp-tools` in all `mcp.json` files.
- Avoid shell redirection for document restore flows when Cyrillic text is involved.
- Prefer byte-safe restore strategy (`git show` via programmatic bytes decode/encode) for docs recovery.
