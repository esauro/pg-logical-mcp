"""CDC stream inspection: a non-destructive peek at the change queue."""

from __future__ import annotations

from typing import Any

from pg_logical_mcp.providers.base import ReplicationProvider


def register(mcp: Any, provider: ReplicationProvider) -> None:
    @mcp.tool()
    def peek_changes(slot_name: str, limit: int = 20) -> list[dict[str, Any]]:
        """Peek at pending changes in a slot WITHOUT consuming them.

        Deterministic. Wraps pg_logical_slot_peek_changes — the *peek* variant,
        not get — so inspecting the queue does not advance the slot or consume
        the changes. Returns up to `limit` rows of (lsn, xid, data). The data
        rendering depends on the slot's output plugin: text for test_decoding,
        binary for pgoutput. Run this on the PUBLISHER.
        """
        return provider.peek_changes(slot_name, limit)
