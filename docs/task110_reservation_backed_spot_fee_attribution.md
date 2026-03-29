# Task 110 - Reservation-Backed Spot Fee Attribution

## Business objectives and boundaries
- Complete fee expense / rebate income attribution for reservation-backed spot fills.
- Preserve the existing cash result of reservation hold, fill settlement, and release.
- Avoid double-counting `cash_available`.
- Keep the change scoped to the phase1 ledger mirror path for quote-backed spot buys whose fee is covered by reservation settlement.

## Module responsibilities and domain model
- `aats/services/ledger/posting.py`
  - Mirrors reservation lifecycle into ledger journals.
  - Owns reservation hold, fill settlement, reservation release, and any non-cash reclassification tied to those journals.
- `OrderObligation`
  - Carries reserve currency and consumed/released amounts.
- `FillEvent`
  - Provides fee currency and signed fee amount.

## Input/output interfaces
- Input:
  - reservation-backed spot buy fill
  - reserve currency equals quote currency
  - fee currency equals quote currency
  - fee may be positive expense or negative rebate
- Output:
  - cash movement stays unchanged
  - external clearing is normalized to pure trade notional
  - fee expense / rebate income appears in dedicated ledger accounts

## Database schema / tables / indexes / constraints
- Reuse existing ledger journal and ledger entry tables.
- No schema migration is required.
- New journal type labels must fit the existing `journal_type` string column length.

## Transactions, Consistency, Concurrency
- Post attribution inside the same SQLAlchemy session and transaction as reservation settlement.
- Reuse deterministic source ids so replay is idempotent.
- Keep ordering stable:
  - reservation hold
  - fill settlement
  - fee attribution
  - reservation release

## Authorization, Authentication, Data Security
- No auth or secret handling change.

## Error Handling and Idempotency
- Attribution journal must be skipped when fee is zero or not reservation-covered.
- Replaying the same fill must not create duplicate attribution journals.

## State Transition and Lifecycle
- Reservation-backed cash flow remains unchanged:
  - `reservation_hold`
  - `fill_settlement`
  - `reservation_release`
- New attribution journal is purely classificatory and does not alter settlement state transitions.

## Caching and Performance
- No new caches.
- O(1) additional work per eligible fill.

## Logging, Monitoring, Auditing
- Reuse fill metadata and include fee classification details in journal metadata.
- Audit outcome should make it obvious whether the journal classified fee expense or rebate income.

## Testing Strategy
- Update PostgreSQL-backed unit coverage for the existing reservation-backed spot buy fee-expense path.
- Add PostgreSQL integration coverage for a reservation-backed spot buy rebate path.
- Keep settlement-posting rebate tests green.

## Migration, Rollback, Compatibility
- No migration required.
- Public APIs remain unchanged.
- Existing cash balances must stay identical before and after the change.

## Configuration and Environment Isolation
- Use repository-local `.venv`.
- PostgreSQL-backed tests require `AATS_DATABASE_URL`.

## Code Organization and Dependencies
- Keep the change in `aats/services/ledger/posting.py` plus targeted tests.
- Reuse existing accounting helpers for signed quote fee interpretation where needed.

## Documentation and Operations Manual
- Document that reservation-backed spot quote fees are classified via non-cash reclassification against `external_clearing`.

## Deployment and Acceptance Criteria
- Acceptance criteria:
  - reservation-backed spot buy with quote fee keeps the same available cash result as before
  - external clearing balance reflects pure trade notional
  - fee expense / rebate income is visible in dedicated ledger accounts
  - targeted lint, unit tests, and the narrowest integration test pass
