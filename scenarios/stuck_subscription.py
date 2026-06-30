"""Scenario 1 — stuck subscription.

Seeds a row on the SUBSCRIBER that will collide with a later publisher insert on
the primary key. The publisher insert replicates, the apply worker hits a
duplicate-key error, and the subscription wedges: apply stops advancing and
apply_error_count climbs.

Then point your MCP client at the subscriber and call `diagnose_stuck_subscription`
— it identifies the conflict and proposes the skip.

    python scenarios/stuck_subscription.py
    python scenarios/stuck_subscription.py --recover   # resolve the row, unwedge apply

The normal run is idempotent: it resolves any leftover wedge and clears id=9001
from both nodes before seeding, so you can run it repeatedly.
"""

from __future__ import annotations

import sys
import time

from psycopg.rows import dict_row

from _common import PUBLISHER_DSN, SUBSCRIBER_DSN, banner, connect, show_rows

CONFLICT_ID = 9001
SUBSCRIPTION = "app_sub"


def clean_slate() -> None:
    """Return to a known-good state: no id=9001 anywhere, apply not wedged.

    Deleting the conflicting row on the subscriber lets a stuck apply of the
    publisher's id=9001 drain; deleting it on the publisher then replicates a
    delete downstream so both nodes end clean. Safe to call when nothing is
    wrong — the deletes just match no rows.
    """
    with connect(SUBSCRIBER_DSN) as sub:
        sub.execute(f"ALTER SUBSCRIPTION {SUBSCRIPTION} ENABLE")  # no-op if already enabled
        sub.execute("DELETE FROM orders WHERE id = %s", (CONFLICT_ID,))
    with connect(PUBLISHER_DSN) as pub:
        pub.execute("DELETE FROM orders WHERE id = %s", (CONFLICT_ID,))
    time.sleep(3)  # let the publisher's delete replicate so the row is gone on both sides


def recover() -> None:
    banner("Recovery: resolving the conflict so apply un-wedges")
    print(f"[subscriber] deleting id={CONFLICT_ID} and re-enabling {SUBSCRIPTION}")
    clean_slate()
    with connect(SUBSCRIBER_DSN) as sub:
        sub.row_factory = dict_row
        stats = sub.execute(
            "SELECT subname, apply_error_count, sync_error_count FROM pg_stat_subscription_stats"
        ).fetchall()
        show_rows("pg_stat_subscription_stats (subscriber)", stats)
    print("\napply should be advancing again; error counts stop climbing once it catches up.")


def main() -> None:
    if "--recover" in sys.argv:
        recover()
        return

    banner("Scenario 1: wedging a subscription on a primary-key conflict")

    print("\n[reset] clearing any leftover id=9001 / prior wedge before seeding")
    clean_slate()

    with connect(SUBSCRIBER_DSN) as sub:
        print(f"\n[subscriber] seeding conflicting row id={CONFLICT_ID} BEFORE the publisher sends it")
        sub.execute(
            "INSERT INTO orders (id, customer, amount_cents) VALUES (%s, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET customer = EXCLUDED.customer",
            (CONFLICT_ID, "local-conflict", 1),
        )

    with connect(PUBLISHER_DSN) as pub:
        print(f"[publisher]  inserting id={CONFLICT_ID} — this will collide downstream")
        pub.execute(
            "INSERT INTO orders (id, customer, amount_cents) VALUES (%s, %s, %s)",
            (CONFLICT_ID, "from-publisher", 5000),
        )

    print("\nwaiting for the apply worker to hit the conflict and error...")
    time.sleep(6)

    with connect(SUBSCRIBER_DSN) as sub:
        sub.row_factory = dict_row
        errors = sub.execute(
            "SELECT subname, apply_error_count, sync_error_count FROM pg_stat_subscription_stats"
        ).fetchall()
        show_rows("pg_stat_subscription_stats (subscriber)", errors)
        state = sub.execute(
            "SELECT s.subname, s.subenabled, st.pid, st.received_lsn::text, st.latest_end_lsn::text "
            "FROM pg_subscription s LEFT JOIN pg_stat_subscription st ON st.subid = s.oid"
        ).fetchall()
        show_rows("subscription state (subscriber)", state)

    banner("Now ask the agent: run diagnose_stuck_subscription against the SUBSCRIBER")
    print(
        "It will report the wedge, the stalled LSN, and the two options:\n"
        "  1. resolve the conflicting row (delete id=%s on the subscriber), or\n"
        "  2. skip_apply_transaction at the exact stalled LSN (discards that change).\n"
        "\nTo recover by resolving the row:\n"
        "  psql 'host=localhost port=5434 user=postgres password=postgres dbname=appdb' \\\n"
        "    -c 'DELETE FROM orders WHERE id = %s;'\n"
        "  -- then: ALTER SUBSCRIPTION app_sub ENABLE;  (if it disabled itself)" % (CONFLICT_ID, CONFLICT_ID)
    )


if __name__ == "__main__":
    main()
