# Task241: Local Orderbook Diff Payload Capture Design

## Current Behavior

Task240 exposes snapshot-ref ordering truth for execution lifecycle stages. It
parses existing refs such as:

- `bronze.market_orderbook_books5:<symbol>:<iso_ts>`
- `bronze.market_orderbook_bbo:<symbol>:<iso_ts>`
- `<source>.bronze.market_orderbook_books5:<symbol>:<iso_ts>`
- `<source>.bronze.market_orderbook_bbo:<symbol>:<iso_ts>`

That proves only that a pre-event ref timestamp is ordered before a post-event
ref timestamp. It does not prove that the referenced bronze rows still exist,
that the row content was the same as the book seen by the collector, or that a
full diff sequence can be reconstructed around the execution event.

The active bronze tables currently store sampled orderbook snapshots:

- `bronze.market_orderbook_bbo`
  - `symbol`
  - `ts` as client-side sample instant
  - `source_ts` as OKX-pushed timestamp
  - `bid_px`, `bid_sz`, `ask_px`, `ask_sz`
  - generated `mid`, `spread`, `imbalance`
  - `ingest_run_id`, `received_at`
  - primary key `(symbol, ts)`
- `bronze.market_orderbook_books5`
  - `symbol`
  - `ts` as client-side sample instant
  - `source_ts` as OKX-pushed timestamp
  - flattened bid/ask levels 1 through 5
  - `ingest_run_id`, `received_at`
  - primary key `(symbol, ts)`

Neither table currently stores raw book payloads, a normalized row checksum, a
collector-local monotonic sequence number, or an exchange diff sequence id.

## Business Objectives And Boundaries

The objective is to define the smallest safe contract needed to move from
snapshot-ref ordering truth to verifiable local orderbook sequence truth.

In scope:

- Define the row fields and read-only resolver output required to prove that
  referenced bronze orderbook rows exist.
- Define a deterministic checksum over existing flattened bbo/books5 fields so
  operator truth can identify the exact row content without exposing secrets.
- Define the later schema fields needed if AATS decides to persist full local
  diff payload or collector sequence truth.
- Keep the next implementation read-only unless a later task explicitly performs
  a schema migration.

Out of scope:

- No strategy, risk, execution gate, kill switch, provider, symbol, venue,
  strategy family, timeframe, release, promotion, or tuning change.
- No live order behavior change.
- No orderbook reconstruction model.
- No exchange private data, credentials, connection strings, API keys, or
  database URLs in operator output.
- No automatic promotion from execution-science evidence to trading decisions.

## Module Responsibilities And Domain Model

`OperatorQueryService` remains the owner of the read-only truth surface.

`aats.services.execution_engine.orderbook_snapshot_refs` remains the owner of
creating refs during lifecycle persistence. The next read-only resolver may live
there or in a small sibling module if sharing with operator code avoids
duplication.

Domain terms:

- Snapshot ref: the current persisted pointer to a bronze orderbook row.
- Snapshot-ref sequence truth: pre-ref timestamp <= post-ref timestamp.
- Row-existence truth: the referenced bronze row can be resolved from the
  configured market-context read source.
- Row-content truth: the referenced row has a deterministic normalized checksum.
- Local sequence truth: row-existence truth plus monotonic source/client ordering
  around the execution lifecycle stage.
- Full diff payload truth: persisted raw or normalized diff payload evidence
  sufficient to replay or audit the book transition. This is not currently
  available.

## Input / Output Interfaces

Input:

- Existing lifecycle refs in execution payload JSON.
- Existing bronze rows in `bronze.market_orderbook_books5` and
  `bronze.market_orderbook_bbo`.
- Existing read-only market-context source selection:
  `AATS_MARKET_CONTEXT_DB_URL`, `AATS_ACTIVE_PARAMETER_DB_URL`,
  `RDP_DATABASE_URL`.

Read-only resolver input:

```text
resolve_orderbook_ref_truth(ref: str, expected_symbol: str | None) -> dict
```

Required resolver output for each ref:

