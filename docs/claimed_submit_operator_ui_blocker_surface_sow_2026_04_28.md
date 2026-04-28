# Claimed Submit Operator UI Blocker Surface SOW

## Business Objectives and Boundaries
Make the post-rebaseline blocker explicit in operator recovery surfaces. When a baseline is accepted but a CLAIMED submit order remains unresolved, `/system/recovery`, rebaseline/resume action messages, and the overview focus panel should show the exact claimed-submit gate instead of a generic recovery limitation. This is read-only and must not mutate order state, exchange state, Redis, Postgres, strategy configuration, risk gates, provider settings, symbols, venues, or strategy families.

## Module Responsibilities and Domain Model
`RecoveryQueryFacade` owns the recovery view payload. It will add a `claimed_submit_recovery_gate` derived from current order truth and execution command truth. `ReconciliationSystemQueryFacade` owns operator action messages and will include this gate in blocked rebaseline/resume copy. The overview UI owns operator-readable display.

## Input/Output Interfaces
Input is existing query-service order, fill, and execution command repositories. Output is an additive `recovery.claimed_submit_recovery_gate` object plus Chinese UI/action text explaining the exact required confirmation string.

## Database Schema / Tables / Indexes / Constraints
No schema, table, index, or constraint changes. Existing read paths are used only for current order, local fills, and submit command lookup.

## Transactions, Consistency, Concurrency
No transactions. The gate is a point-in-time read model. It does not authorize recovery; the protected writer path still performs its own exchange confirmation and exact operator-confirmation checks.

## Authorization, Authentication, Data Security
No credential or secret is read. The endpoint remains under existing operator authentication. The surfaced identifiers are operational order/command ids already visible in operator execution truth.

## Error Handling and Idempotency
If order hydration, fill lookup, or command lookup is unavailable, the gate reports a non-active or read-error status instead of throwing from the recovery view. Re-running the query is idempotent.

## State Transition and Lifecycle
No lifecycle transition is introduced. Rebaseline can still complete while the claimed-submit gate remains active. Recovery remains blocked until the operator verifies OKX absence and uses the exact confirmation through the protected resolve path.

## Caching and Performance
The gate is built inside the existing cached recovery view and uses one latest-order lookup plus scoped fill/command lookups. No new cache is required.

## Logging, Monitoring, Auditing
No new logs are required. The recovery payload itself becomes the audit surface for operator UI and automation smoke checks.

## Testing Strategy
Unit tests cover recovery payload gate detection, rebaseline/resume blocked messages, frontend static markers, and localization terms.

## Migration, Rollback, Compatibility
No migration. The payload change is additive. Rollback is reverting this commit and redeploying through `scripts/deploy.sh`.

## Configuration and Environment Isolation
No new configuration. Behavior follows existing runtime scope and control-plane settings.

## Code Organization and Dependencies
Backend changes stay in `aats/services/operator/recovery_queries.py` and `aats/services/operator/reconciliation_system_queries.py`. Frontend display stays in existing static modules.

## Documentation and Operations Manual
Operators should interpret the gate as: do not repeat rebaseline as the first action; verify in OKX that the client order has no open order, exchange order, fill, trade, or bill; then use the exact confirmation string in the protected resolve flow.

## Deployment and Acceptance Criteria
Acceptance requires focused backend/frontend tests, `ruff check aats/ --fix`, full unit tests, deployment via `scripts/deploy.sh --profile derivatives-live --skip-commit`, and post-deploy runtime truth showing clean deployment while claimed-submit gate remains awaiting external confirmation.
