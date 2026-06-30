"""The judgment layer: pure functions over provider output.

Nothing in here touches a database. Each function takes the plain dicts a
:class:`~pg_logical_mcp.providers.base.ReplicationProvider` returns (and, where
relevant, a couple of already-parsed scalars the tool layer prepared) and
returns a structured finding. That purity is the point — these are the
reasoning routines, and they are unit-tested against canned inputs with no live
server. The deterministic read tools stay separate so the agent can always fall
back to raw facts.

Findings share a shape::

    {"severity": "ok|info|warning|critical", "summary": str, ...}
"""

from __future__ import annotations

from typing import Any

Severity = str  # "ok" | "info" | "warning" | "critical"


def _humanize_bytes(n: float | None) -> str:
    if n is None:
        return "unknown"
    step = 1024.0
    units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
    value = float(n)
    for unit in units:
        if abs(value) < step:
            return f"{value:.1f} {unit}"
        value /= step
    return f"{value:.1f} EiB"


def _humanize_seconds(s: float | None) -> str:
    if s is None:
        return "unknown"
    if s == float("inf"):
        return "no projected limit"
    s = int(s)
    if s < 90:
        return f"{s}s"
    if s < 90 * 60:
        return f"{s // 60}m"
    if s < 36 * 3600:
        return f"{s // 3600}h{(s % 3600) // 60}m"
    return f"{s // 86400}d{(s % 86400) // 3600}h"


def assess_slot_risk(
    slots: list[dict[str, Any]],
    wal_bytes_per_second: float,
    max_slot_wal_keep_bytes: int | None,
    available_disk_bytes: int | None = None,
) -> dict[str, Any]:
    """Project which slot is closest to invalidation, and how soon.

    ``max_slot_wal_keep_bytes`` is the byte form of ``max_slot_wal_keep_size``
    (``None``/negative means unlimited — a slot can pin WAL until the disk
    fills). ``available_disk_bytes`` is optional; pass it to get a
    time-to-disk-fill projection alongside time-to-invalidation.
    """
    unlimited = max_slot_wal_keep_bytes is None or max_slot_wal_keep_bytes < 0
    per_slot: list[dict[str, Any]] = []

    for slot in slots:
        pinned = slot.get("pinned_wal_bytes")
        wal_status = slot.get("wal_status")
        headroom = None if unlimited else max(int(max_slot_wal_keep_bytes) - int(pinned or 0), 0)

        if wal_status == "lost":
            tti: float | None = 0.0
        elif unlimited or pinned is None:
            tti = float("inf")
        elif wal_bytes_per_second <= 0:
            tti = float("inf")
        else:
            tti = headroom / wal_bytes_per_second

        per_slot.append(
            {
                "slot_name": slot.get("slot_name"),
                "active": slot.get("active"),
                "wal_status": wal_status,
                "pinned_wal_bytes": pinned,
                "pinned_wal_human": _humanize_bytes(pinned),
                "headroom_bytes": headroom,
                "time_to_invalidation_seconds": tti,
                "time_to_invalidation_human": _humanize_seconds(tti),
            }
        )

    # Closest to invalidation = smallest finite time-to-invalidation.
    finite = [s for s in per_slot if s["time_to_invalidation_seconds"] not in (None, float("inf"))]
    closest = min(finite, key=lambda s: s["time_to_invalidation_seconds"]) if finite else None

    disk_fill_seconds = None
    if available_disk_bytes is not None and wal_bytes_per_second > 0:
        disk_fill_seconds = available_disk_bytes / wal_bytes_per_second

    if any(s["wal_status"] == "lost" for s in per_slot):
        severity: Severity = "critical"
        summary = "At least one slot is already invalidated (wal_status='lost'). Replication from it cannot resume; the slot must be recreated and the subscriber re-synced."
    elif closest and closest["time_to_invalidation_seconds"] < 3600:
        severity = "critical"
        summary = (
            f"Slot '{closest['slot_name']}' is ~{closest['time_to_invalidation_human']} "
            f"from invalidation at the current WAL rate ({_humanize_bytes(wal_bytes_per_second)}/s)."
        )
    elif closest and closest["time_to_invalidation_seconds"] < 86400:
        severity = "warning"
        summary = (
            f"Slot '{closest['slot_name']}' is ~{closest['time_to_invalidation_human']} "
            f"from invalidation. Watch it."
        )
    elif unlimited and any((s["pinned_wal_bytes"] or 0) > 0 for s in per_slot):
        severity = "info"
        summary = (
            "max_slot_wal_keep_size is unlimited (-1): no slot will be invalidated to protect "
            "the disk. An inactive slot can pin WAL until the volume fills."
        )
    else:
        severity = "ok"
        summary = "No slot is near invalidation at the current WAL rate."

    return {
        "severity": severity,
        "summary": summary,
        "wal_bytes_per_second": wal_bytes_per_second,
        "max_slot_wal_keep_bytes": None if unlimited else int(max_slot_wal_keep_bytes),
        "closest_slot": closest,
        "time_to_disk_fill_seconds": disk_fill_seconds,
        "time_to_disk_fill_human": _humanize_seconds(disk_fill_seconds),
        "slots": per_slot,
    }


def _norm_table(entry: Any) -> tuple[str, str]:
    """Normalise a table reference to ``(schema, table)``; default schema public."""
    if isinstance(entry, dict):
        return (entry.get("schemaname") or entry.get("schema") or "public", entry["tablename"] if "tablename" in entry else entry["table"])
    text = str(entry)
    if "." in text:
        schema, table = text.split(".", 1)
        return (schema, table)
    return ("public", text)


