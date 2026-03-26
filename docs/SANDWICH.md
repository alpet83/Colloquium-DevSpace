Sandwich Pack CLI and Library Documentation
Purpose
The sandwich_pack library and spack CLI utility are designed for client-side analysis and packaging of complex software projects and chat data to enable efficient processing by AI systems, such as large language models (LLMs). They transform source files and chat posts into a structured, compact format called "sandwiches" and generate two JSON indexes (main and deep) with metadata to facilitate AI-driven analysis, modification, or debugging. The library supports Rust, Vue, JavaScript, TypeScript, Python, Shell, PHP, Markdown, TOML, and chat-related content (posts, rules).
Library Usage
The SandwichPack class in /lib/sandwich_pack.py provides a programmatic interface for packing content. It uses a modular structure with a base ContentBlock class and specialized classes for different content types, dynamically loaded from lib/*_block.py.
Example
from lib.sandwich_pack import SandwichPack, ContentBlock

content = [
    ContentBlock("fn main() {}", ".rs", "/main.rs", "2025-07-15 08:00:00Z"),
    ContentBlock("Hello @attach#1", ":post", "chat_1_post_1", "2025-07-15 08:01:00Z", post_id=1, chat_id=1, user_id=1, relevance=0)
]
packer = SandwichPack(max_size=80_000)
result = packer.pack(content)
print(result["index"])  # Main JSON index
print(result["deep_index"])  # Deep JSON index
for i, sandwich in enumerate(result["sandwiches"], 1):
    with open(f"sandwich_{i}.txt", "w") as f:
        f.write(sandwich)

Modules
/lib/content_block.py

ContentBlock: Base class with fields content_text, content_type (e.g., .rs, :post), file_name, timestamp, length, tokens, post_id, chat_id, user_id, relevance. Provides methods for cleaning code (strip_strings, strip_comments via CodeStripper) and determining entity line ranges (detect_bounds). Method parse_content() is a stub for .toml, .md, .markdown, :rules. Updated in version 0.5 to support case-insensitive comment stripping for PHP tags (e.g., <?PHP, <?PhP) using re.IGNORECASE.

/lib/code_stripper.py

CodeStripper: Abstract base class for stripping strings and comments from code to eliminate parser traps (e.g., string literals or comments that could be mistaken for code entities). Defines strip(lines: list) for processing lines, using abstract methods detect_single, detect_multi_open, and detect_multi_close to identify content to remove. Maintains strip_log and warnings for diagnostics. Supports cyclic processing of multiple single-line strings/comments in one line (e.g., 'abc' + "def").
CodeStringStripper: Strips single-line and multi-line string literals, preserving quotes and handling raw strings (e.g., r"..." in Rust, '''...''' in Python). Supports cyclic processing of multiple single-line strings in one line.
CodeCommentStripper: Strips single-line and multi-line comments (e.g., //, /* */ in C-style languages, # in Python). Supports case-insensitive matching for multi-line comments (e.g., <?php, <?PHP). Introduced in version 0.6 to improve modularity.

Other Modules

