# Task 111 - Reservation Spot Sell And Fee Currency Defaults

## Business objectives and boundaries
- Audit and correct reservation-backed spot sell fee handling.
- Unify fee attribution decisions on top of `resolved_fee_currency(...)`.
- Preserve existing cash results unless the current behavior is provably wrong.
- Keep the change scoped to reservation consumption, phase1 ledger fee attribution, and settlement posting fee currency resolution.

## Module responsibilities and domain model
- `aats/services/accounting.py`
  - Defines canonical fee-currency resolution and fee-delta conversions.
- `aats/services/execution_engine/obligations.py`
  - Computes reserved-capital consumption for fills.
- `aats/services/ledger/posting.py`
  - Mirrors reservation-backed fills into ledger journals.
- `aats/services/ledger/settlement_posting.py`
  - Posts full settlement-side fee journals outside the phase1 reservation mirror path.

## Input/output interfaces
- Input:
  - reservation-backed spot buy/sell fills
  - fills with explicit or missing `fee_currency`
  - venue-specific fee-currency defaults, especially OKX
- Output:
  - reservation consumption respects reserve-currency fee when fee is reservation-covered
  - phase1 ledger attribution only runs when fee currency resolves to the reserve currency
  - settlement posting uses the same resolved default currency as accounting and portfolio layers

## Database schema / tables / indexes / constraints
- Reuse existing execution, reservation, settlement, ledger journal, and ledger entry tables.
- No schema migration is required.

## Transactions, Consistency, Concurrency
- Keep all new attribution journals inside the existing transaction/session boundaries.
- Preserve deterministic source ids so replay remains idempotent.

## Authorization, Authentication, Data Security
- No changes.

## Error Handling and Idempotency
- Skip attribution when fee amount is zero or fee currency does not resolve to the reserve currency.
- Preserve existing duplicate-journal guards.

## State Transition and Lifecycle
- Reservation lifecycle remains:
  - hold
  - consume
  - release
- Fee attribution remains classification-only and must not double-post cash.

## Caching and Performance
- No cache changes.
- O(1) per fill.

## Logging, Monitoring, Auditing
- Reuse existing fill metadata and include the resolved fee currency when helpful.

## Testing Strategy
- Add coverage for:
  - reservation-backed spot sell with explicit base-currency fee
  - reservation-backed spot sell with missing fee currency on OKX defaulting to quote and therefore skipping reservation-covered attribution
  - settlement posting with missing fee currency on OKX spot buy defaulting to base

## Migration, Rollback, Compatibility
- No migration required.
- Preserve public APIs.
- Existing reservation-backed quote-fee behavior should stay green.

## Configuration and Environment Isolation
- Use repository-local `.venv`.
- PostgreSQL-backed tests require `AATS_DATABASE_URL`.

## Code Organization and Dependencies
- Limit the patch to the existing accounting / obligations / ledger services plus targeted tests.

## Documentation and Operations Manual
- Document that reservation-covered fee attribution is determined by resolved fee currency, not just the raw payload field.

## Deployment and Acceptance Criteria
- Acceptance criteria:
  - reservation-backed spot sell base fee consumes reserved base and is classified to `fee_expense`
  - missing `fee_currency` on OKX buy resolves to base and is not misclassified as quote
  - missing `fee_currency` on OKX sell resolves to quote and does not trigger base-reservation fee attribution
  - targeted lint, unit tests, and the narrowest integration test pass
