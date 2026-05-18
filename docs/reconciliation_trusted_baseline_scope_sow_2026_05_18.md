# Reconciliation Trusted Baseline Scope SOW

Date: 2026-05-18

## Business objectives and boundaries

Prevent reconciliation and repair from treating local recovery snapshots as trusted exchange portfolio baselines. This change is limited to baseline selection semantics for exchange-coupled reconciliation; it does not mutate live trading state, order state, OKX state, Redis, or database schema.

## Module responsibilities and domain model

- `aats.schemas.portfolio`: already defines trusted baseline origins as `exchange_import` and `operator_rebaseline`.
- `aats.services.runtime_scope`: owns scoped read helpers used by recovery/reconciliation code.
- `aats.storage.portfolio_repo` and `aats.storage.portfolio_repo_postgres`: provide efficient latest scoped snapshot reads.
- `aats.services.reconciliation_service.repair`: rebuilds comparison/repair snapshots and passes baseline trust into the comparator.
- `aats.services.recovery_control.startup_recovery`: preserves the most specific execution-ledger recovery action when a generic open-order recovery block is refined by Phase 4 checks.

## Input/output interfaces

Inputs are portfolio snapshots, execution fills, order states, exchange account snapshots, and runtime scope. Outputs remain `PortfolioSnapshot` and `ReconciliationReport`. No public API payload shape changes.

## Database schema / tables / indexes / constraints

No schema changes. Postgres trusted baseline lookup filters existing JSON payload field `snapshot_origin` with SQLAlchemy `.as_string()`.

## Transactions, consistency, concurrency

Reads are point-in-time repository reads. Existing persistence paths and transaction boundaries are unchanged.

## Authorization, authentication, data security

No credentials are read or printed. The change reduces risk by preventing local recovery artifacts from being promoted to exchange-trusted accounting truth.

## Error handling and idempotency

If no trusted baseline exists, reconciliation falls back to full local replay for comparison/repair and marks exchange portfolio comparison as untrusted. This is fail-closed for live recovery.

## State transition and lifecycle

`exchange_import` and `operator_rebaseline` remain trusted baseline origins. `runtime_bootstrap`, `recovery_rebuild`, `recovery_auto_healed`, and `manual_rebuild` remain baseline-like for historical replay but are not trusted exchange baselines.

## Caching and performance

In-memory reads scan scoped snapshots. Postgres gets a single latest trusted baseline query. No cache changes.

## Logging, monitoring, auditing

No new logs. Reconciliation report classification becomes more accurate because comparator receives a correct trust flag.

## Testing strategy

Add focused unit coverage for:

- scoped trusted baseline helper ignores `recovery_auto_healed` and returns `exchange_import`;
- reconciliation report builder does not set trusted exchange baseline from a recovery snapshot;
- repair baseline-aware rebuild ignores `recovery_auto_healed` and falls back to full replay;
- existing exchange baseline repair behavior remains covered.
- Phase 4 recovery keeps `halted_created_orders_missing_submit_commands` instead of the generic open-order action when a created order lacks a submit command.

Run ruff, focused reconciliation tests, full unit tests, and narrow WSL2 recovery/reconciliation and RDP workflow integration tests.

## Migration, rollback, compatibility

No migration. Rollback is reverting this patch. Public repository methods are additive and backward compatible.

## Configuration and environment isolation

No new config. Behavior follows existing `bootstrap_portfolio_from_exchange` and runtime scope.

## Code organization and dependencies

Keep the new helper beside existing runtime scope helpers. Do not add dependencies.

## Documentation and operations manual

This SOW documents that only operator/exchange imported snapshots may be used as trusted exchange baselines for reconciliation.

## Deployment and acceptance criteria

Acceptance requires tests passing, commit on clean `main`, deployment through `bash scripts/deploy.sh --profile derivatives-live --skip-commit`, gateway health, required app containers healthy, and kill switch still explicit after deploy.
