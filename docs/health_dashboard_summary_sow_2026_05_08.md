# Health Dashboard Summary Read Optimization SOW - 2026-05-08

## Business objectives and boundaries

Reduce operator dashboard `health` panel refresh latency without changing trading behavior, risk controls, order submission, reconciliation, recovery, or the full `/system/health` contract.

This change is limited to the dashboard summary read path used by the snapshot plane and dashboard bundle. Full health reads remain authoritative and can continue to load complete audit counts and recovery details.

## Module responsibilities and domain model

- `RuntimeQueryFacade.build_system_health`: owns health payload assembly.
- `RecoveryQueryFacade.recovery_view_dashboard`: already provides the recovery summary and latest account baseline needed by the first screen.
- `AuditRepository.count`: remains available for full health and audit/replay detail surfaces.
- Frontend dashboard views consume `health.runtime_state`, `health.halted`, `health.blockers`, `health.warnings`, and subsystem freshness summaries. They do not need a synchronous audit row count for first paint.

## Input/output interfaces

Input remains the same runtime services and repositories. Dashboard output keeps the existing health payload shape and adds explicit dashboard markers:

- `dashboard_summary_only = true`
- `truth_source = runtime_health_dashboard_summary`
- `subsystems.audit_replay.audit_record_count = null`
- `subsystems.audit_replay.audit_record_count_status = deferred_from_dashboard_summary`

Full health output remains unchanged.

## Database schema / tables / indexes / constraints

No schema changes. The optimization removes one dashboard-time `audit_repo.count()` call and one duplicate dashboard-time account baseline lookup; it does not add tables, indexes, or constraints.

## Transactions, consistency, concurrency

No writes are added. Dashboard summary health continues to avoid `_persist_blocker_snapshot`. The optimization reduces concurrent read fan-out during snapshot refreshes.

## Authorization, authentication, data security

No authentication or authorization change. No secrets are read or emitted.

## Error handling and idempotency

Dashboard health remains idempotent. If recovery summary lacks a baseline key in a stub or older runtime, dashboard health falls back to `None` rather than issuing a second read.

## State transition and lifecycle

No trading or recovery state transition changes.

## Caching and performance

The dashboard summary path avoids:

- duplicate `latest_account_baseline` lookup already covered by recovery summary
- synchronous audit distinct decision count in first-screen health

The detail/full health path keeps current caching and count behavior.

## Logging, monitoring, auditing

No new log stream. Acceptance is based on gateway `dashboard_snapshot_refresh_*` logs for `panel_key=health`.

## Testing strategy

Unit tests assert dashboard health reuses recovery baseline and does not call the full account baseline or audit count loaders.

Validation:

- ruff
- focused unit tests
- full unit suite
- narrow WSL integration for dashboard bundle snapshot fallback paths

## Migration, rollback, compatibility

Rollback is a single commit revert. Payload compatibility is maintained by keeping `audit_record_count` present with a `null` value in dashboard summaries.

## Configuration and environment isolation

No config changes. The change applies only inside the dashboard summary read path.

## Code organization and dependencies

No new dependencies. Changes stay in `RuntimeQueryFacade` and existing operator dashboard tests.

## Documentation and operations manual

This SOW records the performance contract and acceptance checks for the health dashboard read path.

## Deployment and acceptance criteria

Deploy with the standard WSL2 script after commit. Accept when:

- required derivatives-live containers are healthy
- DB has no active long query backlog
- stable gateway logs show no `panel_key=health` snapshot timeouts over the monitoring window
