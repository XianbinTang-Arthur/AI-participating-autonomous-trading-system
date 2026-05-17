# Reconciliation Execution Truth Source SOW - 2026-05-17

## Business objectives and boundaries
Make reconciliation read local orders and fills from the current execution truth tables, `execution_orders` and `execution_fills`. Do not enable global financial convergence mode or change the live order/fill write path.

## Module responsibilities and domain model
`StorageBackends.execution_repo` remains the runtime execution repository used by execution services. A dedicated `reconciliation_execution_repo` supplies read-only reconciliation and recovery views backed by the converged execution truth model.

## Input/output interfaces
Input remains the same `ExecutionRepository` protocol used by `ReconciliationService` and `ExecutionRecoveryService`. Output remains the existing reconciliation report and recovery status payloads.

## Database schema / tables / indexes / constraints
No schema changes. The read source changes from legacy `order_states` / `fill_events` to v2 `execution_orders` / `execution_fills` through `ConvergedPostgresExecutionRepository`.

## Transactions, consistency, concurrency
No new writes are introduced. Reconciliation reads use the existing repository SQL queries and existing table indexes.

## Authorization, authentication, data security
No API or auth change. No credentials are read or printed.

## Error handling and idempotency
The storage field is optional and falls back to `execution_repo` for memory or unusual test runtimes.

## State transition and lifecycle
Execution lifecycle writes remain unchanged. Reconciliation and recovery now evaluate the same current execution truth that the operator surfaces already use.

## Caching and performance
No new cache. Query volume is unchanged; query target tables change to indexed execution truth tables.

## Logging, monitoring, auditing
No logging change. Existing reconciliation reports will reflect the corrected local truth source.

## Testing strategy
Add unit coverage for wiring so reconciliation and recovery slices receive the dedicated execution truth repo. Run lint, focused tests, full unit tests, and a targeted WSL2 integration check.

## Migration, rollback, compatibility
No migration. Rollback is a code revert. In-memory runtimes and tests without the dedicated repo continue to use the existing execution repo.

## Configuration and environment isolation
No environment variable changes. This avoids turning on full financial convergence in live profile while still fixing reconciliation reads.

## Code organization and dependencies
The change is contained in bootstrap wiring. No new dependency is added.

## Documentation and operations manual
This SOW records that reconciliation should read current execution truth even when full financial convergence is not globally enabled.

## Deployment and acceptance criteria
Acceptance requires reconciliation reports to stop treating v2-only execution fills as unbooked local fills, with tests passing.
