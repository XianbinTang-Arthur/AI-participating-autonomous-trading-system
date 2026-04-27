# Directional Order Intent Handoff Storage Label SOW

## Business Objectives and Boundaries
- Objective: keep execution guard decisions auditable when directional order intents are blocked before venue submission.
- Boundary: do not change strategy, risk, provider, symbol, venue, schema, or live order approval behavior.
- Scope remains OKX + BTC-USDT-SWAP directional.

## Module Responsibilities and Domain Model
- `OrderManager` decides whether an `OrderIntent` should submit or be blocked.
- `PostgresExecutionOutboxPublisher` persists the resulting `OrderState`.
- `PostgresExecutionOrderRepository` projects execution truth into `execution_orders`.
- `execution_orders.execution_style` and `execution_orders.source_system` are narrow database labels, not the full semantic payload.

## Input/Output Interfaces
- Input: blocked `OrderState` with full `submission_mode`, for example `semantic_duplicate_snapshot_blocked`.
- Output: bounded storage labels in `execution_orders` columns while raw JSON payload preserves the full mode and error.

## Database Schema / Tables / Indexes / Constraints
- `execution_orders.execution_style` is `VARCHAR(32)`.
- `execution_orders.source_system` is `VARCHAR(32)`.
- This task does not change schema.

## Transactions, Consistency, Concurrency
- The storage label projection happens inside the existing order row creation path.
- It does not alter transaction boundaries or idempotency keys.

## Authorization, Authentication, Data Security
- No credentials or connection strings are read or printed.
- No operator authentication behavior changes.

## Error Handling and Idempotency
- The fix prevents `StringDataRightTruncation` from hiding blocked order evidence.
- Exact semantic details remain in `raw_payload.order_state.submission_mode` and `raw_payload.source_system`.

## State Transition and Lifecycle
- Blocked intents remain `BLOCKED`.
- No order that was blocked becomes executable because of this change.

## Caching and Performance
- Label projection is constant-time and local.
- No cache shape changes.

## Logging, Monitoring, Auditing
- Existing logs remain unchanged.
- Auditability improves because blocked order rows can persist instead of causing NATS handler redelivery loops.

## Testing Strategy
- Unit-test known long blocked labels.
- Unit-test unknown long labels for deterministic bounded projection.
- Unit-test repository projection preserves full raw payload.

## Migration, Rollback, Compatibility
- No migration required.
- Rollback: revert the code commit; no historical order mutation is performed.

## Configuration and Environment Isolation
- No configuration changes.
- No environment-specific behavior.

## Code Organization and Dependencies
- Add a small storage helper in `aats/storage/execution_order_labels.py`.
- Use it in `PostgresExecutionOrderRepository` at the final database projection boundary.

## Documentation and Operations Manual
- This SOW is the operations note for the handoff reliability fix.

## Deployment and Acceptance Criteria
- Acceptance:
  - Long blocked labels cannot exceed `VARCHAR(32)` in `execution_orders`.
  - Full semantic label remains available in JSON payload.
  - Focused unit tests pass.
  - Full `aats/` lint passes.
- Deployment is deferred to a separate `git-sync-and-deploy` round because this touches the execution persistence path.
