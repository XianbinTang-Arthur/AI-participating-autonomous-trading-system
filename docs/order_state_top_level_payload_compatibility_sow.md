# OrderState Top-Level Payload Compatibility SOW

## Business Objectives and Boundaries
Fix a compatibility gap where historical `execution_orders.raw_payload` rows can store an `OrderState` as a top-level payload rather than under `order_state`. The change is limited to order-state hydration for control paths and execution-order column synchronization.

## Module Responsibilities and Domain Model
- `OrderManager` hydrates `OrderState` for operator/control actions from the active execution repository first, then the phase-2 `execution_orders` row.
- `PostgresExecutionOrderRepository` keeps selected `execution_orders` columns aligned with the persisted order-state payload.

## Input/Output Interfaces
Inputs are existing `execution_orders` rows and `raw_payload` dictionaries. Outputs remain `OrderState` objects and updated `ExecutionOrderModel` fields. No public API changes are introduced.

## Database Schema / Tables / Indexes / Constraints
No schema, index, or constraint changes. The affected table is `execution_orders`.

## Transactions, Consistency, Concurrency
Existing optimistic `state_version` checks remain unchanged. Column sync now accepts both nested `raw_payload.order_state` and top-level `OrderState` payloads.

## Authorization, Authentication, Data Security
No auth behavior changes. No secrets are read or logged.

## Error Handling and Idempotency
Hydration remains fail-closed through existing validation. Repository updates remain idempotent under the existing expected version guard.

## State Transition and Lifecycle
`execution_orders.state` remains authoritative for hydrated status. Top-level payload financial truth such as filled quantity, remaining quantity, average fill price, and fees is preserved.

## Caching and Performance
No new queries or caches. The change is in-memory dictionary normalization only.

## Logging, Monitoring, Auditing
No new logging. Existing execution order history and reconciliation surfaces remain unchanged.

## Testing Strategy
Add focused unit coverage for FILLED top-level `OrderState` hydration and top-level payload column synchronization.

## Migration, Rollback, Compatibility
Backward compatible with nested payloads, top-level payloads, and fallback rows without order-state-shaped payloads. Rollback is a code revert only.

## Configuration and Environment Isolation
No configuration changes.

## Code Organization and Dependencies
Keep helper logic local to the affected modules. No new runtime dependency.

## Documentation and Operations Manual
This SOW documents the scope. No operator procedure changes.

## Deployment and Acceptance Criteria
Acceptance requires ruff plus focused unit tests and the repository unit suite to pass, or any unrelated blocker to be explicitly reported.
