# Task230: Submit/Ack/Fill Orderbook Snapshot Capture Source

## Current Behavior

Task229 made execution raw payloads able to preserve `pre_event_orderbook_snapshot_ref` and
`post_event_orderbook_snapshot_ref` when an upstream caller already provides them. Current live
execution still has no bounded source that assigns those refs from the existing orderbook truth
tables, so lifecycle stages normally remain `capture_status=missing`.

## Objective

Add one minimal, read-only source for submit/ack/fill lifecycle orderbook snapshot refs by linking
execution events to existing BTC-USDT-SWAP bronze orderbook rows.

## Input

- `OrderState` submit/ack lifecycle events persisted through `ConvergedPostgresExecutionRepository`.
- `FillEvent` rows persisted through `ConvergedPostgresExecutionRepository`.
- Existing RDP bronze orderbook rows:
  - `bronze.market_orderbook_books5`
  - `bronze.market_orderbook_bbo`

## Output

- Existing execution raw payload shape remains unchanged.
- `lifecycle_snapshot_refs.<stage>.market_context_snapshot_refs` receives:
  - `pre_event_orderbook_snapshot_ref`
  - `post_event_orderbook_snapshot_ref`
- If no real bronze row exists, refs remain `None` and the existing payload marks the refs missing.

## Scope

In scope:
- Query only persisted bronze orderbook rows.
- Prefer `books5`, fallback to `bbo`.
- Use a bounded time window around the execution event timestamp.
- Fail soft if the bronze schema/table is unavailable.
- Preserve explicit refs supplied by upstream payloads.

Out of scope:
- No new database schema.
- No orderbook reconstruction.
- No strategy, symbol, venue, family, timeframe, gate, release, or promotion change.
- No synthetic refs.

## Validation

- Unit tests cover captured refs, explicit-ref precedence, and missing/failing source behavior.
- Narrow pytest for the new tests and existing snapshot-ref plumbing must pass.
- Full unit suite should pass before commit/deploy when time permits.

## Rollback

Revert the resolver module and repository call sites. Existing raw payload fields remain compatible;
missing refs will again be represented by the existing `capture_status=missing` payload.
