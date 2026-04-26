# Task244: Orderbook Diff Payload Persistence Contract

## Business Objectives And Boundaries

Define the minimum contract required before AATS changes the live
microstructure collector to persist local orderbook diff payload and
collector-sequence evidence.

This task is contract-only. It does not change collector runtime behavior, live
order behavior, risk gates, strategy logic, provider behavior, schema, symbol,
venue, strategy family, release, promotion, or tuning.

## Module Responsibilities And Domain Model

`aats.data_platform.orderbook_diff_payload_contract` owns the machine-readable
contract and fail-fast validation helpers.

Domain terms:

- Snapshot row truth: existing `bronze.market_orderbook_bbo` or
  `bronze.market_orderbook_books5` row exists and has a deterministic checksum.
- Diff payload truth: raw or normalized public orderbook payload is persisted
  with a payload hash and schema version.
- Collector sequence truth: a positive collector-local sequence number with
  explicit scope `per_ingest_run_symbol_channel`.
- Payload sidecar: future bronze table
  `bronze.market_orderbook_payloads`, keyed back to the snapshot table row.

## Input / Output Interfaces

Input is a prospective payload persistence record represented as a mapping.

Required base fields:

- `storage_table`
- `snapshot_table`
- `symbol`
- `ts`
- `source_ts`
- `collector_sequence`
- `collector_sequence_scope`
- `row_checksum`
- `checksum_version`
- `capture_status`
- `ingest_run_id`
- `received_at`

When `capture_status=diff_payload_persisted`, these extra fields are required:

- `payload_hash`
- `payload_schema_version`
- `payload_kind`
- `raw_payload`

Output is `ContractValidation(ok, missing_fields, errors, warnings)`.

## Database Schema / Tables / Indexes / Constraints

No schema migration is included in this task.

The contract selects a future bronze sidecar table:

`bronze.market_orderbook_payloads`

The sidecar references existing snapshot rows by:

`snapshot_table`, `symbol`, `ts`, `row_checksum`

Recommended future indexes:

- `snapshot_table, symbol, ts`
- `snapshot_table, symbol, collector_sequence`
- `snapshot_table, symbol, source_ts`

The contract explicitly rejects `execution.*` as payload storage so execution DB
does not become a duplicated bronze lake.

## Transactions, Consistency, Concurrency

This task opens no transactions and performs no writes.

The future runtime task must treat `collector_sequence` as monotonic within the
explicit scope `per_ingest_run_symbol_channel`, not as wall-clock ordering and
not as a global exchange sequence.

## Authorization, Authentication, Data Security

Allowed evidence:

- table labels
- symbol
- timestamps
- collector sequence
- checksums and payload hashes
- public orderbook payload if approved by schema/storage budget

Forbidden evidence:

- API key
- token
- password
- secret
- passphrase
- authorization header
- cookie
- DB URL / DSN / connection string

The validator recursively rejects credential-like keys inside `raw_payload`.

## Error Handling And Idempotency

Fail-fast errors include:

- unsupported snapshot table
- execution DB payload storage
- unsupported capture status
- invalid sequence scope
- non-positive collector sequence
- invalid checksum/hash
- unsupported checksum or payload schema version
- sensitive payload key

Repeated validation over the same mapping is deterministic.

## State Transition And Lifecycle

Execution-science ladder after this task:

1. snapshot refs exist
2. referenced snapshot rows exist
3. row checksums exist
4. level-1 fill feasibility exists
5. diff payload persistence contract exists
6. later: schema/runtime collector writes persist payload and sequence evidence
7. later: depth-aware fill reconstruction

## Caching And Performance

The contract module is pure Python and side-effect free. It introduces no query,
cache, or runtime path.

## Logging, Monitoring, Auditing

No new logs are emitted. The contract is audited through tests and automation
state.

## Testing Strategy

Unit tests cover:

- contract spec distinguishes snapshot row truth from diff payload truth
- snapshot-only record can be valid without raw payload
- persisted diff payload requires hash, schema, kind, and raw payload
- public OKX orderbook-like payload is accepted
- execution DB payload storage is rejected
- sensitive keys inside payload are rejected
- invalid sequence, checksum, and sequence scope are rejected
- required fields are capture-status specific

## Migration, Rollback, Compatibility

No migration. Rollback is deleting the contract module, tests, and this SOW.

The contract is compatible with the existing read-only row resolver and fill
feasibility truth surface.

## Configuration And Environment Isolation

No env var is read. No credentials are loaded or printed.

## Code Organization And Dependencies

No third-party dependency is introduced.

Files:

- `aats/data_platform/orderbook_diff_payload_contract.py`
- `tests/unit/data_platform/test_orderbook_diff_payload_contract.py`
- `docs/task/task244_orderbook_diff_payload_persistence_contract_sow.md`

## Documentation And Operations Manual

Operators should interpret this task as a prerequisite for later runtime
collector work. It does not mean diff payload is already persisted.

## Deployment And Acceptance Criteria

Accepted when:

- the contract is machine-readable
- tests prove required and forbidden fields
- no runtime collector writes are changed
- no secrets can pass through raw payload keys
- runtime truth smoke remains clean after deployment
