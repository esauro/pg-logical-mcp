"""MCP server entrypoint.

Builds the FastMCP instance, constructs the single PostgresProvider from the
environment, registers every tool module (in build order), and serves over
stdio — the transport an MCP client launches this process with.

The server points at one Postgres node. To inspect both sides of a topology,
configure two server entries in your MCP client (one DSN per side): the
publisher exposes slots/walsenders/publications; the subscriber exposes
subscriptions. See the README.
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from pg_logical_mcp.providers.postgres import PostgresProvider
from pg_logical_mcp.tools import REGISTRARS


def build_server(provider: PostgresProvider | None = None) -> FastMCP:
    """Create and fully wire the FastMCP server (factory; aids testing)."""
    mcp = FastMCP("pg-logical-mcp")
    provider = provider or PostgresProvider(dsn=os.environ.get("PG_LOGICAL_MCP_DSN"))
    for register in REGISTRARS:
        register(mcp, provider)
    return mcp


def main() -> None:
    """Console-script entrypoint: serve over stdio."""
    server = build_server()
    server.run()


if __name__ == "__main__":
    main()