def check_publication_coverage(
    publication_tables: list[dict[str, Any]],
    replica_identities: list[dict[str, Any]],
    expected_tables: list[Any],
) -> dict[str, Any]:
    """Flag the two silent publication failures.

    1. A table in ``expected_tables`` that no publication actually carries — it
       simply never replicates, and nothing errors.
    2. A *published* table whose ``REPLICA IDENTITY`` can't support UPDATE/DELETE
       (``nothing``, or ``default`` with no primary key) — inserts replicate but
       updates/deletes are silently dropped.
    """
    published = {_norm_table(t) for t in publication_tables}
    expected = {_norm_table(t) for t in expected_tables}
    ident_by_table = {(_norm_table(r)): r for r in replica_identities}

    missing = sorted(expected - published)

    identity_problems: list[dict[str, Any]] = []
    for key in sorted(published):
        ident = ident_by_table.get(key)
        if ident is None:
            continue
        ri = ident.get("replica_identity")
        has_pk = ident.get("has_primary_key")
        if ri == "nothing" or (ri == "default" and not has_pk):
            identity_problems.append(
                {
                    "schemaname": key[0],
                    "tablename": key[1],
                    "replica_identity": ri,
                    "has_primary_key": has_pk,
                    "problem": "UPDATE/DELETE will not replicate: no usable replica identity. "
                    "Add a primary key, or set REPLICA IDENTITY FULL / an index.",
                }
            )

    if missing or identity_problems:
        severity: Severity = "warning"
        parts = []
        if missing:
            parts.append(f"{len(missing)} expected table(s) not in any publication")
        if identity_problems:
            parts.append(f"{len(identity_problems)} published table(s) cannot replicate UPDATE/DELETE")
        summary = "Publication coverage gaps: " + "; ".join(parts) + "."
    else:
        severity = "ok"
        summary = "All expected tables are published and have a usable replica identity."

    return {
        "severity": severity,
        "summary": summary,
        "missing_from_publication": [f"{s}.{t}" for s, t in missing],
        "replica_identity_problems": identity_problems,
        "published_count": len(published),
        "expected_count": len(expected),
    }


def diagnose_stuck_subscription(
    subscriptions: list[dict[str, Any]],
    errors: list[dict[str, Any]],
) -> dict[str, Any]:
    """Correlate subscription state with error counts to explain a wedge.

    The classic failure: the apply worker hits a row that conflicts with a
    pre-existing row on the subscriber (usually a unique/primary-key collision),
    errors out, restarts, hits the same row, and loops — apply never advances
    past one LSN.

    This works from a catalog snapshot, so it identifies *which* subscription is
    wedged and the LSN apply is stalled at, and lays out the two real options.
    The exact conflicting row comes from the subscriber's server log (the
    ``ERROR: duplicate key value ...`` line) — we say so rather than pretending
    the catalog exposes it.
    """
    errors_by_name = {e["subname"]: e for e in errors}
    findings: list[dict[str, Any]] = []

    for sub in subscriptions:
        name = sub.get("subname")
        err = errors_by_name.get(name, {})
        apply_errors = err.get("apply_error_count") or 0
        enabled = sub.get("subenabled")
        worker_present = sub.get("pid") is not None
        wedged_lsn = sub.get("received_lsn") or sub.get("latest_end_lsn")

        stuck = bool(enabled and (apply_errors > 0 or not worker_present))
        if not stuck:
            findings.append(
                {
                    "subscription": name,
                    "severity": "ok",
                    "stuck": False,
                    "summary": f"Subscription '{name}' shows no apply error and a {'running' if worker_present else 'disabled'} worker.",
                }
            )
            continue

        findings.append(
            {
                "subscription": name,
                "severity": "critical",
                "stuck": True,
                "apply_error_count": apply_errors,
                "worker_running": worker_present,
                "wedged_at_lsn": wedged_lsn,
                "summary": (
                    f"Subscription '{name}' is wedged: apply has logged {apply_errors} error(s) "
                    f"and is not advancing past LSN {wedged_lsn}. The usual cause is a "
                    f"unique/primary-key conflict on the subscriber — the publisher's row "
                    f"collides with a row already present locally."
                ),
                "confirm_with": (
                    "Check the SUBSCRIBER server log for the apply-worker ERROR line "
                    "(e.g. 'duplicate key value violates unique constraint ...'); it names "
                    "the table, constraint, and conflicting key."
                ),
                "options": [
                    {
                        "option": "resolve_the_row",
                        "description": (
                            "Fix the data: delete or reconcile the conflicting row on the "
                            "subscriber so the incoming change applies cleanly. Preferred when "
                            "the local row is wrong — no data is discarded."
                        ),
                    },
                    {
                        "option": "skip_the_transaction",
                        "description": (
                            "Skip the offending transaction with skip_apply_transaction "
                            f"(ALTER SUBSCRIPTION {name} SKIP (lsn = '{wedged_lsn}')). This "
                            "DISCARDS that change permanently — only safe when you've confirmed "
                            "the change is genuinely redundant. Pass the exact LSN; this tool "
                            "will not guess it for you."
                        ),
                        "candidate_lsn": wedged_lsn,
                    },
                ],
            }
        )

    stuck_any = [f for f in findings if f.get("stuck")]
    return {
        "severity": "critical" if stuck_any else "ok",
        "summary": (
            f"{len(stuck_any)} subscription(s) wedged."
            if stuck_any
            else "No subscription appears wedged."
        ),
        "subscriptions": findings,
    }
