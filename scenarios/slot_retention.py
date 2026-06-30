"""Scenario 2 — slot retention disaster.

Stops the subscriber so its slot goes inactive, then runs a pile of writes on
the publisher. The slot's restart_lsn freezes while WAL piles up behind it. Call
`assess_slot_risk` against the publisher to watch the time-to-invalidation
projection shrink.

The demo containers set max_slot_wal_keep_size=64MB, so with enough writes the
slot's wal_status actually flips to `lost` — a genuinely invalidated slot.

    python scenarios/slot_retention.py
    python scenarios/slot_retention.py --recover   # restart subscriber, let it catch up

Stopping/starting the subscriber uses `docker`; if that's unavailable the script
prints the command for you to run by hand.
"""

from __future__ import annotations

import subprocess
import sys
import time

from psycopg.rows import dict_row

from _common import PUBLISHER_DSN, banner, connect, show_rows

SUBSCRIBER_CONTAINER = "pg-logical-subscriber"
WRITE_BATCHES = 40
ROWS_PER_BATCH = 20_000


def _docker(*args: str) -> bool:
    try:
        subprocess.run(["docker", *args], check=True, capture_output=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"  (could not run `docker {' '.join(args)}`: {exc}); do it manually.")
        return False


def _show_slots() -> None:
    with connect(PUBLISHER_DSN) as pub:
        pub.row_factory = dict_row
        rows = pub.execute(
            "SELECT slot_name, active, wal_status, "
            "pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS pinned_wal, "
            "safe_wal_size "
            "FROM pg_replication_slots"
        ).fetchall()
        show_rows("pg_replication_slots (publisher)", rows)


def recover() -> None:
    banner("Recovery: restarting the subscriber so it reconnects and drains the slot")
    _docker("start", SUBSCRIBER_CONTAINER)
    print("subscriber starting; give it a few seconds to reconnect, then re-check the slot.")
    time.sleep(8)
    _show_slots()


def main() -> None:
    if "--recover" in sys.argv:
        recover()
        return

    banner("Scenario 2: an inactive slot pins a growing pile of WAL")

    print(f"\n[1] stopping the subscriber ({SUBSCRIBER_CONTAINER}) so its slot goes inactive")
    _docker("stop", SUBSCRIBER_CONTAINER)
    _show_slots()

    print(f"\n[2] generating WAL on the publisher ({WRITE_BATCHES} x {ROWS_PER_BATCH:,} rows)")
    with connect(PUBLISHER_DSN) as pub:
        pub.execute("CREATE TABLE IF NOT EXISTS wal_filler (id bigint, payload text)")
        for i in range(WRITE_BATCHES):
            pub.execute(
                "INSERT INTO wal_filler SELECT g, repeat('x', 200) "
                "FROM generate_series(1, %s) g",
                (ROWS_PER_BATCH,),
            )
            pub.execute("DELETE FROM wal_filler")
            if i % 5 == 0:
                print(f"    batch {i + 1}/{WRITE_BATCHES}")
    _show_slots()

    banner("Now ask the agent: run assess_slot_risk against the PUBLISHER")
    print(
        "It samples the WAL rate and projects time-to-invalidation for the pinned slot.\n"
        "If wal_status shows 'lost', the slot is already invalidated — recreate it and\n"
        "re-sync the subscriber. Otherwise recover with:\n"
        "  python scenarios/slot_retention.py --recover"
    )


if __name__ == "__main__":
    main()
