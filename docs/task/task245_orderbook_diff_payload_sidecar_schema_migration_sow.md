# Task245: Orderbook Diff Payload Sidecar Schema Migration

## Business Objectives And Boundaries

Add the reversible bronze schema infrastructure required to persist local
orderbook diff payload and collector-sequence truth in a later task.

This task is schema-only. It does not connect collector writes and does not
change strategy logic, risk gates, execution behavior, provider behavior,
symbols, venues, strategy families, release, promotion, or tuning.

## Module Responsibilities And Domain Model

`bronze.market_orderbook_payloads` is the sidecar selected by
`aats.data_platform.orderbook_diff_payload_contract`.

It stores:

- snapshot row identity: `snapshot_table`, `symbol`, `ts`, `row_checksum`
- collector sequence truth: `collector_sequence`, `collector_sequence_scope`
- diff payload truth when available: `payload_hash`,
  `payload_schema_version`, `payload_kind`, `raw_payload`
- missing payload evidence when diff payload is unavailable

Existing `bronze.market_orderbook_bbo` and
`bronze.market_orderbook_books5` remain the snapshot-row source of truth.

## Input / Output Interfaces

Input is a future collector-side write record matching the contract from
Task244. Output is one row in `bronze.market_orderbook_payloads`.

This task only creates the table, constraints, indexes, ORM model, and tests.
No runtime writer consumes the interface yet.

## Database Schema / Tables / Indexes / Constraints

New table:

- `bronze.market_orderbook_payloads`

Primary key:

- `snapshot_table`, `symbol`, `ts`, `row_checksum`

Important constraints:

- storage table must be `bronze.market_orderbook_payloads`
- snapshot table must be one of the two supported bronze orderbook tables
- collector sequence must be positive
- sequence scope must be `per_ingest_run_symbol_channel`
- checksum and payload hashes must be `sha256:<64 lowercase hex chars>`
- `diff_payload_persisted` requires payload hash, schema version, kind, and raw
  payload

Indexes:

- `snapshot_table, symbol, ts`
- `snapshot_table, symbol, collector_sequence`
- `snapshot_table, symbol, source_ts`
- unique expression index for `ingest_run_id, symbol, COALESCE(channel, ''),
  collector_sequence`

## Transactions, Consistency, Concurrency

The migration runs in one transaction. The unique sequence-scope index prevents
duplicate collector-local sequence evidence within one ingest run, symbol, and
channel.

No existing snapshot rows are mutated.

## Authorization, Authentication, Data Security

No credentials are read. The table stores public market microstructure payloads
only. Credential-like key rejection remains enforced by the contract validator
before a future writer is connected.

## Error Handling And Idempotency

Forward migration uses `CREATE TABLE IF NOT EXISTS` and `CREATE INDEX IF NOT
EXISTS`. Rollback drops only the new sidecar table.

## State Transition And Lifecycle

Execution-science ladder after this task:

1. snapshot refs exist
2. referenced snapshot rows exist
3. row checksums exist
4. level-1 fill feasibility exists
5. diff payload persistence contract exists
6. sidecar schema exists
7. later: collector writes payload and sequence evidence
8. later: depth-aware fill reconstruction

## Caching And Performance

No cache changes. Indexes are scoped to expected read paths and sequence
validation paths.

## Logging, Monitoring, Auditing

No new runtime logs. Audit is through migration files, ORM metadata, tests, git
history, deployment, and runtime truth smoke.

## Testing Strategy

Unit tests cover:

- ORM roundtrip for snapshot-only sidecar rows
- DB rejection of diff rows missing payload fields
- ORM roundtrip for persisted diff rows
- rejection of invalid snapshot table, sequence, and checksum
- Batch B registration order
- forward and rollback SQL files
- rollback drops only the sidecar table

## Migration, Rollback, Compatibility

Forward migration:

- `aats/data_platform/migrations/batch_b_12_orderbook_payloads.sql`

Rollback migration:

- `aats/data_platform/migrations/batch_b_12_orderbook_payloads_rollback.sql`

Rollback command path is the existing Batch B rollback runner or `git revert`
of this task followed by the standard deployment flow. Rollback does not touch
existing bbo/books5 rows.

## Configuration And Environment Isolation

No configuration or environment variable is added.

## Code Organization And Dependencies

Files:

- `aats/data_platform/migrations/_batch_b.py`
- `aats/data_platform/migrations/batch_b_12_orderbook_payloads.sql`
- `aats/data_platform/migrations/batch_b_12_orderbook_payloads_rollback.sql`
- `aats/data_platform/rdp_models.py`
- `tests/unit/data_platform/test_orderbook_diff_payload_schema.py`
- `docs/task/task245_orderbook_diff_payload_sidecar_schema_migration_sow.md`

No new dependency is introduced.

## Documentation And Operations Manual

Operators should interpret this as schema readiness only. Full diff payload
truth is still missing until a later collector writer persists real payload rows
and live samples are observed.

## Deployment And Acceptance Criteria

Accepted when:

- the sidecar schema implements the approved contract fields and constraints
- rollback removes only the new sidecar table and indexes
- no collector write path is connected
- tests pass
- deployment uses `scripts/deploy.sh`
- post-deploy runtime truth reports no active blocker
