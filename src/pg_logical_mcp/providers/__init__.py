"""Replication providers. One protocol, one Postgres implementation."""

from pg_logical_mcp.providers.base import ReplicationProvider
from pg_logical_mcp.providers.postgres import PostgresProvider

__all__ = ["ReplicationProvider", "PostgresProvider"]
