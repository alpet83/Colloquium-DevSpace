#!/usr/bin/env python3
import re

with open('p:\\opt\\docker\\cqds\\projects\\mcp_server.py', 'r') as f:
    content = f.read()

# Replace async with PROCESS_REGISTRY_LOCK with with PROCESS_REGISTRY_LOCK
content = re.sub(r'async with PROCESS_REGISTRY_LOCK:', 'with PROCESS_REGISTRY_LOCK:', content)

with open('p:\\opt\\docker\\cqds\\projects\\mcp_server.py', 'w') as f:
    f.write(content)

print("✓ Replaced all 'async with PROCESS_REGISTRY_LOCK:' with 'with PROCESS_REGISTRY_LOCK:'")
