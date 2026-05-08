# ExecutionLatest Dashboard Summary SOW - 2026-05-08

## Business Objectives And Boundaries

Reduce operator dashboard snapshot latency for the `executionLatest` panel without changing trading decisions, execution behavior, risk thresholds, reconciliation semantics, or the public `/execution/latest` diagnostic payload.

## Module Responsibilities And Domain Model

`AccountQueryFacade` owns account/execution read models. `OperatorQueryService` owns TTL/singleflight caching. `auth_routes` chooses dashboard bundle and snapshot panel loaders. The domain payload remains latest order, latest fill, latest reconciliation, runtime freshness flags, terminal-no-fill explanation, execution readiness, and summary metadata.

## Input/Output Interfaces

Input is the active `ApplicationRuntime` and current operator scope. Output is the existing `executionLatest` dashboard panel shape plus `dashboard_summary_only`, `recent_failures_deferred`, and `deferred_sections` metadata. Full `/execution/latest` continues to include full `recent_failures`, normalized full recovery context, latest reconciliation, and full execution adapter readiness.

## Database Schema / Tables / Indexes / Constraints

No schema, table, index, or constraint changes. The change only narrows dashboard read paths.

## Transactions, Consistency, Concurrency

No write transactions are introduced. Dashboard calls keep the existing TTL/singleflight cache discipline and use a separate `execution_latest_dashboard` cache key so they do not contend with or alter the full diagnostic cache entry.

## Authorization, Authentication, Data Security

No auth behavior changes. Existing authenticated dashboard bundle routes continue to serve the panel. No secrets are read or logged.

## Error Handling And Idempotency

Dashboard panel fallback behavior remains unchanged: panel errors are isolated by existing bundle timeout/error wrappers. Read operations are idempotent.

## State Transition And Lifecycle

No lifecycle state transitions are added. `recent_failures` are explicitly deferred in dashboard summary payloads because execution errors already have a dedicated `executionErrors` panel/API.

## Caching And Performance

Dashboard `executionLatest` now uses dashboard recovery/mode readers, avoids synchronous `execution_errors()` scanning, defers full execution adapter readiness, defers latest reconciliation, and parallelizes independent latest order/fill/recovery/mode reads. Snapshot-plane loader and request fallback both call `query.execution_latest_dashboard()`.

## Logging, Monitoring, Auditing

No new logs are required. Existing dashboard snapshot timing and timeout logs remain the acceptance signal.

## Testing Strategy

Add unit coverage for the new summary path and source-level guardrails that dashboard bundle loaders call dashboard readers. The dashboard summary unit should fail if full execution adapter readiness or latest reconciliation is accidentally called. Add integration coverage that dashboard bundle fallback does not call the full `execution_latest()` method and UI coverage that deferred reconciliation is not displayed as missing data.

## Migration, Rollback, Compatibility

Rollback is a code revert. Public `/execution/latest` compatibility is preserved; dashboard-only metadata is additive.

## Configuration And Environment Isolation

No configuration or environment changes.

## Code Organization And Dependencies

Changes stay in operator account queries, query-service facade, auth route dashboard loaders, and focused tests. No new dependencies.

## Documentation And Operations Manual

This SOW documents the operational boundary. Operators should continue using `/execution/latest` for full execution diagnostics and dashboard bundle panels for fast UI refreshes.

## Deployment And Acceptance Criteria

Acceptance requires focused unit/integration tests, full unit suite, WSL2 integration subset, deployment through `scripts/deploy.sh`, and post-deploy monitoring showing no new long active DB query and fewer `executionLatest` snapshot timeouts.
