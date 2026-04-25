# Task233: Read-Only Market-Context Orderbook Source

## Business Objectives and Boundaries

AATS is in the AI-enabled trading microscope stage. Task230 can attach
submit/ack/fill orderbook refs only when bronze orderbook rows are visible from
the execution DB session. Runtime evidence shows execution truth is stored in
`aats_live_derivatives`, while active bronze orderbook truth is stored in
`aats_research`. This task closes that DB-boundary gap without changing trading
logic.

In scope:

- Resolve lifecycle orderbook refs from a dedicated read-only market-context DB
  source.
- Prefer `AATS_MARKET_CONTEXT_DB_URL`, then fallback to
  `AATS_ACTIVE_PARAMETER_DB_URL`, then `RDP_DATABASE_URL`.
- Keep execution writes in `aats_live_derivatives`.
- Keep all failures fail-soft.

Out of scope:

- No strategy, symbol, venue, family, timeframe, AI autonomy, risk gate,
  execution gate, kill switch, release, promotion, or tuning change.
- No bronze table duplication into the execution DB.
- No FDW / cross-database SQL view.
- No orderbook reconstruction or execution-science model change.

## Module Responsibilities and Domain Model

- `aats.services.execution_engine.orderbook_snapshot_refs` owns orderbook ref
  source resolution and nearest-snapshot lookup.
- `ConvergedPostgresExecutionRepository` owns execution lifecycle persistence
  and passes the configured read source into lookup calls.
- Bootstrap wires the optional source into the converged execution repository.

The domain artifact remains a lifecycle market-context ref:

- `pre_event_orderbook_snapshot_ref`
- `post_event_orderbook_snapshot_ref`

Refs must be auditable enough to identify the source DB/table/symbol/timestamp.

## Input / Output Interfaces

Input:

- Existing execution event timestamps from `OrderState` and `FillEvent`.
- Existing bronze rows in:
  - `bronze.market_orderbook_books5`
  - `bronze.market_orderbook_bbo`
- Optional upstream refs already present in payloads.

Output:

- Existing execution raw payload shape is preserved.
- `lifecycle_snapshot_refs.<stage>.market_context_snapshot_refs` receives real
  refs when available.
- Missing or failing lookup remains explicit via existing missing refs.

## Database Schema / Tables / Indexes / Constraints

No schema change. The task reads existing bronze tables and writes only existing
execution raw payload fields through existing persistence paths.

## Transactions, Consistency, Concurrency

Execution persistence transaction remains isolated to the execution DB session.
The market-context read source uses a separate read-only session and small pool.
Lookup failure cannot rollback or poison execution persistence.

## Authorization, Authentication, Data Security

The implementation must never log or emit database URLs, passwords, tokens, or
connection strings. It may expose non-secret source labels such as database name,
schema, table, symbol, and timestamp.

## Error Handling and Idempotency

All lookup errors are fail-soft. Existing explicit refs always win. Re-running
the same persistence path should not rewrite explicit refs with different source
refs.

## State Transition and Lifecycle

No order state transition semantics change. The task only enriches lifecycle
truth metadata for submit, ack, and fill stages.

## Caching and Performance

The default read source is lazily cached per process using a small SQLAlchemy
pool. Each event performs at most bounded nearest-row lookups inside a short time
window and stops when both refs are captured.

## Logging, Monitoring, Auditing

No new noisy logging. Refs should identify the non-secret source DB/table path.

## Testing Strategy

- Unit tests for explicit source success and source metadata.
- Unit tests for env fallback order.
- Unit tests for source failure falling back without aborting persistence.
- Existing Task230 tests remain valid.

## Migration, Rollback, Compatibility

No migration. Rollback is reverting the helper/repository/bootstrap/test changes;
payload shape remains backward-compatible.

## Configuration and Environment Isolation

Runtime configuration is isolated to:

- `AATS_MARKET_CONTEXT_DB_URL`
- `AATS_ACTIVE_PARAMETER_DB_URL`
- `RDP_DATABASE_URL`

Only the first available URL is used for the market-context source. The source is
read-only.

## Code Organization and Dependencies

Use existing SQLAlchemy dependencies. Do not introduce a new package or service.

## Documentation and Operations Manual

After deployment, verify from runtime rows that lifecycle refs point at
`aats_research` bronze source when live execution events are persisted.

## Deployment and Acceptance Criteria

Acceptance:

- With a configured market-context source, missing lifecycle refs are resolved
  from bronze rows outside the execution DB.
- Captured refs identify source DB/table/symbol/timestamp.
- Missing source/data remains fail-soft.
- Unit tests pass.
- No forbidden scope/gate/release/promotion drift.
