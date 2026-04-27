# Historical No-Submit Slippage Reference Gap SOW

## Business objectives and boundaries
- Objective: make the historical `no_submit_command` slippage reference gap machine-readable and auditable without fabricating trading evidence.
- Boundary: read-only truth surface, documentation, and tests only. No strategy, risk, execution, provider, symbol, venue, strategy family, schema, or live order behavior changes.

## Module responsibilities and domain model
- `scripts/runtime_truth_report.py` owns the aggregate live truth report.
- A valid slippage reference must be a persisted pre-trade order or command reference price. Fill price, post-fill mid, or reconstructed market prices are not valid substitutes.

## Input/output interfaces
- Input: aggregate execution fill/order/command coverage facts already returned by the runtime truth DB probe.
- Output: `slippage_proxy.coverage_audit` now includes deterministic backfill status, reason, affected fill count, mutation flag, and reference policy.

## Database schema / tables / indexes / constraints
- No schema changes.
- No database mutations.

## Transactions, Consistency, Concurrency
- No write transaction.
- The summary is deterministic for a given runtime truth payload.

## Authorization, Authentication, Data Security
- No credentials are read or printed.
- The report emits only aggregate counts and non-sensitive reason codes.

## Error Handling and Idempotency
- If missing references are all historical `no_submit_command` fills and current command-path reference evidence exists, report `blocked_no_persisted_pretrade_reference_price`.
- If current submit-command fills still lack references, report `blocked_current_command_path_reference_gap`.
- If no command-path coverage exists, report `blocked_no_command_path_reference_coverage`.

## State Transition and Lifecycle
- No order, fill, lifecycle, recovery, or Redis state is changed.

## Caching and Performance
- No new database query is introduced.
- Only existing aggregate rows are classified.

## Logging, Monitoring, Auditing
- The new fields give PM Loop and operator surfaces an exact no-mutation blocker instead of a vague missing-reference count.

## Testing Strategy
- Unit tests cover the historical no-submit-command gap and live runtime fact projection.

## Migration, Rollback, Compatibility
- No migration.
- Rollback by reverting the commit and redeploying with `scripts/deploy.sh --skip-commit`.
- Existing fields remain backward compatible.

## Configuration and Environment Isolation
- No configuration changes.

## Code Organization and Dependencies
- No new dependencies.
- Modified only `scripts/runtime_truth_report.py`, unit tests, and this SOW.

## Documentation and Operations Manual
- Operators should not backfill missing slippage references from fill price, post-trade mid, or later market data. The only acceptable reference is persisted pre-trade order or command provenance.

## Deployment and Acceptance Criteria
- `coverage_audit` exposes deterministic no-mutation status and reason.
- Current command-reference coverage remains visible separately.
- Focused tests pass.
