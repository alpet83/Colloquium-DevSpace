from __future__ import annotations

import asyncio

from mcp.server.stdio import stdio_server  # type: ignore[import]

from text_editor.mcp_editor import create_server


async def _main() -> None:
    server = create_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()

