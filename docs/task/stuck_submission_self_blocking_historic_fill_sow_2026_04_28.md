# Stuck Submission Self-Blocking Historic Fill SOW (2026-04-28)

## Business objectives and boundaries

Objective: allow the protected stuck-submission recovery precheck to recognize the current self-blocking reconciliation shape: one local pre-submit OKX order absent from exchange open orders, plus non-blocking historical fills outside the OKX lookback window.

Boundary: no live order state, exchange state, strategy, risk, provider, symbol, venue, schema, or deployment behavior is changed by this code patch. Actual recovery still requires the existing admin operator action and, for a CLAIMED submit command, the explicit confirmation string.

## Module responsibilities and domain model

- `OperatorQueryService._latest_reconciliation_allows_stuck_submission_resolution` owns the final safety predicate before resolving a stuck pre-submit order.
- Reconciliation findings remain the source of truth for whether fill differences are blocking or informational.
- Historical `historic_orphan_fill` findings with `reason_code=local_fill_older_than_exchange_lookback_window` are informational lookback artifacts, not live exchange fills.

## Input/output interfaces

Input remains the latest `ReconciliationReport` and `client_order_id`.

Output remains a boolean allowing or rejecting stuck-submission resolution.

## Database schema / tables / indexes / constraints

No schema, index, table, migration, or data update.

## Transactions, Consistency, Concurrency

No transaction behavior changes. The predicate is read-only and runs before any operator recovery mutation.

## Authorization, Authentication, Data Security

No authorization behavior changes. The protected endpoint remains admin-only. No credentials are read or logged.

## Error Handling and Idempotency

Unexpected exchange fills, balance differences, position differences, unrelated reconciliation categories, or blocking findings still fail closed.

## State Transition and Lifecycle

No state transition is introduced. If the predicate passes, the existing `resolve_stuck_submission` flow still performs the audited `CREATED/SUBMITTING -> FAILED` transition through the existing writer.

## Caching and Performance

The change inspects the already-loaded report payload. No new database query is added.

## Logging, Monitoring, Auditing

No new logging. Existing operator action audit remains the recovery audit path.

## Testing Strategy

Add focused unit tests for:

- self-blocking order unknown-on-exchange findings with real report reason codes
- non-blocking historical fill lookback findings coexisting with the self-blocking order
- existing unexpected exchange fill rejection behavior

## Migration, Rollback, Compatibility

Rollback is a code revert. Public API and response shapes are unchanged.

## Configuration and Environment Isolation

No configuration changes.

## Code Organization and Dependencies

Changes stay in `aats/services/operator/query_service.py` and focused unit tests.

## Documentation and Operations Manual

Operators should still verify on OKX that the CLAIMED no-venue order has no open order or fill before confirming stuck-submission recovery.

## Deployment and Acceptance Criteria

Deployment is not part of this patch. Acceptance is focused tests plus standard lint/unit validation passing.
