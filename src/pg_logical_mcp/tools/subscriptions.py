"""Subscriber-side tools: state, errors, and stuck-apply diagnosis."""

from __future__ import annotations

from typing import Any

from pg_logical_mcp import diagnostics
from pg_logical_mcp.providers.base import ReplicationProvider


def register(mcp: Any, provider: ReplicationProvider) -> None:
    @mcp.tool()
    def inspect_subscriptions() -> list[dict[str, Any]]:
        """Inspect subscriptions and their apply-lag signals.

        Deterministic. Joins pg_subscription with pg_stat_subscription for
        worker state, received/latest-end LSNs, and apply-lag signals
        (last_msg_send_time vs last_msg_receipt_time). The connection string
        (subconninfo) is deliberately omitted — it holds a password. Reading
        pg_subscription needs elevated privileges. Run this on the SUBSCRIBER.
        """
        return provider.inspect_subscriptions()

    @mcp.tool()
    def subscription_errors() -> list[dict[str, Any]]:
        """Report apply and tablesync error counts per subscription.

        Deterministic. Reads pg_stat_subscription_stats (PG15+) for
        apply_error_count and sync_error_count. A non-zero apply_error_count is
        the first sign of a wedged subscription. Run this on the SUBSCRIBER.
        """
        return provider.subscription_errors()

    @mcp.tool()
    def diagnose_stuck_subscription() -> dict[str, Any]:
        """Explain why apply is wedged and lay out the two real fixes.

        Judgment tool (the marquee one). Correlates subscription error state
        with the LSN apply is stuck at, explains the usual cause (a
        unique/primary-key conflict on the subscriber), and presents the two
        options: resolve the conflicting row, or skip the offending transaction.
        Points you at the subscriber server log for the exact conflicting row.
        Run this on the SUBSCRIBER.
        """
        return diagnostics.diagnose_stuck_subscription(
            subscriptions=provider.inspect_subscriptions(),
            errors=provider.subscription_errors(),
        )
