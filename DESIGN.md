# Postgres Logical Replication MCP — Working Brief

> **For Claude Code:** This is an executable design brief, not just documentation. Build in the order given under "Build order". The guiding design philosophy is in the next section — keep it intact, because it is the point of the project.

## What this is

An MCP server that lets an AI agent introspect and reason about a PostgreSQL logical replication / CDC setup: replication slots and WAL retention, walsender and decoding pressure, publications, subscriptions, and stuck-apply diagnosis — plus a small set of *gated* remediation tools.

It deliberately does **not** compete in the crowded "query optimizer / index advisor" MCP space (Postgres MCP Pro, PostgreSQL Analytics MCP, etc. already own that). It targets the replication/CDC layer, which is largely untooled and where real production incidents come from.

## Design philosophy (do not dilute)

Two layers, explicitly separated:

1. **Deterministic tools** return raw catalog/stats state. They keep the agent grounded in facts and are individually testable.
2. **Judgment tools** synthesise that state into "here's what's wrong and what you can do." This is where the reasoning earns its place.

Dangerous operations sit behind **deterministic guardrails**: the LLM can *recommend* a slot drop or transaction skip, but is structurally prevented from fat-fingering a data-losing operation. This is the same deterministic-gate-plus-judgment split used in the companion pre-commit-review hook; state it in the README so the two pieces read as one coherent point of view.

Scope is **Postgres-only**. Keep a thin `ReplicationProvider` protocol with a single `PostgresProvider` behind it so the tools are decoupled from raw catalog queries (testability + a future seam) — but do **not** build, stub, or add MySQL/binlog notes. No `NotImplementedError` placeholder.

## Architecture

```
src/
  server.py              # MCP server entrypoint, tool registration, stdio transport
  providers/
    base.py              # ReplicationProvider protocol
    postgres.py          # PostgresProvider — the only implementation
  tools/
    slots.py             # slot health + WAL retention
    publisher.py         # walsenders + decoding stats
    publications.py      # publication coverage
    subscriptions.py     # subscription state + stuck-apply diagnosis
    cdc.py               # non-destructive change peek
    remediation.py       # gated, irreversible operations
  diagnostics.py         # judgment-layer logic (pure functions over provider output)
docker/
  docker-compose.yml     # pg-publisher + pg-subscriber, wal_level=logical
  init-publisher.sql
  init-subscriber.sql
scenarios/
  stuck_subscription.py  # scenario 1
  slot_retention.py      # scenario 2
README.md
pyproject.toml           # packaged for PyPI; runnable via uvx/pipx
```

`ReplicationProvider` is a `typing.Protocol`. Catalog reads and diagnostics are methods on it. Judgment logic in `diagnostics.py` should be pure functions taking provider output and returning structured findings, so it can be unit-tested without a live database.

## Tool surface

### Slot health & WAL retention

- **`list_replication_slots`** (deterministic) — wraps `pg_replication_slots`, joining in WAL pinned per slot via `pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)`, plus `wal_status` (reserved/extended/unreserved/lost), `safe_wal_size`, active state and holding pid.
- **`assess_slot_risk`** (judgment) — takes pinned-WAL figures, current WAL generation rate (sample `pg_current_wal_lsn()` twice with a short interval), and `max_slot_wal_keep_size`; returns which slot is closest to invalidation and a rough time-to-invalidation / time-to-disk-fill projection. This is the differentiated diagnostic no incumbent does.

### Publisher side

- **`inspect_walsenders`** (deterministic) — reads `pg_stat_replication`: per-connection state, write/flush/replay lag intervals and their LSN diffs, `sync_state`.
- **`decoding_stats`** (deterministic) — reads `pg_stat_replication_slots` for `spill_txns`/`spill_bytes` and streaming stats. Surfaces large-transaction decode spill (a real, obscure slowness cause — ties to outbox batches decoding badly).

### Publications

- **`list_publications`** (deterministic) — enumerates `pg_publication` and `pg_publication_tables` with row filters, column lists, published operations.
- **`check_publication_coverage`** (judgment) — cross-checks published tables against an expected set; flags the two silent failures: a table never added to the publication, and a table whose `REPLICA IDENTITY` won't support UPDATE/DELETE replication. Both lose data quietly rather than erroring.

### Subscriber side

