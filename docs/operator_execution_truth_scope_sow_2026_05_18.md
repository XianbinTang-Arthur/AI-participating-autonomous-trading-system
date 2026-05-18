# Operator Execution Truth Scope SoW

## Business Objectives and Boundaries

Ensure operator views that compare local execution truth with exchange state read the same scoped execution ledger that reconciliation uses. The change is limited to read paths for `execution_orders` and truth-source labeling; it does not alter order submission, exchange adapters, position sizing, ledger writes, or runtime recovery state.

## Module Responsibilities and Domain Model

- `OperatorQueryService` owns scoped operator read helpers for execution truth.
- `AccountQueryFacade` renders account/order panels from those helpers.
- `PostgresExecutionOrderRepository` owns SQL reads from `execution_orders`.

## Input/Output Interfaces

Inputs are runtime scope fields: product type, margin mode, allowed symbols, offset, limit, and an `open_only` flag. Outputs remain JSON-compatible execution order payloads.

## Database Schema / Tables / Indexes / Constraints

No schema changes. Reads continue to use `execution_orders` and existing indexes.

## Transactions, Consistency, Concurrency

The patch is read-only. It removes an unscoped `open_orders()` dashboard read in favor of scope-aware `list_orders_for_scope(..., open_only=True)`.

## Authorization, Authentication, Data Security

No new credentials or operator privileges. No secrets are read or logged.

## Error Handling and Idempotency

Existing read failures propagate unchanged. Truth-source labels now distinguish `reconciliation_execution_repo` fallback from the legacy `execution_repo`.

## State Transition and Lifecycle

No lifecycle or state transition changes. Terminal execution order states are excluded only when the caller requests open orders.

## Caching and Performance

Scope-aware SQL filtering prevents cross-scope data from entering the operator surface. The bounded fallback remains in place for repositories without a scope-aware reader.

## Logging, Monitoring, Auditing

No new logs. Operator payload truth-source labeling becomes more precise.

## Testing Strategy

Add unit coverage for scoped phase5 open order reads and reconciliation execution truth source labeling. Re-run targeted operator/reconciliation tests, full unit tests, and the narrow WSL2 integration subset affected by reconciliation/recovery.

## Migration, Rollback, Compatibility

`open_only` is an optional repository argument with a default of `False`. Rollback is a normal code revert.

## Configuration and Environment Isolation

No config or environment changes.

## Code Organization and Dependencies

Use existing operator facade and repository boundaries. No new dependencies.

## Documentation and Operations Manual

This SoW records the review finding and acceptance criteria.

## Deployment and Acceptance Criteria

Deploy through `bash scripts/deploy.sh --skip-commit` after commit. Acceptance requires passing validation and gateway health after deploy.