```json
{
  "raw_ref": "...",
  "parse_status": "parsed",
  "source_name": "optional_non_secret_source",
  "table_name": "bronze.market_orderbook_books5",
  "symbol": "BTC-USDT-SWAP",
  "ts": "2026-04-25T00:00:00.000000Z",
  "row_exists": true,
  "source_ts": "2026-04-25T00:00:00.000000Z",
  "received_at": "2026-04-25T00:00:00.000000Z",
  "ingest_run_id": "uuid-string",
  "content_checksum": "sha256:<hex>",
  "checksum_source": "computed_from_flattened_row",
  "sequence_key": {
    "table_name": "bronze.market_orderbook_books5",
    "symbol": "BTC-USDT-SWAP",
    "source_ts": "2026-04-25T00:00:00.000000Z",
    "ts": "2026-04-25T00:00:00.000000Z",
    "content_checksum": "sha256:<hex>"
  },
  "missing_evidence": []
}
```

Operator sequence output extension:

```json
{
  "status": "local_snapshot_row_sequence_validated_diff_payload_missing",
  "pre": {"row_exists": true, "content_checksum": "sha256:<hex>"},
  "post": {"row_exists": true, "content_checksum": "sha256:<hex>"},
  "ordered_by": {
    "snapshot_ref_ts": true,
    "source_ts": true,
    "client_ts": true
  },
  "delta_ms": 500,
  "source_delta_ms": 500,
  "client_delta_ms": 500,
  "missing_evidence": [
    "local_orderbook_diff_payload_not_persisted",
    "local_orderbook_diff_sequence_id_not_persisted"
  ]
}
```

The status must remain incomplete until full diff payload or explicit collector
sequence evidence exists.

## Database Schema / Tables / Indexes / Constraints

No schema change is required for the next read-only implementation. It can query
existing columns by `(symbol, ts)` from:

- `bronze.market_orderbook_bbo`
- `bronze.market_orderbook_books5`

Minimal read projections:

`bronze.market_orderbook_bbo`:

- `symbol`, `ts`, `source_ts`
- `bid_px`, `bid_sz`, `ask_px`, `ask_sz`
- `ingest_run_id`, `received_at`

`bronze.market_orderbook_books5`:

- `symbol`, `ts`, `source_ts`
- `bid_px_1` through `bid_px_5`
- `bid_sz_1` through `bid_sz_5`
- `ask_px_1` through `ask_px_5`
- `ask_sz_1` through `ask_sz_5`
- `ingest_run_id`, `received_at`

Later schema migration candidates, if full diff truth is approved:

- `collector_sequence BIGINT NOT NULL`
- `row_checksum TEXT NOT NULL`
- `checksum_version TEXT NOT NULL DEFAULT 'orderbook_row_v1'`
- `payload_hash TEXT NULL`
- `raw_payload JSONB NULL` only if storage and retention budget are approved
- optional index `(symbol, source_ts)`
- optional index `(symbol, collector_sequence)`
- optional unique index `(symbol, ts, row_checksum)`

Do not add these fields in the read-only resolver task.

## Transactions, Consistency, Concurrency

The resolver must use a read-only market-context session. Lookup failure must not
rollback or block operator reads. If the referenced row is missing due to
retention, migration, or DB-boundary issues, the truth surface must mark it as
`row_missing` instead of fabricating sequence truth.

Resolver queries must be deterministic:

- Parse the ref.
- Query exactly the referenced table, symbol, and client `ts`.
- If one row exists, compute checksum over normalized selected fields.
- If no row exists, return missing evidence.

No write transaction is opened in the read-only task.

## Authorization, Authentication, Data Security

The resolver may expose:

- source label
- schema/table name
- symbol
- timestamps
- UUID ingest run id
- deterministic non-secret checksum

The resolver must never expose:

- DB URL
- username/password
- API key
- token
- raw private exchange payload
- complete connection string

## Error Handling And Idempotency

Expected fail-soft statuses:

- `missing_ref`
- `unparseable_ref`
- `unsupported_table`
- `symbol_mismatch`
- `row_missing`
- `source_unavailable`
- `checksum_unavailable`
- `client_ts_order_invalid`
- `source_ts_order_invalid`

Repeated reads over unchanged bronze rows must return the same checksum and
sequence result.

Checksum normalization:

- Use stable JSON with sorted keys and compact separators.
- Convert decimals to canonical strings.
- Convert timestamps to UTC ISO strings with microseconds and `Z`.
- Include table name, symbol, client `ts`, source `source_ts`, and all book
  fields that define the persisted row content.

## State Transition And Lifecycle

No order state changes. Submit, ack, fill, cancel, reject, and timeout lifecycle
stages are only inspected if they already expose refs.

The lifecycle truth ladder becomes:

