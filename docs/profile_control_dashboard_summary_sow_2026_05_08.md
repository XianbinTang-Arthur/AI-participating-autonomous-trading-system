# Profile Control Dashboard Summary SOW - 2026-05-08

## Business Objectives And Boundaries

Reduce `profileControlSummary` dashboard refresh latency without changing strategy profile activation, optimization, recommendation, or manual control semantics.

## Module Responsibilities And Domain Model

`OperatorQueryService` owns operator read models and dashboard panel payloads. `StrategyProfileQueryFacade` owns strategy profile read composition. `StrategyProfileControlService` remains the source of activation state, latest optimization reports, latest selection decisions, and profile-control evidence.

## Input/Output Interfaces

Input is the active runtime strategy profile service and current operator scope. Output remains the existing `/reports/profile-control-summary` shape: `control_summary`, `activation`, `active_revision`, `latest_selection_decision`, and summarized `latest_optimization_report`.

## Database Schema / Tables / Indexes / Constraints

No schema, table, index, or constraint change. This is a read-path composition change only.

## Transactions, Consistency, Concurrency

No writes or transactions are introduced. Existing operator TTL/singleflight cache remains the concurrency boundary for dashboard reads.

## Authorization, Authentication, Data Security

No auth behavior changes. No secrets are read or logged.

## Error Handling And Idempotency

Read operations remain idempotent. Existing dashboard snapshot timeout and stale-panel handling remains unchanged.

## State Transition And Lifecycle

No strategy profile lifecycle transition changes. Activation and selection state are only read.

## Caching And Performance

`profileControlSummary` should no longer build the full strategy profile snapshot. It should compose a summary from seed/activation state, active revision, latest optimization payload, latest selection payload, and only compute fallback control evidence when the latest optimization report lacks a `control_summary`.

## Logging, Monitoring, Auditing

No new logs are required. Existing `dashboard_snapshot_refresh_success`, `dashboard_snapshot_refresh_timeout`, and `parallel_fetch_slow` logs remain the acceptance signal.

## Testing Strategy

Add unit coverage proving the summary path does not call the full strategy profile snapshot or heavy tuning context when the latest optimization payload already carries a control summary. Keep existing API integration coverage for `/reports/profile-control-summary`.

## Migration, Rollback, Compatibility

Rollback is a code revert. Public response fields are preserved.

## Configuration And Environment Isolation

No configuration or environment changes.

## Code Organization And Dependencies

Changes stay in operator strategy profile read facades, focused unit tests, and this SOW. No new dependencies.

## Documentation And Operations Manual

This SOW documents the dashboard read-path boundary. Full strategy profile snapshots remain available through the existing full profile endpoint.

## Deployment And Acceptance Criteria

Acceptance requires focused unit/integration tests, full unit suite, deployment via `scripts/deploy.sh --profile derivatives-live --skip-commit`, and post-deploy monitoring showing `profileControlSummary` no longer has multi-second refreshes.
