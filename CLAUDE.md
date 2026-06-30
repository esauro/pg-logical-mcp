# CLAUDE.md

Guidance for working in this repo. The authoritative design intent is in
`DESIGN.md` — read it before making structural changes; the philosophy there is
the point of the project and must not be diluted.

## What this is

An MCP server for diagnosing PostgreSQL **logical replication / CDC** — slots and
WAL retention, walsender/decoding pressure, publications, subscriptions, stuck
apply — plus gated remediation. It does **not** do query/index optimization;
that space is already crowded and is explicitly out of scope.

## The non-negotiable design split

Keep these two layers separate. It is the whole point:

1. **Deterministic tools** wrap a catalog/stats view and return raw facts. No
   interpretation. Individually testable.
2. **Judgment tools** synthesise facts into a diagnosis + options. All judgment
   logic lives in `diagnostics.py` as **pure functions** over provider output —
   no DB calls — so it is unit-tested against canned dicts.

**Irreversible operations sit behind deterministic guardrails**, not behind the
model's good judgment. A judgment tool may *recommend* a drop/skip/advance; the
remediation tool that performs it enforces: env switch + per-call flag + exact
arguments + dry-run preview. If you touch `tools/remediation.py`, do not weaken
any of those four. Never add an "advance to latest" / "skip to head" convenience.

## Layout

```
src/pg_logical_mcp/
  server.py            # FastMCP entrypoint; build_server() wires tools; main() serves stdio
  providers/
    base.py            # ReplicationProvider protocol (typing.Protocol)
    postgres.py        # PostgresProvider — the ONLY implementation
  tools/               # one module per tool group; each exposes register(mcp, provider)
    slots.py publisher.py publications.py subscriptions.py cdc.py remediation.py
  diagnostics.py       # judgment layer: PURE functions, no DB
docker/                # docker-compose + init SQL for the publisher/subscriber demo
scenarios/             # stuck_subscription.py, slot_retention.py
tests/                 # unit tests for diagnostics.py
```

> The brief sketches a flat `src/` tree; the code lives under
> `src/pg_logical_mcp/` so it's a clean importable package for the `uvx`/`pipx`
> console-script entrypoint (`pg-logical-mcp = pg_logical_mcp.server:main`).
> Module names match the brief exactly.

## Conventions

- **Postgres-only.** Keep the thin `ReplicationProvider` protocol with a single
  `PostgresProvider` behind it (testability + a future seam). Do **not** build,
  stub, or note MySQL/binlog support. No `NotImplementedError` placeholders.
- **Provider returns JSON-serialisable dicts/lists.** LSNs come back as text
  (`::text`), byte diffs as `bigint`. `diagnostics.py` consumes exactly these
  shapes — keep them in sync when you add a field.
- **Tools are thin.** A tool reads from the provider and (for judgment tools)
  hands the data to a `diagnostics` function. Put real logic in `diagnostics`,
  where it can be tested without a database.
- **Adding a tool:** add the provider method (+ protocol entry), write the
  `@mcp.tool()` in the right `tools/*.py` module, and — if it's judgment — a pure
  function in `diagnostics.py` with a unit test. Registration happens
  automatically via `tools/__init__.py:REGISTRARS`.
- **Never surface secrets.** `subconninfo` holds a password; it is deliberately
  not selected in `inspect_subscriptions`. Keep it that way.
- A provider points at one node. Publisher-side vs subscriber-side tools are
  documented as such in each docstring; don't assume one connection sees both.

## Privileges & safety

- Reading `pg_subscription`/subscription stats needs elevated privileges. The
  demo runs as superuser for simplicity; docs must keep saying production wants a
  scoped role. State the caveat, don't bury it.
- Remediation tools default to dry-run and are inert unless
  `PG_LOGICAL_MCP_ALLOW_WRITES` is set in the environment.

## Commands

```bash
uv venv && uv pip install -e ".[dev]"
pytest                                            # judgment layer; no DB required
docker compose -f docker/docker-compose.yml up -d # demo pair (pub :5433, sub :5434)
python scenarios/stuck_subscription.py            # scenario 1
python scenarios/slot_retention.py                # scenario 2  (--recover to undo)
python -m pg_logical_mcp.server                   # run the server over stdio (needs a DSN)
```
