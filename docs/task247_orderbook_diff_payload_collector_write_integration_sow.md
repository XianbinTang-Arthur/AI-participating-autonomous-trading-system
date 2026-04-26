# Task247: Orderbook Diff Payload Collector Write Integration SOW

## Business Objectives And Boundaries

Persist execution-science sidecar evidence for sampled OKX `bbo-tbt` and
`books5` orderbook rows so future fill reconstruction can link snapshot rows to
public raw payload, exchange sequence id, collector-local sequence, checksum,
and payload hash.

This task does not change strategy, risk gates, execution, order placement, AI
provider behavior, symbol, venue, strategy family, release, promotion, tuning,
or timeframe plumbing.

## Module Responsibilities And Domain Model

- `aats.data_platform.orderbook_diff_payload_contract` owns the shared checksum
  and payload-hash contract.
- `aats.data_platform.collectors.microstructure_ws_collector` parses public OKX
  orderbook payloads, assigns collector-local sequences, writes existing bronze
  snapshot rows, and writes sidecar rows to `bronze.market_orderbook_payloads`.
- Existing execution-science read surfaces can continue resolving rows by
  `snapshot_table`, `symbol`, `ts`, and `row_checksum`.

## Input / Output Interfaces

Input:
- Public OKX `bbo-tbt` and `books5` messages already consumed by the
  microstructure daemon.
- Existing `ingest_run_id` created by the daemon at startup.

Output:
- Existing `bronze.market_orderbook_bbo` and
  `bronze.market_orderbook_books5` rows remain unchanged.
- New sidecar rows in `bronze.market_orderbook_payloads` when a sampled row has
  collector sequence evidence.

## Database Schema / Tables / Indexes / Constraints

Uses the already deployed Batch B stage `batch_b_12_orderbook_payloads`.
Primary identity remains `(snapshot_table, symbol, ts, row_checksum)`.
Collector sequence uniqueness remains scoped by
`(ingest_run_id, symbol, COALESCE(channel, ''), collector_sequence)`.

No schema change is included in this bounded task.

## Transactions, Consistency, Concurrency

Snapshot row and sidecar row writes run in the same SQLAlchemy session and
transaction. If sidecar validation or insertion fails, the flush fails and the
batch is rolled back by the existing session context.

Collector sequence is monotonic per daemon run, symbol, and channel.

## Authorization, Authentication, Data Security

Only public OKX market payloads are persisted. Contract validation rejects
credential-like keys in raw payloads. No credentials, tokens, API keys,
database passwords, or connection strings are logged or stored.

## Error Handling And Idempotency

The sidecar writer validates each record before SQL execution. Invalid evidence
raises a flush error instead of silently claiming truth. Inserts use
`ON CONFLICT DO NOTHING` on the sidecar primary key.

## State Transition And Lifecycle

This task adds sidecar evidence to the existing microstructure daemon lifecycle.
It does not create a new daemon, scheduler, or trading state transition.

## Caching And Performance

Sidecar writes are batched with the existing bbo/books5 flushes. Additional data
volume is limited to sampled bbo/books5 rows and public payload JSON.

## Logging, Monitoring, Auditing

Existing flush error accounting and ingest run lifecycle continue to capture
write failures. Collector status now exposes the latest sidecar sequence per
channel and symbol.

## Testing Strategy

Unit coverage:
- checksum/hash contract stability;
- parser preservation of raw public payload and exchange sequence id;
- sidecar SQL generation for bbo/books5;
- collector sequence monotonicity.

Integration validation should use post-deploy runtime truth report and a DB
check proving `bronze.market_orderbook_payloads` receives rows after the daemon
restarts.

## Migration, Rollback, Compatibility

No migration in this task. Rollback is `git revert` of the collector write
integration and redeploy. The existing sidecar table can remain unused.

Existing callers that construct `BboRow` or `Books5Row` without sidecar fields
remain compatible because the new fields are optional and sidecar write is
skipped without `collector_sequence`.

## Configuration And Environment Isolation

No new environment variables. The collector uses existing RDP database
configuration loaded by the running daemon.

## Code Organization And Dependencies

No new external dependency. Shared checksum/hash helpers live in
`orderbook_diff_payload_contract` to avoid duplicating the evidence contract
across collector and read surfaces.

## Documentation And Operations Manual

Operators should treat a non-empty `bronze.market_orderbook_payloads` table as
evidence that raw public orderbook payload truth is being captured. Empty rows
after deployment indicate the microstructure daemon has not yet restarted or
the bbo/books5 flush path is failing.

## Deployment And Acceptance Criteria

Acceptance is binary:
- tests pass;
- code is committed, pushed, deployed through `scripts/deploy.sh`;
- runtime truth report has no blocking findings;
- `bronze.market_orderbook_payloads` row count increases after deployment;
- no live order behavior changes are introduced.
