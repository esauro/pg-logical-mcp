"""The one and only :class:`ReplicationProvider` implementation.

Wraps a single libpq connection (psycopg 3) and turns Postgres catalog/stats
views into the plain dicts the tool layer expects. All reads run with
``autocommit`` so they never hold a transaction open against the catalogs.

Connection details come from a DSN (``PG_LOGICAL_MCP_DSN``) or the standard
``PG*`` libpq environment variables — credentials are the operator's, never the
author's. See the README's hosting rationale.
"""

from __future__ import annotations

import os
import time
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

# Connection info containing a password — never surfaced through a tool result.
_REDACTED_SUBSCRIPTION_FIELDS = {"subconninfo"}


class PostgresProvider:
    """Catalog-backed provider for a single Postgres instance.

    A provider points at *one* node. To inspect both sides of a topology, run
    one server per side (or reconnect) — the publisher exposes slots/walsenders,
    the subscriber exposes subscriptions.
    """

    def __init__(self, dsn: str | None = None) -> None:
        # An empty conninfo lets libpq fall back to PGHOST/PGUSER/... env vars.
        self._dsn = dsn if dsn is not None else os.environ.get("PG_LOGICAL_MCP_DSN", "")
        self._conn: psycopg.Connection | None = None

    # -- connection plumbing ---------------------------------------------

    def _connection(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self._dsn, autocommit=True, row_factory=dict_row)
        return self._conn

    def _query(self, query: Any, params: tuple[Any, ...] | None = None) -> list[dict[str, Any]]:
        with self._connection().cursor() as cur:
            cur.execute(query, params)
            if cur.description is None:
                return []
            return list(cur.fetchall())

    def _server_version_num(self) -> int:
        return int(self._query("SHOW server_version_num")[0]["server_version_num"])

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.close()

    # -- slots & WAL ------------------------------------------------------

    def list_replication_slots(self) -> list[dict[str, Any]]:
        return self._query(
            """
            SELECT slot_name,
                   slot_type,
                   plugin,
                   database,
                   active,
                   active_pid,
                   restart_lsn::text             AS restart_lsn,
                   confirmed_flush_lsn::text     AS confirmed_flush_lsn,
                   wal_status,
                   safe_wal_size,
                   pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)::bigint
                       AS pinned_wal_bytes
            FROM pg_replication_slots
            ORDER BY pinned_wal_bytes DESC NULLS LAST
            """
        )

    def sample_wal_rate(self, interval_seconds: float) -> dict[str, Any]:
        # Absolute byte offset of the current WAL position (diff against 0/0).
        def _pos() -> int:
            row = self._query("SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), '0/0')::bigint AS pos")
            return int(row[0]["pos"])

        start = _pos()
        time.sleep(max(interval_seconds, 0.0))
        end = _pos()
        delta = max(end - start, 0)
        seconds = max(interval_seconds, 1e-9)
        return {
            "bytes": delta,
            "seconds": interval_seconds,
            "bytes_per_second": delta / seconds,
        }

    def get_setting(self, name: str) -> str | None:
        rows = self._query("SELECT setting FROM pg_settings WHERE name = %s", (name,))
        return rows[0]["setting"] if rows else None

    # -- publisher side ---------------------------------------------------

    def inspect_walsenders(self) -> list[dict[str, Any]]:
        return self._query(
            """
            SELECT pid,
                   usename,
                   application_name,
                   client_addr::text                          AS client_addr,
                   state,
                   sync_state,
                   sent_lsn::text                             AS sent_lsn,
                   write_lsn::text                            AS write_lsn,
                   flush_lsn::text                            AS flush_lsn,
                   replay_lsn::text                           AS replay_lsn,
                   pg_wal_lsn_diff(sent_lsn, write_lsn)::bigint   AS write_lsn_bytes,
                   pg_wal_lsn_diff(sent_lsn, flush_lsn)::bigint   AS flush_lsn_bytes,
                   pg_wal_lsn_diff(sent_lsn, replay_lsn)::bigint  AS replay_lsn_bytes,
                   extract(epoch FROM write_lag)              AS write_lag_seconds,
                   extract(epoch FROM flush_lag)              AS flush_lag_seconds,
                   extract(epoch FROM replay_lag)             AS replay_lag_seconds
            FROM pg_stat_replication
            ORDER BY pid
            """
        )

    def decoding_stats(self) -> list[dict[str, Any]]:
        return self._query(
            """
            SELECT slot_name,
                   spill_txns,
                   spill_count,
                   spill_bytes,
                   stream_txns,
                   stream_count,
                   stream_bytes,
                   total_txns,
                   total_bytes,
                   stats_reset::text AS stats_reset
            FROM pg_stat_replication_slots
            ORDER BY spill_bytes DESC NULLS LAST
            """
        )

    # -- publications -----------------------------------------------------

    def list_publications(self) -> list[dict[str, Any]]:
        pubs = self._query(
            """
            SELECT pubname,
                   puballtables,
                   pubinsert,
                   pubupdate,
                   pubdelete,
                   pubtruncate
            FROM pg_publication
            ORDER BY pubname
            """
        )

        # ``rowfilter`` only exists from PG15. Select it conditionally so the
        # tool works against older servers without erroring.
        has_rowfilter = self._server_version_num() >= 150000
        rowfilter_col = "rowfilter" if has_rowfilter else "NULL::text AS rowfilter"
        tables = self._query(
            sql.SQL(
                """
                SELECT pubname, schemaname, tablename, attnames, {rowfilter}
                FROM pg_publication_tables
                ORDER BY pubname, schemaname, tablename
                """
            ).format(rowfilter=sql.SQL(rowfilter_col))
        )

        by_pub: dict[str, list[dict[str, Any]]] = {}
        for t in tables:
            by_pub.setdefault(t["pubname"], []).append(
                {
                    "schemaname": t["schemaname"],
                    "tablename": t["tablename"],
                    "attnames": t["attnames"],
                    "rowfilter": t["rowfilter"],
                }
            )
        for p in pubs:
            p["tables"] = by_pub.get(p["pubname"], [])
        return pubs

    def table_replica_identities(self) -> list[dict[str, Any]]:
        return self._query(
            """
            SELECT n.nspname AS schemaname,
                   c.relname AS tablename,
                   CASE c.relreplident
                       WHEN 'd' THEN 'default'
                       WHEN 'n' THEN 'nothing'
                       WHEN 'f' THEN 'full'
                       WHEN 'i' THEN 'index'
                   END AS replica_identity,
                   EXISTS (
                       SELECT 1 FROM pg_index i
                       WHERE i.indrelid = c.oid AND i.indisprimary
                   ) AS has_primary_key
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind = 'r'
              AND n.nspname NOT IN ('pg_catalog', 'information_schema')
            ORDER BY n.nspname, c.relname
            """
        )

    # -- subscriber side --------------------------------------------------

    def inspect_subscriptions(self) -> list[dict[str, Any]]:
        # subconninfo is deliberately NOT selected — it holds the password.
        return self._query(
            """
            SELECT s.oid::bigint                          AS subid,
                   s.subname,
                   s.subenabled,
                   s.subslotname,
                   s.subpublications,
                   st.pid,
                   st.received_lsn::text                  AS received_lsn,
                   st.latest_end_lsn::text                AS latest_end_lsn,
                   st.last_msg_send_time::text            AS last_msg_send_time,
                   st.last_msg_receipt_time::text         AS last_msg_receipt_time,
                   st.latest_end_time::text               AS latest_end_time,
                   extract(epoch FROM (st.last_msg_receipt_time - st.last_msg_send_time))
                                                          AS apply_lag_seconds
            FROM pg_subscription s
            LEFT JOIN pg_stat_subscription st ON st.subid = s.oid
            ORDER BY s.subname
            """
        )

    def subscription_errors(self) -> list[dict[str, Any]]:
        return self._query(
            """
            SELECT subname,
                   apply_error_count,
                   sync_error_count,
                   stats_reset::text AS stats_reset
            FROM pg_stat_subscription_stats
            ORDER BY (apply_error_count + sync_error_count) DESC
            """
        )

    # -- CDC stream -------------------------------------------------------

    def peek_changes(self, slot_name: str, limit: int | None = None) -> list[dict[str, Any]]:
        # peek (not get) — does not advance the slot or consume the queue.
        # ``data`` rendering depends on the slot's output plugin (text for
        # test_decoding; binary for pgoutput).
        return self._query(
            """
            SELECT lsn::text AS lsn, xid::text AS xid, data
            FROM pg_logical_slot_peek_changes(%s, NULL, %s)
            """,
            (slot_name, limit),
        )

    # -- gated, irreversible writes --------------------------------------

    def advance_slot(self, slot_name: str, to_lsn: str) -> dict[str, Any]:
        rows = self._query(
            "SELECT slot_name, end_lsn::text AS end_lsn "
            "FROM pg_replication_slot_advance(%s, %s)",
            (slot_name, to_lsn),
        )
        return rows[0] if rows else {"slot_name": slot_name, "end_lsn": None}

    def skip_apply_transaction(self, subscription_name: str, lsn: str) -> dict[str, Any]:
        # Identifier can't be parameterised; compose it safely. The LSN is
        # validated by casting to pg_lsn before it reaches the DDL.
        self._query("SELECT %s::pg_lsn", (lsn,))
        stmt = sql.SQL("ALTER SUBSCRIPTION {name} SKIP (lsn = {lsn})").format(
            name=sql.Identifier(subscription_name),
            lsn=sql.Literal(lsn),
        )
        self._query(stmt)
        return {"subscription_name": subscription_name, "skipped_lsn": lsn}

    def drop_slot(self, slot_name: str) -> dict[str, Any]:
        self._query("SELECT pg_drop_replication_slot(%s)", (slot_name,))
        return {"slot_name": slot_name, "dropped": True}
