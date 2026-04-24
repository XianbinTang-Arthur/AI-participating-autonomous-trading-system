# Task217 Lifecycle Snapshot Refs Operator Exposure SOW

## Business Objective And Boundaries
- Expose deployed `lifecycle_snapshot_refs` from execution order/fill raw payloads through operator/control-plane execution record payloads.
- Increase P1 lifecycle truth auditability without adding strategy, symbol, venue, AI autonomy, release, promotion, tuning, or runtime timeframe changes.

## Module Responsibilities And Domain Model
- `aats.services.operator.query_service.OperatorQueryService` remains the read-side flattening layer for execution order/fill records.
- Existing write-side lifecycle stages remain owned by task216 writers:
  - `submit`: execution order service / outbox submit seed.
  - `ack`: converged execution repo order-state updates.
  - `fill`: outbox/converged fill persistence.

## Input / Output Interfaces
- Input: execution order/fill dict rows with `raw_payload.lifecycle_snapshot_refs`, or nested fallback paths under `order_state`, `fill_event`, or `intent`.
- Output: flattened payload includes `lifecycle_snapshot_refs` as a machine-readable dict, or `None` for legacy/malformed records.

## Database Schema / Tables / Indexes / Constraints
- No schema change.
- No migration.
- Source data remains in existing `execution_orders.raw_payload` and `execution_fills.raw_payload`.

## Transactions, Consistency, Concurrency
- Read-only transformation.
- No new transaction boundaries or concurrency changes.

## Authorization, Authentication, Data Security
- No credential handling.
- No secret output.
- Exposes only snapshot reference ids already present in execution raw payloads.

## Error Handling And Idempotency
- Malformed or non-dict lifecycle payloads degrade to `None`.
- Stage entries that are not dicts are skipped.
- Missing standard snapshot ref keys are filled with `None` for predictable output shape.

## State Transition And Lifecycle
- Does not create new lifecycle stages.
- Does not alter order or fill state machines.

## Caching And Performance
- Constant-time in-memory dict normalization per returned execution record.
- No additional database queries.

## Logging, Monitoring, Auditing
- No new logs.
- Audit improvement is the direct read-side visibility of submit/ack/fill lifecycle refs.

## Testing Strategy
- Unit tests lock:
  - top-level lifecycle refs are surfaced for order rows.
  - nested order-state lifecycle refs are surfaced when top-level data is absent.
  - fill lifecycle refs are surfaced.
  - malformed or absent lifecycle refs return `None`.

## Migration, Rollback, Compatibility
- Backward-compatible output field addition.
- Rollback by reverting this read-side change and tests.

## Configuration And Environment Isolation
- No configuration changes.

## Code Organization And Dependencies
- Reuses `SNAPSHOT_REF_KEYS` from the task216 lifecycle helper to keep ref names consistent.
- No new third-party dependencies.

## Deployment And Acceptance Criteria
- Unit tests for operator execution record truth exposure pass.
- Ruff passes for touched files.
- No strategy/timeframe/symbol/venue/AI autonomy drift.
