"""Publisher-side tools: walsenders and decoding pressure."""

from __future__ import annotations

from typing import Any

from pg_logical_mcp.providers.base import ReplicationProvider


def register(mcp: Any, provider: ReplicationProvider) -> None:
    @mcp.tool()
    def inspect_walsenders() -> list[dict[str, Any]]:
        """Inspect active walsender connections and their lag.

        Deterministic. Reads pg_stat_replication: per-connection state,
        write/flush/replay lag (both as time intervals and as LSN byte diffs
        from sent_lsn), and sync_state. Run this on the PUBLISHER.
        """
        return provider.inspect_walsenders()

    @mcp.tool()
    def decoding_stats() -> list[dict[str, Any]]:
        """Report logical-decoding spill/stream stats per slot.

        Deterministic. Reads pg_stat_replication_slots for spill_txns /
        spill_bytes and streaming stats. Large spill_bytes means big
        transactions are spilling decode work to disk — an obscure but real
        cause of replication slowness (e.g. outbox batches that decode badly).
        Run this on the PUBLISHER.
        """
        return provider.decoding_stats()
