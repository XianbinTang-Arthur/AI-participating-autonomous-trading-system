# Portfolio Snapshot Single Writer SOW - 2026-04-27

## Business Objectives and Boundaries

Converge production portfolio snapshot writes to one writer service:
`PostgresPortfolioOutboxPublisher`. Recovery, reconciliation repair, portfolio
bootstrap, and fill-derived projection paths must no longer write Postgres
portfolio snapshots directly and then rely on cache listener side effects.

The boundary is portfolio snapshot persistence and publication only. Execution
order/fill/obligation writer convergence is a separate follow-up.

## Module Responsibilities and Domain Model

- `PostgresPortfolioOutboxPublisher` owns durable `PortfolioSnapshot` writes for
  Postgres runtime.
- `PortfolioRepository` remains a low-level storage abstraction and test helper.
- Services may use a legacy direct fallback only for non-Postgres repositories.
- `PortfolioSnapshotCache` remains a hot-state optimization. It is updated after
  the outbox writer commits; it is not the authoritative writer.

## Input and Output Interfaces

Inputs are existing `PortfolioSnapshot`, `PortfolioBalanceDelta`, and
`FillOutcomeRecord` models. Outputs are:

- `portfolio_snapshots` row
- `event_store` envelope on `portfolio.snapshots`
- outbox row for retryable publication
- best-effort Redis hot-state cache update after commit
- NATS publication through outbox flush

No public schema or API response shape changes.

## Database Schema, Tables, Indexes, Constraints

No schema migration is required. Existing tables remain authoritative:

- `portfolio_snapshots`
- `events`
- `outbox_events`
- `fill_outcomes`

The change is write-path routing, not table shape.

## Transactions, Consistency, Concurrency

For Postgres runtime, a portfolio snapshot write must be inside a single
SQLAlchemy transaction that also appends the durable event and enqueues the
outbox row. Cache publication happens only after commit. Async callers await
cache publication and outbox flush. Sync recovery/repair callers use the same
writer to commit durable rows, then schedule or explicitly await post-commit
publication from an async boundary.

## Authorization, Authentication, Data Security

No new auth surface and no credential access. Logs must not include secrets.

## Error Handling and Idempotency

The Postgres writer is fail-closed for the durable write. Cache and NATS
publication remain best-effort after commit, with outbox retry preserving the
durable event. Direct writes against a Postgres portfolio repository without a
publisher are rejected as configuration errors.

## State Transition and Lifecycle

Portfolio snapshot lifecycle becomes:

1. service builds snapshot
2. `PostgresPortfolioOutboxPublisher` commits snapshot + event + outbox
3. writer updates hot cache after commit
4. writer flushes pending outbox events
5. subscribers update cross-process state from `portfolio.snapshots`

## Caching and Performance

Redis cache updates remain best-effort and post-commit. The previous direct
save listener is no longer required for production wiring. Non-Postgres tests
can still use repository writes.

## Logging, Monitoring, Auditing

Add clear warning/error logs for legacy fallback and misconfigured Postgres
direct writes. Durable audit trail improves because recovery/repair snapshots
now always get event/outbox rows in Postgres runtime.

## Testing Strategy

- Unit tests for generic snapshot persistence through
  `PostgresPortfolioOutboxPublisher`.
- Recovery regression verifying `recovery_auto_healed` uses the publisher.
- Contract test forbidding production service files from directly calling
  `portfolio_repo.save_snapshot(...)`.
- Existing portfolio cache listener tests remain as repository-level
  compatibility tests.

## Migration, Rollback, Compatibility

No data migration. Rollback is code-only. Non-Postgres repositories keep legacy
fallback to avoid breaking unit tests and in-memory demos.

## Configuration and Environment Isolation

In Postgres runtime, `portfolio_outbox_publisher` must be constructed before
portfolio, recovery, and reconciliation services. If construction fails, direct
Postgres snapshot writes fail fast instead of silently bypassing outbox.

## Code Organization and Dependencies

Keep changes in existing portfolio/recovery/reconciliation modules plus a small
writer guard helper. No new third-party dependency.

## Documentation and Operations Manual

This SOW documents the new writer boundary. Operationally, pending
`portfolio.snapshots` outbox rows are now expected for recovery/repair writes
if NATS publication fails.

## Deployment and Acceptance Criteria

Acceptance:

- No production service file directly calls `portfolio_repo.save_snapshot(...)`.
- Recovery auto-heal snapshot writes create durable outbox-backed
  `portfolio.snapshots` events.
- Gateway no longer depends on cache-only direct-save broadcasts for recovery
  visibility.
- Required lint and unit tests pass.
