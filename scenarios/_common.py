"""Shared helpers for the demo scenarios.

The scenarios talk to the demo containers directly with psycopg to *create* the
failure state, then point you at the MCP tools that diagnose it. They default to
the docker-compose port mapping (publisher 5433, subscriber 5434) and can be
overridden with env vars.
"""

from __future__ import annotations

import os

import psycopg

PUBLISHER_DSN = os.environ.get(
    "DEMO_PUBLISHER_DSN", "host=localhost port=5433 user=postgres password=postgres dbname=appdb"
)
SUBSCRIBER_DSN = os.environ.get(
    "DEMO_SUBSCRIBER_DSN", "host=localhost port=5434 user=postgres password=postgres dbname=appdb"
)


def connect(dsn: str) -> psycopg.Connection:
    return psycopg.connect(dsn, autocommit=True)


def banner(text: str) -> None:
    line = "=" * len(text)
    print(f"\n{line}\n{text}\n{line}")


def show_rows(title: str, rows: list[dict]) -> None:
    print(f"\n-- {title} --")
    if not rows:
        print("(none)")
        return
    for row in rows:
        print("  " + ", ".join(f"{k}={v}" for k, v in row.items()))
