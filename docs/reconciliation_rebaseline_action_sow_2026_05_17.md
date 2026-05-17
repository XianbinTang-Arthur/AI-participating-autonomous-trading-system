# Reconciliation Rebaseline Action SOW - 2026-05-17

## Business objectives and boundaries
Expose the safe operator recovery action when reconciliation has halted trading and the current runtime policy allows operator rebaseline. Do not add a forced resume path for reconciliation hard mismatches.

## Module responsibilities and domain model
`RecoveryQueryFacade` builds the dashboard recovery summary. `BlockerControlService` converts recovery state into operator actions. `ReconciliationStateSnapshot` remains the persisted summary of the latest reconciliation recovery posture.

## Input/output interfaces
Input is the latest scoped reconciliation state snapshot plus runtime recovery policy. Output is the existing `recovery_view_dashboard()` payload, especially `rebaseline_available`.

## Database schema / tables / indexes / constraints
No schema changes. The fix derives dashboard summary fields from existing snapshot columns and runtime policy.

## Transactions, consistency, concurrency
Read-only dashboard summary logic remains cache-backed and does not write state. No new transaction boundary is introduced.

## Authorization, authentication, data security
No API authorization change. No secrets or credential files are read or printed.

## Error handling and idempotency
If recovery policy is unavailable in a test or nonstandard runtime, the summary preserves the existing recovery-status fallback.

## State transition and lifecycle
`resume_blocked` and `review_required` reconciliation states can surface `rebaseline_available` when operator rebaseline is supported. `resume_eligible` remains false for hard reconciliation halt until follow-up validation succeeds.

## Caching and performance
No additional database queries. The dashboard summary continues to use the existing latest state snapshot fetch.

## Logging, monitoring, auditing
No new logs. Existing operator action endpoints continue to audit actual rebaseline execution.

## Testing strategy
Add unit coverage for dashboard recovery summary derivation and blocker-control action exposure. Run lint, focused unit tests, full unit tests, and the narrowest related WSL2 integration smoke.

## Migration, rollback, compatibility
Backward compatible payload field correction. Rollback is a single code revert with no data migration.

## Configuration and environment isolation
Uses the runtime `recovery_policy.operator_rebaseline_supported` flag. No environment variable changes.

## Code organization and dependencies
No new dependencies. Keep the change inside operator recovery summary and existing tests.

## Documentation and operations manual
This SOW documents the intended behavior: show safe rebaseline when available, not forced resume.

## Deployment and acceptance criteria
After commit, deploy through `scripts/deploy.sh --skip-commit` for the live derivatives profile and verify app containers plus gateway health.