/lib/sandwich_pack.py: Core class SandwichPack for packing content into sandwiches and generating JSON indexes.
/lib/*_block.py: Language-specific block classes (e.g., ContentCodePython, ContentCodeRust) for parsing and dependency extraction.
/tests/*_parse.py: Unit tests for validating parsing and stripping logic.

Index Format
sandwiches_index.json
{
  "packer_version": "string",
  "context_date": "YYYY-MM-DD HH:MM:SSZ",
  "templates": {
    "filelist": "file_id,file_name,md5,tokens,timestamp",
    "users": "user_id,username,role",
    "entities": "vis(pub/prv),type,parent,name,file_id,start_line-end_line,tokens"
  },
  "project_name": "string",
  "datasheet": {
    "project_root": "path",
    "backend_address": "url",
    "frontend_address": "url",
    "resources": {
      "database": "uri",
      "log_file": "path"
    }
  },
  "files": ["file_id,file_name,md5,tokens,timestamp"],
  "entities": ["vis,type,parent,name,file_id,start_line-end_line,tokens"],
  "users": ["user_id,username,role"]
}

sandwiches_structure.json
{
  "templates": {
    "entities": "vis(pub/prv),type,parent,name,file_id,start_line-end_line,tokens",
    "modules": "module_name"
  },
  "dep_format": "modules: index (int) referencing modules list; imports: tuple (file_id, entity_name) referencing entities list",
  "modules": ["module_name"],
  "sandwiches": [
    {
      "file": "sandwich_N.txt",
      "blocks": [
        {
          "file_id": integer,
          "start_line": integer,
          "modules": ["external file names"],
          "imports": ["entity names or indices"],
          "calls": ["function/method names or indices"],
          "entities": [integer]
        }
      ]
    }
  ]
}

Context Restoration

Load sandwiches_index.json to access project metadata, files, entities, and users.
Load sandwiches_structure.json to navigate sandwich files (sandwich_N.txt) and their blocks using start_line, file_id, and dependency details.
Update relevance for posts based on LLM references.
Use datasheet from sandwiches_index.json for project context (e.g., paths, addresses).
Note: Tools using older index formats (without start_line-end_line in entities) may require updates to handle the new format.

Limitations

Dependency resolution is heuristic-based and may miss complex imports.
Entity extraction for .vue, .js, .ts, .py, .sh, .php is basic, though improved for .js, .ts, .py, and .php in version 0.5 with better line number accuracy, TypeScript support, and PHP import/call parsing. Further improved for .py and .vue in version 0.6 with local function support and accurate component detection.
Large projects require multiple sandwiches, managed via the deep index.
Files must be in UTF-8 (with BOM support via utf-8-sig).

Expected Issues

Multiple Multi-line Comments in One Line: CodeCommentStripper in /lib/code_stripper.py does not support processing multiple multi-line comments in a single line (e.g., /* c1 */ code /* c2 */). Only the first comment is stripped, leaving subsequent comments intact. This is a rare case but may affect parsing accuracy in specific scenarios. Planned for resolution in a future version with cyclic multi-line comment processing.

Future Improvements

Enhance dependency extraction for .vue, .js, .ts, .sh, .php to support complex structures (e.g., nested imports, dynamic calls, PHP namespaces, Composer dependencies).
Optimize index size with shorter field names.
Add versioning for indexes to improve compatibility with older tools.
Add module/namespace support for .vue, .js, .ts, .php similar to Rust's module prefixing.
Improve CodeStripper to handle edge cases like nested string literals or multiple multi-line comments in one line.

Contributing