1. lifecycle refs absent
2. snapshot refs present
3. snapshot-ref timestamps ordered
4. referenced bronze rows exist
5. row checksums computed
6. source/client ordering validated
7. full diff payload or collector sequence persisted

Current Task241 design covers steps 4 through 6 and leaves step 7 as missing
evidence until a later schema/runtime task.

## Caching And Performance

The resolver should query at most two rows per lifecycle stage: pre and post.
Per decision, cap stage evidence exactly as the existing truth chain caps
sequence evidence. No broad window scan is needed for row-existence truth because
the ref already points at a specific `(table, symbol, ts)`.

The checksum is computed in process from one row projection. No table-wide scan
or orderbook reconstruction is allowed in the operator read path.

## Logging, Monitoring, Auditing

No new noisy logs are required for the read-only task. Operator API output is the
audit surface. If logging is added, it must log only status codes and non-secret
source/table names.

Audit fields to expose:

- `row_exists`
- `content_checksum`
- `checksum_source`
- `source_ts`
- `received_at`
- `ingest_run_id`
- `ordered_by.snapshot_ref_ts`
- `ordered_by.source_ts`
- `ordered_by.client_ts`
- `missing_evidence`

## Testing Strategy

Focused unit tests for the next read-only implementation must cover:

- books5 ref resolves to one row and computes a stable checksum.
- bbo ref resolves to one row and computes a stable checksum.
- missing row returns `row_missing` and does not mark sequence complete.
- pre/post refs ordered by ref ts but invalid by `source_ts` remain incomplete.
- pre/post refs with symbol mismatch return `symbol_mismatch`.
- unsupported table remains fail-soft.
- market-context source failure returns `source_unavailable`.
- checksum is stable across repeated calls and independent of dict ordering.

No integration test is required for this design-only task. A later resolver task
should add the narrowest unit tests plus a runtime smoke that confirms at least
one live decision lifecycle ref can be resolved or explicitly reports missing
bronze rows.

## Migration, Rollback, Compatibility

This design task has no migration.

Rollback for the next read-only implementation:

- Revert resolver/read-surface code.
- Existing payloads and refs remain backward-compatible.
- Operator output returns to Task240 snapshot-ref sequence behavior.

Rollback for any later schema task:

- Add an explicit reversible migration.
- Keep the read-only resolver tolerant of rows with missing new columns during
  rolling deploys.

## Configuration And Environment Isolation

Use the existing read source resolution order:

1. `AATS_MARKET_CONTEXT_DB_URL`
2. `AATS_ACTIVE_PARAMETER_DB_URL`
3. `RDP_DATABASE_URL`

Do not print these values. The resolver should expose only a non-secret
`source_name` derived by existing source-name logic.

## Code Organization And Dependencies

Preferred implementation shape:

- Add a small row resolver near `orderbook_snapshot_refs.py` if it must share DB
  source construction.
- Add operator payload composition in `OperatorQueryService`.
- Keep checksum normalization in one helper with direct unit tests.
- Do not introduce new third-party dependencies.
- Do not expand public API shapes except the read-only operator truth payload.

## Documentation And Operations Manual

Operator interpretation after the next implementation:

- `snapshot_ref_sequence_validated_diff_missing`: refs are parseable and
  ordered, but row existence/checksum has not been validated.
- `local_snapshot_row_sequence_validated_diff_payload_missing`: pre/post refs
  exist, rows exist, checksums are computed, and source/client ordering is valid,
  but full diff payload or collector sequence is not persisted.
- `invalid_local_orderbook_sequence`: at least one row exists but source/client
  ordering contradicts the lifecycle ordering.
- `row_missing`: a ref points at a row that cannot be resolved from the
  configured market-context source.

## Deployment And Acceptance Criteria

This design task is accepted when:

- The exact current tables and fields are documented.
- The next resolver output contract is explicit.
- Snapshot-ref ordering is clearly distinguished from row-content and full diff
  sequence truth.
- The later schema candidates are documented but not implemented.
- No strategy, risk, execution behavior, provider, schema, symbol, venue,
  release, promotion, tuning, or live order behavior changes are made.

The next implementation task is accepted only when:

- For each lifecycle stage with complete refs, the operator truth surface can
  prove both referenced rows exist or explicitly mark why they cannot be
  resolved.
- Deterministic checksums are available for books5 and bbo refs.
- Source/client timestamp ordering is validated independently from ref ordering.
- Full diff payload absence remains explicit missing evidence.
- Focused unit tests pass.
