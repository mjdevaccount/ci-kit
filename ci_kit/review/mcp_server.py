"""The reviewer's tools (`ci_kit.review.tools`) as a stdio MCP server, for the CLI transport.

`claude` runs `claude -p` with EVERY built-in tool removed (`--tools ""`), so the only tools the
reviewer has are these four -- the same functions the `api` transport hands pydantic-ai. One tool
set, two transports; nothing here re-implements a tool. The library is the `mcp` SDK (2.x calls
the server MCPServer; it was FastMCP in 1.x).

REVIEW_GATE_ROOT names the checkout: `tools` fences paths to the cwd it is imported in, and the
CLI's choice of cwd for a server is not ours -- so chdir happens BEFORE that import.
"""
from __future__ import annotations

import os


def build_server():
    from mcp.server.mcpserver import MCPServer

    from ci_kit.review import tools
    server = MCPServer("review")
    for name in tools.TOOLS:
        # unstructured: every tool returns plain text
        server.add_tool(getattr(tools, name), structured_output=False)
    return server


def main() -> None:
    os.chdir(os.environ.get("REVIEW_GATE_ROOT") or os.getcwd())   # BEFORE the import fixes ROOT
    build_server().run()


if __name__ == "__main__":
    main()
