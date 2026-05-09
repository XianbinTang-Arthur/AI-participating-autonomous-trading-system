# Guarded Live Risk Dashboard On-Demand SOW - 2026-05-09

## Business Objectives And Boundaries

Reduce operator dashboard background refresh load from guarded-live risk panels without changing trading decisions, risk thresholds, order generation, kill-switch behavior, recovery state, or full operator report semantics.

## Module Responsibilities And Domain Model

`DashboardSnapshotPolicy` owns whether a panel participates in startup prewarm and scheduler refresh. `OperatorQueryService` remains the source of guarded-live preflight and run-packet payloads. The risk page still requests those panels as deferred view data, while the runtime panel keeps its lightweight guarded-live summary for first-screen status.

## Input/Output Interfaces

No API route changes. `guardedLivePreflight` and `guardedLiveRunPacket` keep the same dashboard panel keys and response shapes. Missing snapshots still return default payloads and enqueue on-demand refresh when the panel is requested.

## Database Schema / Tables / Indexes / Constraints

No database, migration, index, or constraint change.

## Transactions, Consistency, Concurrency

No writes or transactions are introduced. The existing snapshot plane singleflight and per-priority concurrency controls remain unchanged.

## Authorization, Authentication, Data Security

No auth changes. No secrets, credentials, or environment values are read or logged.

## Error Handling And Idempotency

Panel reads remain idempotent. If the user opens the risk page and a snapshot is missing, the dashboard snapshot plane returns the default payload with loading metadata and refreshes the panel in the background.

## State Transition And Lifecycle

No state-machine change. Guarded-live preflight and run-packet summaries are operator read surfaces only; they do not authorize or block execution by themselves.

## Caching And Performance

`guardedLivePreflight` and `guardedLiveRunPacket` no longer participate in startup prewarm or periodic scheduler refresh. This prevents deferred risk panels from consuming background snapshot workers when the operator is not looking at the risk page. Direct risk-page demand still uses the existing TTL cache and dashboard summary builders.

## Logging, Monitoring, Auditing

Continue relying on `dashboard_snapshot_refresh_success`, `dashboard_snapshot_refresh_timeout`, `dashboard_bundle_slow`, and `parallel_fetch_slow`. Post-deploy monitoring should show no startup/scheduler refresh for these two panels unless the operator explicitly requests them.

## Testing Strategy

Add policy tests proving guarded-live risk P2 panels are on-demand and not startup/scheduled. Run ruff, full unit tests, and the narrow operator dashboard bundle integration test.

## Migration, Rollback, Compatibility

Rollback is a code revert. Public response fields and direct report endpoints remain compatible.

## Configuration And Environment Isolation

No new configuration or environment variables.

## Code Organization And Dependencies

Changes stay in dashboard snapshot policy, tests, and this SOW. No new dependencies.

## Documentation And Operations Manual

This document records why guarded-live risk details are loaded on demand: the runtime panel already carries a first-screen safety summary, while detailed preflight/run-packet diagnostics belong to the risk page.

## Deployment And Acceptance Criteria

Deploy with `bash scripts/deploy.sh --profile derivatives-live --skip-commit`. Acceptance requires healthy derivatives-live app containers and monitoring that shows zero startup/scheduler refreshes for `guardedLivePreflight` and `guardedLiveRunPacket` in the post-deploy window, with no dashboard timeouts or ERROR-level gateway logs.