- **`inspect_subscriptions`** (deterministic) — joins `pg_subscription` with `pg_stat_subscription` for worker state and apply-lag signals (`last_msg_send_time` vs `last_msg_receipt_time`, `latest_end_lsn`).
- **`subscription_errors`** (deterministic) — reads `pg_stat_subscription_stats` for apply and sync error counts.
- **`diagnose_stuck_subscription`** (judgment) — **marquee tool.** Correlates error state with the LSN apply is wedged at and the conflicting row; explains *why* apply is blocked (typically a unique-constraint conflict on the subscriber); lays out the two real options (resolve the conflicting row, or skip the offending transaction).

### CDC stream inspection

- **`peek_changes`** (deterministic) — wraps `pg_logical_slot_peek_changes` (the *peek* variant, **not** `get`), so inspecting the queue does not consume it or advance the slot.

### Gated remediation

- **`advance_slot`**, **`skip_apply_transaction`**, **`drop_slot`** — all mutate irreversible state. Each:
  - requires an explicit `allow_writes` flag,
  - requires the caller to pass the exact slot name and LSN it expects (no "advance to latest" convenience that silently discards data),
  - returns a dry-run preview before executing.

## Demo wiring

`docker/docker-compose.yml`: two services, `pg-publisher` and `pg-subscriber`, both `wal_level=logical`. Init scripts create a table + publication on the publisher and a subscription on the subscriber. Whole thing comes up with `docker compose up`; one script per scenario. Target: an evaluator sees it work in ~2 minutes without touching their own infrastructure.

**Scenario 1 — stuck subscription (`scenarios/stuck_subscription.py`):** seed a conflicting row on the subscriber so a later publisher insert collides on the primary key; the apply worker errors and the subscription wedges; `diagnose_stuck_subscription` identifies the conflict and proposes the skip.

**Scenario 2 — slot retention disaster (`scenarios/slot_retention.py`):** `docker stop pg-subscriber`, run writes on the publisher, watch `restart_lsn` pin a growing pile of WAL while `assess_slot_risk` projects time-to-invalidation. Then either restart the subscriber to recover, or let `max_slot_wal_keep_size` trip and show `wal_status` flip to `lost` — the slot actually invalidated.

## Distribution

Local / self-hosted only — **the author hosts nothing.** Ship as a local subprocess the MCP client launches over stdio. Publish to PyPI so install is a one-liner (`uvx` / `pipx`); the user adds a config block to their MCP client pointing at their own database with their own credentials. The demo runs entirely in local containers.

Rationale to state in the README: this tool needs elevated privileges and a path into the replication subsystem, often production. A hosted model would mean the author holding credentials and a route into other people's production databases — unacceptable exposure for everyone. Local means the author never touches a credential or a customer database. (If a team later wants a shared deployment, MCP's HTTP transport lets *them* self-host it near their own database — their deployment decision, not a hosted service.)

## Privilege caveat (document honestly)

Reading `pg_subscription` and subscription stats needs elevated privileges. The demo containers run as a superuser role for simplicity; the README must say plainly that production use needs a deliberately scoped role rather than superuser. State it rather than letting a reviewer discover it.

## Build order

1. `ReplicationProvider` protocol (`providers/base.py`) and `PostgresProvider` skeleton (`providers/postgres.py`) with connection handling.
2. Deterministic read tools, in this order: `list_replication_slots`, `inspect_walsenders`, `decoding_stats`, `list_publications`, `inspect_subscriptions`, `subscription_errors`, `peek_changes`. Wire each into `server.py` as it lands.
3. `diagnostics.py` judgment functions (pure, unit-tested) feeding `assess_slot_risk`, `check_publication_coverage`, `diagnose_stuck_subscription`.
4. Gated remediation tools (`advance_slot`, `skip_apply_transaction`, `drop_slot`) with the dry-run + explicit-LSN + `allow_writes` guardrails.
5. Demo: `docker-compose.yml` + init SQL, then the two scenario scripts.
6. README: lead with the one-line description and the deterministic-gate-plus-judgment philosophy; include install/config block, the privilege caveat, and the local-only hosting rationale.

## README opening line (use or adapt)

> Diagnose Postgres logical replication: slot WAL retention, walsender lag, stuck subscriptions, with gated remediation.
