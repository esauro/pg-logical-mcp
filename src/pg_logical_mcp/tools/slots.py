"""Slot health + WAL retention tools."""

from __future__ import annotations

from typing import Any

from pg_logical_mcp import diagnostics
from pg_logical_mcp.providers.base import ReplicationProvider


def _max_slot_wal_keep_bytes(provider: ReplicationProvider) -> int | None:
    """Read ``max_slot_wal_keep_size`` (reported in MB) as bytes.

    ``-1`` means unlimited; we return ``None`` for that so the judgment layer
    treats it as "no slot will be invalidated to protect the disk".
    """
    raw = provider.get_setting("max_slot_wal_keep_size")
    if raw is None:
        return None
    try:
        mb = int(raw)
    except ValueError:
        return None
    if mb < 0:
        return None
    return mb * 1024 * 1024


def register(mcp: Any, provider: ReplicationProvider) -> None:
    @mcp.tool()
    def list_replication_slots() -> list[dict[str, Any]]:
        """List replication slots with the WAL each one pins.

        Deterministic. Wraps pg_replication_slots and joins in the bytes of WAL
        held per slot (pg_current_wal_lsn() - restart_lsn), wal_status
        (reserved/extended/unreserved/lost), safe_wal_size, active state and the
        holding pid. Run this on the PUBLISHER.
        """
        return provider.list_replication_slots()

    @mcp.tool()
    def assess_slot_risk(sample_interval_seconds: float = 2.0, available_disk_bytes: int | None = None) -> dict[str, Any]:
        """Project which slot is closest to invalidation, and how soon.

        Judgment tool. Samples the current WAL generation rate (reads
        pg_current_wal_lsn() twice, sample_interval_seconds apart), reads
        max_slot_wal_keep_size, and projects a rough time-to-invalidation per
        slot. Pass available_disk_bytes to also get a time-to-disk-fill
        estimate. Run this on the PUBLISHER.
        """
        slots = provider.list_replication_slots()
        rate = provider.sample_wal_rate(sample_interval_seconds)
        max_keep = _max_slot_wal_keep_bytes(provider)
        result = diagnostics.assess_slot_risk(
            slots=slots,
            wal_bytes_per_second=rate["bytes_per_second"],
            max_slot_wal_keep_bytes=max_keep,
            available_disk_bytes=available_disk_bytes,
        )
        result["wal_sample"] = rate
        return result