Add new block classes in /lib/*_block.py with SandwichPack.register_block_class.
Enhance entity/dependency extraction for other languages.
Submit issues or PRs to the project repository.

## MCP Server

`src/spack_mcp_server.py` exposes the Sandwich-pack index as a
[Model Context Protocol](https://modelcontextprotocol.io/) (MCP) server, letting
AI agents (e.g. GitHub Copilot) query a live, cached entity index without
triggering a full reindex on every access.

### Installation

```
pip install -r requirements.txt
```

### Universal packer script — `spack_agent.py`

`src/spack_agent.py` is the project-independent runner used by the MCP server
during reindex. It accepts any project directory via `--project` and derives
everything else from that path — no hardcoded paths anywhere.

```
# Pack cwd and update .github/copilot-instructions.md
python src/spack_agent.py

# Explicit project
python src/spack_agent.py --project /path/to/myproject

# Pack only, skip instructions update
python src/spack_agent.py --project /path/to/myproject --pack-only

# Rebuild instructions from existing index, skip repacking
python src/spack_agent.py --project /path/to/myproject --update-only

# Override project name and instructions file location
python src/spack_agent.py --project /path/to/myproject \
    --project-name myproj \
    --instructions /path/to/myproject/.github/copilot-instructions.md
```

### Per-project shim (optional)

For convenience you can drop a thin `spack_<project>.py` shim into a project root.
The shim locates `spack_agent.py` automatically — no absolute paths are needed:

```python
#!/usr/bin/env python3
# Shim — delegates to spack_agent.py. No hardcoded paths.
import os, subprocess, sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent

def _find_agent():
    if home := os.environ.get('SPACK_HOME'):
        c = Path(home) / 'src' / 'spack_agent.py'
        if c.exists(): return c
    for parent in [SCRIPT_DIR.parent, SCRIPT_DIR.parent.parent]:
        for name in ['Sandwich-pack', 'sandwich-pack', 'spack']:
            c = parent / name / 'src' / 'spack_agent.py'
            if c.exists(): return c
    return None

agent = _find_agent()
if not agent:
    print('ERROR: spack_agent.py not found. Set SPACK_HOME.', file=sys.stderr)
    sys.exit(1)
sys.exit(subprocess.run(
    [sys.executable, str(agent), '--project', str(SCRIPT_DIR)] + sys.argv[1:]
).returncode)
```

The shim resolution order:
1. `SPACK_HOME` environment variable (e.g. `set SPACK_HOME=C:\\tools\\Sandwich-pack`)
2. Sibling directories of the project root (`../Sandwich-pack/src/spack_agent.py`, etc.)

### MCP server usage

```
python src/spack_mcp_server.py --project /path/to/project
```

By default the server calls `spack_agent.py` from its own `src/` directory.
Pass `--script` only if you need a custom packer:

```
python src/spack_mcp_server.py --project /path/to/project --script /custom/packer.py
```

Optional flags:

| Flag | Default | Description |
|---|---|---|
| `--project PATH` | `.` (cwd) | Root directory of the project to index |
| `--script PATH` | `spack_agent.py` beside the server | Custom pack script called during reindex |
| `--no-watch` | off | Disable filesystem watcher (manual reindex only) |
| `--debounce FLOAT` | `5.0` | Seconds to wait after last file change before marking stale |
| `--log-file PATH` | `<project>/.vscode/mcp_sandwich_pack.log` | Log file path |

### MCP Tools

| Tool | Description |
|---|---|
| `spack_get_status` | Check stale flag, build state, age, file/entity counts. Call first in each session. |
| `spack_reindex` | Trigger full reindex. Mutex-guarded — concurrent calls return `already_building` immediately. |
| `spack_get_index` | Return full cached index JSON instantly (never blocks). |
| `spack_get_entities` | Return entity list, optionally filtered by `file_filter` path substring. |

### Concurrency Model

`IndexManager` holds an `asyncio.Lock` around `reindex()`. A second call while a
build is running returns `{"status": "already_building"}` without waiting.
The filesystem watcher sets only a `_stale` flag — it never writes files — so
watcher callbacks cannot race with the reindex subprocess.

### VS Code integration

Add an entry to your project's `.vscode/mcp.json`:

```json
{
  "servers": {
    "sandwich-pack": {
      "command": "python",
      "args": [
        "/path/to/Sandwich-pack/src/spack_mcp_server.py",
        "--project", "${workspaceFolder}"
      ],
      "type": "stdio"
    }
  }
}
```

> **Windows example** — use double backslashes or forward slashes:
> ```json
> "args": [
>   "C:\\tools\\Sandwich-pack\\src\\spack_mcp_server.py",
>   "--project", "${workspaceFolder}"
> ]
> ```

If your Python interpreter is not on `PATH`, replace `"python"` with the full
path to the executable (e.g. `"C:\\Apps\\Python3\\python.exe"`).

The `--script` argument is **no longer needed** when `spack_agent.py` is used —
the server resolves it automatically from its own directory.

### Tests

```
cd /path/to/Sandwich-pack
python -m pytest src/tests/test_mcp_server.py -v
```

21 tests covering IndexManager cache warm-up, stale marking, concurrent reindex
mutex, entity filtering, and FileWatcher debounce + ignore rules.
