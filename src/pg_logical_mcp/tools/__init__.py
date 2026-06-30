"""MCP tool modules.

Each module exposes ``register(mcp, provider)`` which defines its tools on the
shared FastMCP instance. ``server.py`` calls them in build order.
"""

from pg_logical_mcp.tools import (
    cdc,
    publications,
    publisher,
    remediation,
    slots,
    subscriptions,
)

REGISTRARS = [
    slots.register,
    publisher.register,
    publications.register,
    subscriptions.register,
    cdc.register,
    remediation.register,
]

__all__ = ["REGISTRARS"]
