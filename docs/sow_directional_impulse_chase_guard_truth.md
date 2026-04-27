# Directional Impulse-Chase Guard Truth Surface SOW

## Business Objectives And Boundaries
Expose read-only runtime evidence that the directional impulse-chase guard is present, deployed, and either has or has not blocked live directional entry attempts. Scope remains OKX + BTC-USDT-SWAP, live carrier `directional`, shadow benchmark `none_verified`.

## Module Responsibilities And Domain Model
`scripts/runtime_truth_report.py` owns aggregate runtime truth only. The trading behavior remains in `TargetPositionEngine`; this SOW does not modify strategy, risk, execution, provider, symbol, venue, strategy family, release, promotion, tuning, or timeframe plumbing.

## Input/Output Interfaces
Input: existing container-loaded database probe, git/deployment truth, and local source marker inspection. Output: `directional_impulse_chase_guard_truth` plus projected `runtime.live_runtime_facts` keys for status, code presence, deployed-head match, hit counts, blocked-entry counts, and latest hit metadata.

## Database Schema / Tables / Indexes / Constraints
No schema, index, or constraint changes. Read-only aggregate queries use `portfolio_allocation_decisions` payload text to count directional decisions carrying impulse-chase guard flags.

## Transactions, Consistency, Concurrency
No writes and no transactions beyond SQLAlchemy's read-only connection scope. Counts are point-in-time diagnostics and are not used to gate live execution.

## Authorization, Authentication, Data Security
Database access stays inside the existing gateway container environment. The report prints only aggregate counts and non-sensitive IDs; no credentials, tokens, passwords, or connection strings are read or emitted.

## Error Handling And Idempotency
Missing DB facts, missing code markers, or deployment mismatch are represented as explicit status values with `smallest_missing_field`. Re-running the report is idempotent.

## State Transition And Lifecycle
No order, position, risk, or lifecycle state transitions are modified. The truth surface classifies observed guard deployment/effectiveness only.

## Caching And Performance
The added queries are small aggregate scans over existing decision rows. No cache keys or runtime caches are modified.

## Logging, Monitoring, Auditing
Runtime truth gains audit-friendly fields for guard code presence, deployed-head match, recent guard hits, blocked live entry hits, and latest guard-hit evidence.

## Testing Strategy
Add unit tests for DB probe coverage, no-trigger classification, blocked-entry classification, and live runtime fact projection.

## Migration, Rollback, Compatibility
No migration required. Rollback is a normal code revert of the report/test/doc changes.

## Configuration And Environment Isolation
No new environment variables. Existing report arguments and container/env loading remain unchanged.

## Code Organization And Dependencies
Keep implementation in `scripts/runtime_truth_report.py`; no new dependencies.

## Documentation And Operations Manual
Operators should read `directional_impulse_chase_guard_truth.status` and the corresponding `runtime.live_runtime_facts` keys when assessing whether spike-chase protection is deployed and being sampled.

## Deployment And Acceptance Criteria
Safe-readonly eligible: implement, test, commit, push, deploy through `scripts/deploy.sh --skip-commit`, and smoke with runtime truth. Acceptance: no blocking findings, deployed matches Windows, guard truth field present, and `directional_1h` remains `none_verified`.
