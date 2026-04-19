# Task 109 - Settlement Posting Rebate Semantics

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## Business objectives and boundaries
- Ensure fill settlement journals preserve signed fee economics.
- Post positive fees as expenses and negative fees as rebate income.
- Keep the change scoped to settlement posting and the directly coupled reservation consumption path.
- Preserve existing journal ids, source ids, and public service interfaces.

## Module responsibilities and domain model
- `aats/services/ledger/settlement_posting.py`
  - Materializes ledger journals for fill settlement side effects.
  - Owns cash movement for realized pnl and fill fees.
- `aats/services/execution_engine/obligations.py`
  - Tracks reserved capital consumption for orders and fills.
  - Must treat quote-fee rebate as a reduction in quote consumption, not an increase.
- `FillSettlementProjection`
  - `realized_pnl_delta` is gross trading pnl in quote currency.
  - `fee_delta` is the signed fee delta carried by upstream portfolio projection.

## Input/output interfaces
- Input:
  - `FillEvent.fill_fee` fields may be positive cost or negative rebate.
  - Spot reservations may exist for quote-funded buys.
- Output:
  - Ledger journals reflect fee expense or fee rebate income with correct sign.
  - Reservation consumption for quote-funded spot fills respects signed quote fee delta.

## Database schema / tables / indexes / constraints
- Reuses existing ledger tables and reservation tables.
- No schema, index, or constraint changes are required.
- New ledger account type labels are allowed through the existing ledger account repository contract.

## Transactions, Consistency, Concurrency
- Continue posting inside the existing SQLAlchemy session/transaction boundary.
- Preserve current idempotency check by `source_type + source_id`.
- Do not introduce cross-transaction reads or writes.

## Authorization, Authentication, Data Security
- No auth surface change.
- No secret handling or permission model changes.

## Error Handling and Idempotency
- Preserve current stable journal ids and duplicate-source guards.
- Rebate posting must be idempotent under replayed fill events.

## State Transition and Lifecycle
- Opening balance behavior remains unchanged.
- Fill settlement lifecycle remains:
  - asset/cash movement
  - realized pnl posting
  - fee expense or rebate posting

## Caching and Performance
- No new caches.
- Keep the change O(1) per fill.

## Logging, Monitoring, Auditing
- Reuse existing ledger journal metadata from `_fill_metadata`.
- Auditable outcome should distinguish fee expense and fee rebate journal types.

## Testing Strategy
- Add PostgreSQL-backed regression coverage for:
  - derivatives fill with negative fee rebate increasing available balance
  - quote-backed spot reservation consumption reduced by rebate
- Reuse existing temporary PostgreSQL runtime helpers.

## Migration, Rollback, Compatibility
- No migration required.
- Backward compatible with existing public APIs.
- Existing positive-fee behavior must remain unchanged.

## Configuration and Environment Isolation
- Use repository-local `.venv` Python runtime.
- PostgreSQL-backed tests depend on `AATS_DATABASE_URL`.

## Code Organization and Dependencies
- Keep the change within existing ledger and obligation services.
- Do not introduce new external dependencies.

## Documentation and Operations Manual
- Record the signed fee semantics here for future ledger audits.

## Deployment and Acceptance Criteria
- Acceptance criteria:
  - negative fee rebate creates ledger income and increases available cash
  - positive fee expense behavior is unchanged
  - quote reservation consumption decreases when a fill receives a quote-currency rebate
  - targeted lint and tests pass
