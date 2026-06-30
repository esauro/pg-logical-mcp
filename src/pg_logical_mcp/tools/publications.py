"""Publication tools: enumeration and coverage checking."""

from __future__ import annotations

from typing import Any

from pg_logical_mcp import diagnostics
from pg_logical_mcp.providers.base import ReplicationProvider


def register(mcp: Any, provider: ReplicationProvider) -> None:
    @mcp.tool()
    def list_publications() -> list[dict[str, Any]]:
        """List publications and the tables each carries.

        Deterministic. Enumerates pg_publication and pg_publication_tables with
        per-table column lists, row filters (PG15+), and the published
        operations (insert/update/delete/truncate). Run this on the PUBLISHER.
        """
        return provider.list_publications()

    @mcp.tool()
    def check_publication_coverage(expected_tables: list[str]) -> dict[str, Any]:
        """Cross-check published tables against an expected set.

        Judgment tool. Flags the two silent failures that lose data without
        erroring: (1) an expected table that no publication carries, and (2) a
        published table whose REPLICA IDENTITY can't support UPDATE/DELETE
        (REPLICA IDENTITY NOTHING, or DEFAULT with no primary key). Pass
        expected_tables as "schema.table" strings (bare names assume public).
        Run this on the PUBLISHER.
        """
        publications = provider.list_publications()
        published_tables = [t for pub in publications for t in pub.get("tables", [])]
        identities = provider.table_replica_identities()
        return diagnostics.check_publication_coverage(
            publication_tables=published_tables,
            replica_identities=identities,
            expected_tables=list(expected_tables),
        )
