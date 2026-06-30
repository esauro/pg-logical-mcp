"""Gated remediation: the only tools that mutate irreversible state.

Every tool here can lose data. The guardrails are deterministic, not advisory —
the LLM can *recommend* an action but is structurally prevented from
fat-fingering one:

* **Two-key write gate.** The per-call ``allow_writes=True`` flag is necessary
  but not sufficient: the operator must *also* have launched the server with the
  ``PG_LOGICAL_MCP_ALLOW_WRITES`` environment switch set. With the switch unset,
  these tools are read-only no matter what the model passes.
* **Dry-run by default.** ``allow_writes=False`` (the default) returns a preview
  of exactly what would happen and mutates nothing.
* **Explicit, exact arguments.** The caller passes the precise slot name and the
  exact LSN it expects. There is no "advance to latest" / "skip to head"
  convenience that would silently discard an unknown amount of data.

This is the same deterministic-gate-plus-judgment split the companion
pre-commit-review hook uses; see the README.
"""

from __future__ import annotations

import os
from typing import Any

from pg_logical_mcp.providers.base import ReplicationProvider

_WRITE_SWITCH_ENV = "PG_LOGICAL_MCP_ALLOW_WRITES"


def _writes_enabled_in_env() -> bool:
    return os.environ.get(_WRITE_SWITCH_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _gate(operation: str, allow_writes: bool, preview: dict[str, Any]) -> dict[str, Any] | None:
    """Return a refusal/dry-run dict if the write must NOT proceed, else None."""
    if not _writes_enabled_in_env():
        return {
            "executed": False,
            "blocked": True,
            "reason": (
                f"Writes are disabled. Start the server with {_WRITE_SWITCH_ENV}=1 to enable "
                f"remediation tools. This is an out-of-band switch the model cannot set."
            ),
            "operation": operation,
            "preview": preview,
        }
    if not allow_writes:
        return {
            "executed": False,
            "dry_run": True,
            "operation": operation,
            "preview": preview,
            "to_execute": "Re-call with allow_writes=true and the same exact arguments.",
        }
    return None


def register(mcp: Any, provider: ReplicationProvider) -> None:
    def _find_slot(slot_name: str) -> dict[str, Any] | None:
        return next((s for s in provider.list_replication_slots() if s["slot_name"] == slot_name), None)

    @mcp.tool()
    def advance_slot(slot_name: str, to_lsn: str, allow_writes: bool = False) -> dict[str, Any]:
        """Advance a replication slot to an EXPLICIT LSN (irreversible).

        Moves the slot's confirmed position forward, discarding all changes
        before `to_lsn`. You must pass the exact slot name and target LSN — there
        is no advance-to-latest shortcut. Dry-run unless allow_writes=true AND
        the server was started with PG_LOGICAL_MCP_ALLOW_WRITES set.
        """
        slot = _find_slot(slot_name)
        preview = {
            "slot_name": slot_name,
            "target_lsn": to_lsn,
            "current_slot_state": slot,
            "effect": "Discards all changes before target_lsn. Data not yet applied downstream is lost.",
            "slot_exists": slot is not None,
        }
        gated = _gate("advance_slot", allow_writes, preview)
        if gated is not None:
            return gated
        result = provider.advance_slot(slot_name, to_lsn)
        return {"executed": True, "operation": "advance_slot", "result": result}

    @mcp.tool()
    def skip_apply_transaction(subscription_name: str, lsn: str, allow_writes: bool = False) -> dict[str, Any]:
        """Skip the transaction wedging a subscription at an EXPLICIT LSN.

        Issues ALTER SUBSCRIPTION ... SKIP (lsn = ...) so the apply worker steps
        over the offending transaction. That transaction's changes are discarded
        PERMANENTLY — use only when you've confirmed (from the subscriber log)
        that the change is genuinely redundant. Pass the exact LSN from
        diagnose_stuck_subscription; there is no skip-to-head shortcut. Dry-run
        unless allow_writes=true AND PG_LOGICAL_MCP_ALLOW_WRITES is set.
        """
        preview = {
            "subscription_name": subscription_name,
            "skip_lsn": lsn,
            "effect": "The transaction finishing at skip_lsn is skipped; its changes never apply downstream.",
        }
        gated = _gate("skip_apply_transaction", allow_writes, preview)
        if gated is not None:
            return gated
        result = provider.skip_apply_transaction(subscription_name, lsn)
        return {"executed": True, "operation": "skip_apply_transaction", "result": result}

    @mcp.tool()
    def drop_slot(slot_name: str, allow_writes: bool = False) -> dict[str, Any]:
        """Drop a replication slot by EXACT name (irreversible).

        Removes the slot and releases the WAL it pins. Any subscriber relying on
        it can no longer resume and must be re-created and re-synced. You must
        pass the exact slot name. Dry-run unless allow_writes=true AND
        PG_LOGICAL_MCP_ALLOW_WRITES is set.
        """
        slot = _find_slot(slot_name)
        preview = {
            "slot_name": slot_name,
            "current_slot_state": slot,
            "slot_exists": slot is not None,
            "active": slot.get("active") if slot else None,
            "pinned_wal_bytes": slot.get("pinned_wal_bytes") if slot else None,
            "effect": "Slot is destroyed and its pinned WAL freed. A subscriber using it cannot resume.",
        }
        gated = _gate("drop_slot", allow_writes, preview)
        if gated is not None:
            return gated
        result = provider.drop_slot(slot_name)
        return {"executed": True, "operation": "drop_slot", "result": result}
