"""The ``ReplicationProvider`` protocol.

The tool layer talks to a database *only* through this protocol. Today the sole
implementation is :class:`~pg_logical_mcp.providers.postgres.PostgresProvider`;
the protocol exists to decouple the tools from raw catalog SQL (so the judgment
layer can be unit-tested against canned dicts) and to leave a clean seam should a
second backend ever be wanted. It is intentionally Postgres-shaped — there is no
MySQL/binlog abstraction here, and none is planned.

Every method returns plain ``dict``/``list`` data (JSON-serialisable). The
judgment functions in :mod:`pg_logical_mcp.diagnostics` consume exactly these
shapes, which keeps them pure and database-free.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ReplicationProvider(Protocol):
    """Read + (gated) write access to a logical-replication topology.

    Read methods wrap catalog/stats views and never mutate state. The three
    write methods at the end mutate irreversible state and are only reachable
    through the gated remediation tools.
    """

    # -- slots & WAL ------------------------------------------------------

    def list_replication_slots(self) -> list[dict[str, Any]]:
        """``pg_replication_slots`` plus pinned-WAL bytes and ``wal_status``."""
        ...

    def sample_wal_rate(self, interval_seconds: float) -> dict[str, Any]:
        """Sample ``pg_current_wal_lsn()`` twice, ``interval_seconds`` apart.

        Returns ``{"bytes": int, "seconds": float, "bytes_per_second": float}``
        describing how fast the publisher is generating WAL right now.
        """
        ...

    def get_setting(self, name: str) -> str | None:
        """Return a ``pg_settings`` value (e.g. ``max_slot_wal_keep_size``)."""
        ...

    # -- publisher side ---------------------------------------------------

    def inspect_walsenders(self) -> list[dict[str, Any]]:
        """``pg_stat_replication``: per-connection state and lag."""
        ...

    def decoding_stats(self) -> list[dict[str, Any]]:
        """``pg_stat_replication_slots``: spill/stream decode stats."""
        ...

    # -- publications -----------------------------------------------------

    def list_publications(self) -> list[dict[str, Any]]:
        """``pg_publication`` + ``pg_publication_tables`` with row filters."""
        ...

    def table_replica_identities(self) -> list[dict[str, Any]]:
        """Per-table ``REPLICA IDENTITY`` setting for published-table checks."""
        ...

    # -- subscriber side --------------------------------------------------

    def inspect_subscriptions(self) -> list[dict[str, Any]]:
        """``pg_subscription`` joined with ``pg_stat_subscription``."""
        ...

    def subscription_errors(self) -> list[dict[str, Any]]:
        """``pg_stat_subscription_stats``: apply/sync error counts."""
        ...

    # -- CDC stream -------------------------------------------------------

    def peek_changes(self, slot_name: str, limit: int | None = None) -> list[dict[str, Any]]:
        """``pg_logical_slot_peek_changes`` — inspect without consuming."""
        ...

    # -- gated, irreversible writes --------------------------------------

    def advance_slot(self, slot_name: str, to_lsn: str) -> dict[str, Any]:
        """``pg_replication_slot_advance`` to an explicit LSN."""
        ...

    def skip_apply_transaction(self, subscription_name: str, lsn: str) -> dict[str, Any]:
        """``ALTER SUBSCRIPTION ... SKIP (lsn = ...)`` at an explicit LSN."""
        ...

    def drop_slot(self, slot_name: str) -> dict[str, Any]:
        """``pg_drop_replication_slot`` — destroys the slot."""
        ...
