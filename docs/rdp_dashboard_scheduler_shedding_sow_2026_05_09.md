# RDP Dashboard Scheduler Shedding SOW - 2026-05-09

## Business Objectives And Boundaries

Reduce repeated operator dashboard background reads from RDP panels while preserving the intended RDP workflow semantics and first-screen RDP governance visibility. This change does not alter RDP workflow scheduling, research execution, approval gates, release controls, trading decisions, risk thresholds, or order submission.

## Module Responsibilities And Domain Model

`DashboardSnapshotPolicy` owns operator dashboard snapshot prewarm and scheduler refresh behavior. RDP workflow configs under `configs/rdp_workflows` remain the source of truth for actual RDP task scheduling. `aats.api.rdp_control_summary` remains the source of RDP operator payloads.

## Input/Output Interfaces

No API route or payload shape changes. The RDP dashboard still requests `rdpControl`, `rdpWorkbenchOverview`, `rdpWorkbenchItems`, `rdpWorkbenchAlerts`, `rdpTuningOverview`, and `rdpTuningProposals` through the same dashboard bundle panel keys and direct RDP endpoints.

## Database Schema / Tables / Indexes / Constraints

No schema, migration, table, index, or constraint change.

## Transactions, Consistency, Concurrency

No writes are introduced. The dashboard snapshot plane keeps the same singleflight and priority concurrency behavior. Only policy participation in startup/scheduler refresh changes.

## Authorization, Authentication, Data Security

No auth change. No secrets, environment credentials, or tokens are read or logged.

## Error Handling And Idempotency

RDP panel reads remain idempotent. Missing deferred snapshots continue to return default payload plus loading metadata and enqueue a refresh on demand.

## State Transition And Lifecycle

No RDP task lifecycle, approval, recommendation, release, rollback, or observation behavior changes. This is strictly a dashboard read-load control.

## Caching And Performance

RDP primary panels (`rdpControl`, `rdpWorkbenchOverview`, `rdpTuningOverview`) keep startup prewarm so the RDP view is not reset to an empty first screen after deploy. All RDP dashboard panels stop periodic scheduler refresh. RDP deferred panels (`rdpWorkbenchItems`, `rdpWorkbenchAlerts`, `rdpTuningProposals`) also skip startup prewarm because they are loaded after the primary RDP bundle.

## Logging, Monitoring, Auditing

Continue using `dashboard_snapshot_refresh_success`, `dashboard_snapshot_refresh_timeout`, `dashboard_bundle_slow`, and `parallel_fetch_slow`. Post-deploy monitoring should show no `reason=scheduler_*` refreshes for RDP panels while keeping startup refreshes for the primary RDP panels.

## Testing Strategy

Add a dashboard snapshot policy test proving RDP primary panels preserve startup prewarm while all RDP panels are excluded from scheduled refresh, and RDP deferred panels are also excluded from startup prewarm. Run ruff, full unit tests, and narrow operator dashboard bundle integration tests.

## Migration, Rollback, Compatibility

Rollback is a code revert. Public RDP API responses and workflow semantics remain compatible.

## Configuration And Environment Isolation

No new configuration or environment variables.

## Code Organization And Dependencies

Changes stay in dashboard snapshot policy, tests, and this SOW. No new dependencies.

## Documentation And Operations Manual

This document records the boundary between RDP workflow automation and operator dashboard snapshot refresh. Disabling dashboard scheduler refresh does not disable any RDP workflow schedule.

## Deployment And Acceptance Criteria

Deploy with `bash scripts/deploy.sh --profile derivatives-live --skip-commit`. Acceptance requires healthy derivatives-live app containers, gateway `/healthz` ok, no dashboard timeouts or ERROR-level gateway logs, and no RDP `reason=scheduler_*` snapshot refreshes in the post-deploy monitoring window.
