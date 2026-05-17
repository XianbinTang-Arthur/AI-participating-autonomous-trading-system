# Execution Truth Read Model Audit SOW - 2026-05-17

## Business objectives and boundaries
Find and fix read-only paths that still treat legacy `order_states` / `fill_events` as current execution truth while live execution facts are present in `execution_orders` / `execution_fills`. Do not change order submission, cancellation, outbox writes, or global financial convergence settings.

## Module responsibilities and domain model
`execution_repo` remains the transitional execution write-path dependency. `execution_truth_repo` is the read model for diagnostics, recovery posture, operator views, decision context, strategy inventory, and portfolio projections that need current local execution facts.

## Input/output interfaces
Inputs stay on the existing `ExecutionRepository` protocol. Outputs keep their existing payload shapes, with truth-source metadata updated where operator payloads previously said legacy execution views were authoritative.

## Database schema / tables / indexes / constraints
No schema changes. The dedicated truth repo uses existing `execution_orders` and `execution_fills` tables through `ConvergedPostgresExecutionRepository`.

## Transactions, Consistency, Concurrency
No new transactions are introduced. Write-path services continue to use their existing repositories; read-model services use the current execution truth adapter.

## Authorization, Authentication, Data Security
No auth change and no credential reads. Operator write fallbacks continue to use existing authenticated paths and are not moved to the read adapter.

## Error Handling and Idempotency
If a runtime does not expose `execution_truth_repo`, helpers fall back to the existing `execution_repo`. This preserves memory tests and non-Postgres runtimes.

## State Transition and Lifecycle
Execution state transitions are unchanged. Startup recovery and operator/recovery read models now observe the same current execution ledger used by v2 truth tables.

## Caching and Performance
Order-state hot cache truth loading is pointed at the current execution truth repo. Query volume is unchanged; read-model fallbacks should hit the indexed v2 tables instead of stale legacy tables.

## Logging, Monitoring, Auditing
Existing logs and reports remain. Operator payload truth-source labels distinguish `execution_truth_repo` from legacy `execution_repo`.

## Testing Strategy
Add unit coverage that operator scoped reads prefer `execution_truth_repo`, keep reconciliation/recovery wiring coverage, and extend Postgres wiring integration checks.

## Migration, Rollback, Compatibility
No migration. Rollback is a code revert. In-memory and older test runtimes remain compatible through fallback to `execution_repo`.

## Configuration and Environment Isolation
No environment variable changes. The change does not require enabling Phase5 control plane or full financial convergence mode.

## Code Organization and Dependencies
The change is limited to bootstrap wiring, runtime-scope helper logic, operator read models, recovery posture, and strategy profile read paths. No dependency is added.

## Documentation and Operations Manual
This SOW records the audit result: read-only execution fact consumers should use `execution_truth_repo`; write-path services should keep their existing execution repository unless explicitly migrated.

## Deployment and Acceptance Criteria
Acceptance requires tests to pass and operator/recovery/strategy read models to observe v2-only orders and fills without requiring global convergence flags.
