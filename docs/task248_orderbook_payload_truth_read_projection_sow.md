# Task 248 · Orderbook Payload Truth Read Projection SOW

## Business Objectives and Boundaries

Expose the persisted `bronze.market_orderbook_payloads` sidecar as read-only execution-science evidence for OKX `BTC-USDT-SWAP`. The goal is to make local orderbook payload, collector sequence, exchange sequence, row checksum, and payload hash truth visible to operator/truth-chain surfaces before depth-aware fill reconstruction.

This task is read-only. It does not change strategy logic, risk gates, execution gates, order placement, provider behavior, symbol, venue, strategy family, release, promotion, tuning, schema, or runtime timeframe plumbing.

## Module Responsibilities and Domain Model

- `aats.services.execution_engine.orderbook_snapshot_refs` resolves orderbook snapshot refs into persisted bronze row truth and sidecar payload evidence.
- `aats.services.operator.query_service` aggregates resolved row and sidecar evidence into decision execution-science truth-chain payloads.
- `bronze.market_orderbook_payloads` remains the sole sidecar table for payload evidence.

Domain model:

- Snapshot row: sampled `bronze.market_orderbook_bbo` or `bronze.market_orderbook_books5` row.
- Sidecar row: payload evidence keyed by `(snapshot_table, symbol, ts, row_checksum)`.
- Read projection: non-sensitive sidecar fields only; raw payload is intentionally not exposed.

## Input / Output Interfaces

Input:

- Existing lifecycle market context orderbook snapshot refs.
- Existing read-only market context DB source.
- Existing sidecar rows in `bronze.market_orderbook_payloads`.

Output:

- `payload_evidence` object on resolved orderbook row payloads.
- Operator execution-science sequence status `local_snapshot_row_sequence_validated_diff_payload_persisted` when both pre/post sidecar payload hashes exist.
- Explicit missing evidence when sidecar rows or payload hashes are absent.

## Database Schema / Tables / Indexes / Constraints

No schema change. The task reads:

- `bronze.market_orderbook_bbo`
- `bronze.market_orderbook_books5`
- `bronze.market_orderbook_payloads`

The sidecar lookup uses the existing primary identity:

- `snapshot_table`
- `symbol`
- `ts`
- `row_checksum`

## Transactions, Consistency, Concurrency

The read source is created with PostgreSQL read-only transaction options. Sidecar lookup is performed inside the same read session as the snapshot row lookup. Lookup is fail-soft: if the sidecar table is unavailable or the query fails, execution persistence and operator API rendering must not fail.

## Authorization, Authentication, Data Security

No credentials are read or printed. The projection excludes `raw_payload` by design and only exposes hashes, sequence ids, capture status, and timestamps. `raw_payload_exposed=false` is included as an explicit contract marker.

## Error Handling and Idempotency

The resolver reports:

- `orderbook_payload_sidecar_missing` when the sidecar row is absent.
- `orderbook_payload_sidecar_lookup_failed` when the sidecar lookup fails.
- `orderbook_diff_payload_not_persisted` or `orderbook_payload_hash_missing` when the sidecar row is incomplete for diff truth.

Read projection is idempotent and has no write side effects.

## State Transition and Lifecycle

No runtime state transition is introduced. Sequence validation now distinguishes:

- row truth missing
- row truth present but sidecar payload missing
- row truth and sidecar payload persisted

## Caching and Performance

The sidecar lookup is a single indexed point lookup per resolved snapshot row. The existing query result cap in operator truth-chain output is unchanged.

## Logging, Monitoring, Auditing

No new logs are required. The audit surface is the returned `payload_evidence` and sequence validation status. Missing evidence remains explicit in API payloads.

## Testing Strategy

Unit coverage:

- Snapshot row resolver projects sidecar evidence without raw payload.
- Resolver reports explicit sidecar missing evidence.
- Operator truth-chain sequence validation becomes persisted when pre/post sidecar rows exist.
- Existing missing sidecar semantics remain backward-compatible.

## Migration, Rollback, Compatibility

No migration. Rollback is `git revert` of the read projection commit and redeploy with `scripts/deploy.sh`. Existing sidecar write path and table remain compatible.

## Configuration and Environment Isolation

No new configuration. The task uses the existing read source resolution and read-only DB connection policy.

## Code Organization and Dependencies

The implementation stays in existing execution-engine ref resolution and operator query-service modules. It reuses the existing sidecar contract constants.

## Documentation and Operations Manual

Operators should treat `payload_evidence.complete=true` as proof that the sidecar payload hash and collector/exchange sequence were available for that snapshot ref. It is not proof of trade profitability, alpha quality, or release readiness.

## Deployment and Acceptance Criteria

Acceptance:

- Existing snapshot refs can report whether sidecar payload evidence exists.
- Projection includes channel, collector sequence, exchange sequence id, payload hash, capture status, and timestamps.
- Raw payload is not exposed by default.
- Missing sidecar evidence is explicit.
- No live order behavior changes are introduced.
