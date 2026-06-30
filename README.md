# pg-logical-mcp

> Diagnose Postgres logical replication: slot WAL retention, walsender lag, stuck subscriptions, with gated remediation.

An MCP server that lets an AI agent introspect and reason about a PostgreSQL
logical replication / CDC setup — replication slots and WAL retention, walsender
and decoding pressure, publications, subscriptions, and stuck-apply diagnosis —
plus a small set of **gated** remediation tools.

It deliberately does **not** compete in the crowded "query optimizer / index
advisor" MCP space. It targets the replication/CDC layer, which is largely
untooled and where a lot of real production incidents come from.

## The design point: deterministic gate + judgment

Two layers, explicitly separated:

1. **Deterministic tools** return raw catalog/stats state. They keep the agent
   grounded in facts and are individually testable. (`list_replication_slots`,
   `inspect_walsenders`, `decoding_stats`, `list_publications`,
   `inspect_subscriptions`, `subscription_errors`, `peek_changes`.)
2. **Judgment tools** synthesise that state into "here's what's wrong and what
   you can do." This is where the reasoning earns its place. (`assess_slot_risk`,
   `check_publication_coverage`, `diagnose_stuck_subscription`.)

Dangerous operations sit behind **deterministic guardrails**: the model can
*recommend* a slot drop or a transaction skip, but is structurally prevented
from fat-fingering a data-losing operation. This is the same
deterministic-gate-plus-judgment split used in the companion pre-commit-review
hook — the two pieces read as one coherent point of view: let the model reason,
but put the irreversible levers behind a deterministic lock it cannot pick.

The judgment logic lives in `diagnostics.py` as **pure functions** over provider
output, so it is unit-tested with no live database (`tests/test_diagnostics.py`).

## Tools

### Slot health & WAL retention (run against the publisher)
- **`list_replication_slots`** *(deterministic)* — `pg_replication_slots` plus WAL pinned per slot, `wal_status`, `safe_wal_size`, active state, holding pid.
- **`assess_slot_risk`** *(judgment)* — samples the WAL generation rate and, against `max_slot_wal_keep_size`, projects which slot is closest to invalidation and a rough time-to-invalidation / time-to-disk-fill. The differentiated diagnostic no incumbent does.

### Publisher side
- **`inspect_walsenders`** *(deterministic)* — `pg_stat_replication`: state, write/flush/replay lag (intervals and LSN diffs), `sync_state`.
- **`decoding_stats`** *(deterministic)* — `pg_stat_replication_slots`: `spill_txns`/`spill_bytes` and streaming stats. Surfaces large-transaction decode spill, a real, obscure slowness cause.

### Publications
- **`list_publications`** *(deterministic)* — `pg_publication` / `pg_publication_tables` with row filters, column lists, published operations.
- **`check_publication_coverage`** *(judgment)* — flags the two silent failures: a table never added to the publication, and a published table whose `REPLICA IDENTITY` won't support UPDATE/DELETE. Both lose data quietly rather than erroring.

### Subscriber side (run against the subscriber)
- **`inspect_subscriptions`** *(deterministic)* — `pg_subscription` joined with `pg_stat_subscription`. The connection string is omitted (it holds a password).
- **`subscription_errors`** *(deterministic)* — `pg_stat_subscription_stats` apply/sync error counts.
- **`diagnose_stuck_subscription`** *(judgment, marquee)* — correlates error state with the LSN apply is wedged at, explains *why* apply is blocked (typically a unique-constraint conflict on the subscriber), and lays out the two real options: resolve the conflicting row, or skip the offending transaction.

### CDC stream inspection
- **`peek_changes`** *(deterministic)* — wraps `pg_logical_slot_peek_changes` (the **peek** variant, not `get`), so inspecting the queue does not consume it or advance the slot.

### Gated remediation
- **`advance_slot`**, **`skip_apply_transaction`**, **`drop_slot`** — each mutates irreversible state. Every one:
  - is **read-only unless** the server was started with `PG_LOGICAL_MCP_ALLOW_WRITES` set **and** the call passes `allow_writes=true` (a two-key gate the model can't fully turn on its own);
  - requires the **exact** slot name / subscription name and the **exact** LSN — there is no "advance to latest" / "skip to head" convenience that silently discards an unknown amount of data;
  - returns a **dry-run preview** before it will execute.

## Install & configure

Published to PyPI; run it with `uvx` or `pipx` — no clone required:

```jsonc
// MCP client config — one entry per node you want to inspect.
{
  "mcpServers": {
    "pg-publisher": {
      "command": "uvx",
      "args": ["pg-logical-mcp"],
      "env": { "PG_LOGICAL_MCP_DSN": "host=publisher.internal port=5432 user=replmon dbname=appdb" }
    },
    "pg-subscriber": {
      "command": "uvx",
      "args": ["pg-logical-mcp"],
      "env": { "PG_LOGICAL_MCP_DSN": "host=subscriber.internal port=5432 user=replmon dbname=appdb" }
    }
  }
}
```

Each server points at **one** node. Slots/walsenders/publications live on the
publisher; subscriptions live on the subscriber — so to see both sides, add both
entries. Connection details come from `PG_LOGICAL_MCP_DSN` (or the standard
`PG*` libpq env vars). Provide the password via `.pgpass` or `PGPASSWORD` rather
than embedding it where it might be logged.

To enable the remediation tools, add `"PG_LOGICAL_MCP_ALLOW_WRITES": "1"` to that
server's `env`. Leave it unset and the server is strictly read-only.

## Privileges (stated plainly)

Reading `pg_subscription` and the subscription stats views needs **elevated
privileges**. The demo containers run as the `postgres` superuser for simplicity.
**Production use should use a deliberately scoped role, not superuser** — grant
only what the tools read (`pg_monitor` covers most stats views; reading
`pg_subscription` and using the replication functions needs more). Treat the
remediation tools as privileged operations and gate them at the role level too,
not just with `PG_LOGICAL_MCP_ALLOW_WRITES`.

## Hosting: local only, by design

**The author hosts nothing.** This ships as a local subprocess your MCP client
launches over stdio. This tool needs elevated privileges and a path into the
replication subsystem — often in production. A hosted model would mean the author
holding your credentials and a route into your production database: unacceptable
exposure for everyone. Local means the author never touches a credential or a
customer database. If your team later wants a shared deployment, MCP's HTTP
transport lets *you* self-host it near your own database — your deployment
decision, not a hosted service.

## Demo: see it work in ~2 minutes

Local containers, no infrastructure of your own touched:

```bash
docker compose -f docker/docker-compose.yml up -d   # publisher :5433, subscriber :5434

# Scenario 1 — wedge a subscription on a primary-key conflict
python scenarios/stuck_subscription.py
#   then ask the agent: run diagnose_stuck_subscription against the subscriber

# Scenario 2 — pin a growing pile of WAL behind an inactive slot
python scenarios/slot_retention.py
#   then ask the agent: run assess_slot_risk against the publisher
python scenarios/slot_retention.py --recover        # restart subscriber, drain the slot
```

Point the MCP client's `pg-publisher` entry at `host=localhost port=5433` and
`pg-subscriber` at `host=localhost port=5434` (user/password `postgres`, db
`appdb`) to drive the tools against the demo.

## Development

```bash
uv venv && uv pip install -e ".[dev]"
pytest                      # exercises the pure judgment layer, no DB needed
```

See [CLAUDE.md](CLAUDE.md) for architecture and conventions.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
