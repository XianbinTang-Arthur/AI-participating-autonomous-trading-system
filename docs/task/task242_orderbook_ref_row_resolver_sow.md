# Task242: Read-Only Orderbook Ref Row Resolver

## Business Objectives And Boundaries

Task241 defined the contract for moving beyond snapshot-ref timestamp ordering.
This task implements the first safe read-only step: for each lifecycle
orderbook ref, prove whether the referenced bronze row exists, compute a stable
row checksum, and validate client/source timestamp ordering separately.

In scope:

- Read-only lookup of `bronze.market_orderbook_books5` and
  `bronze.market_orderbook_bbo` by exact `(symbol, ts)` from an existing ref.
- Deterministic checksum over already persisted flattened row fields.
- Operator truth-surface extension under
  `truth_chain.execution_science.sequence_validation`.
- Focused unit tests.

Out of scope:

- No schema migration.
- No collector write-path change.
- No raw diff payload persistence.
- No strategy, risk, execution gate, kill switch, provider, symbol, venue,
  family, release, promotion, tuning, or live order behavior change.

## Module Responsibilities And Domain Model

- `aats.services.execution_engine.orderbook_snapshot_refs` owns shared ref
  parsing, read-only row resolution, and checksum normalization.
- `OperatorQueryService` owns read-only presentation and sequence status
  aggregation.

Domain ladder:

1. snapshot refs present
2. snapshot-ref timestamps ordered
3. referenced bronze rows exist
4. row checksums computed
5. source/client timestamp ordering validated
6. full diff payload or collector sequence persisted

This task implements steps 3 through 5 and keeps step 6 as explicit missing
evidence.

## Input / Output Interfaces

Input:

- Existing lifecycle refs:
  - `pre_event_orderbook_snapshot_ref`
  - `post_event_orderbook_snapshot_ref`
- Existing read-only market-context source.
- Existing bronze rows.

Resolver output includes:

- `row_lookup_status`
- `row_exists`
- `source_ts`
- `received_at`
- `ingest_run_id`
- `content_checksum`
- `checksum_source`
- `checksum_version`
- `sequence_key`
- `missing_evidence`

Operator output adds:

- `ordered_by.snapshot_ref_ts`
- `ordered_by.source_ts`
- `ordered_by.client_ts`
- `source_delta_ms`
- `client_delta_ms`
- per-side row evidence in `pre` and `post`

## Database Schema / Tables / Indexes / Constraints

No schema change.

The resolver reads these existing tables:

- `bronze.market_orderbook_books5`
- `bronze.market_orderbook_bbo`

Lookup is exact by `(symbol, ts)` and must not scan broad time windows.

## Transactions, Consistency, Concurrency

The resolver uses the existing read-only market-context source. Source failure
is fail-soft and represented as `source_unavailable`. No write transaction is
opened.

## Authorization, Authentication, Data Security

No credentials, tokens, DB URLs, API keys, or connection strings are read or
printed. Operator output contains only non-secret source/table/symbol/timestamp,
ingest run id, and row checksum evidence.

## Error Handling And Idempotency

Fail-soft statuses:

- `source_unavailable`
- `row_missing`
- `symbol_mismatch`
- `unsupported_table`
- `unparseable_ref`
- `invalid_local_orderbook_sequence`

Repeated reads over the same row return the same checksum.

## State Transition And Lifecycle

No order state transitions are changed. Existing submit/ack/fill lifecycle refs
are inspected only.

## Caching And Performance

At most two exact row lookups are performed per lifecycle stage: pre and post.
No orderbook reconstruction, broad scans, or write-side cache mutation.

## Logging, Monitoring, Auditing

No new logs are required. The operator API response is the audit surface.

## Testing Strategy

Focused tests cover:

- bbo row resolver checksum stability.
- missing row evidence.
- linked lifecycle refs with resolved rows.
- source timestamp order invalid while snapshot refs remain ordered.
- existing missing/invalid ref cases.

## Migration, Rollback, Compatibility

No migration. Rollback is `git revert` of the code, test, and SOW changes, then
standard deploy. Existing persisted payloads remain compatible.

## Configuration And Environment Isolation

The resolver uses the existing market-context source resolution and never emits
the URL. Unit tests inject fake sources and disable default env resolution.

## Code Organization And Dependencies

No new third-party dependency. The checksum uses Python standard library
`hashlib` and stable JSON normalization.

## Documentation And Operations Manual

Operators should interpret:

- `local_snapshot_row_sequence_validated_diff_payload_missing`: refs and rows
  are valid, but full diff payload is still not persisted.
- `snapshot_ref_sequence_validated_row_truth_missing`: refs are ordered, but
  row lookup could not prove row existence.
- `invalid_local_orderbook_sequence`: row evidence contradicts expected
  source/client ordering.

## Deployment And Acceptance Criteria

Acceptance:

- Focused lint and tests pass.
- Full lint and unit suite pass before deployment.
- Deploy uses `scripts/deploy.sh`.
- Post-deploy runtime truth has no blocking findings.
- No strategy, risk, execution, provider, schema, collector, symbol, venue,
  release, promotion, tuning, or live order behavior changes.
