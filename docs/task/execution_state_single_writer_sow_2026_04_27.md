# Execution State Single Writer SOW - 2026-04-27

## Business Objectives and Boundaries

Converge production execution order/fill repair writes to
`PostgresExecutionOutboxPublisher`. Operator stuck-submission resolution and
startup exchange stuck-order reconciliation must no longer directly update
`execution_repo`, `execution_order_repo`, or Redis cache as separate side
effects.

This SOW covers repair/recovery/operator paths and guards legacy fallback in
order manager. Full command-service refactoring is out of scope.

## Module Responsibilities and Domain Model

- `PostgresExecutionOutboxPublisher` owns durable `OrderState` and `FillEvent`
  writes for Postgres runtime.
- It writes `order_states` / `fill_events`, durable envelopes, outbox rows, and
  hot-state cache after commit.
- For operator/recovery repair writes, it also synchronizes the `execution_orders`
  row and history in the same transaction.
- Repositories remain low-level storage and test helpers.

## Input and Output Interfaces

Inputs remain `OrderState` and `FillEvent`. Outputs:

- `order_states` row and JSON payload
- `execution_orders` state/raw payload when repair sync is requested
- `execution_order_state_history` transition rows
- `fill_events` row and optional `execution_fills` row
- `events` / `outbox_events`
- Redis hot state via `OrderStateHotCache` / `FillEventHotCache`

No external API response shape changes.

## Database Schema / Tables / Indexes / Constraints

No schema migration. Existing tables:

- `order_states`
- `fill_events`
- `execution_orders`
- `execution_order_state_history`
- `execution_fills`
- `events`
- `outbox_events`

## Transactions, Consistency, Concurrency

Postgres repair writes must commit business rows, execution truth rows, event
rows, and outbox rows in one SQLAlchemy transaction. Cache publication and NATS
publication happen after commit; outbox retry covers publish failure. Existing
OrderState OCC retry remains in the publisher.

## Authorization, Authentication, Data Security

No auth changes. Operator endpoints keep existing role checks and action audit.
No secrets are logged.

## Error Handling and Idempotency

If a Postgres repository is used without an execution outbox publisher, direct
legacy fallback raises a configuration error. Non-Postgres tests may still use
legacy direct writes. Stuck exchange reconciliation treats ambiguous exchange
responses as non-updates.

## State Transition and Lifecycle

Operator stuck submission:

1. build failed `OrderState`
2. persist via execution outbox publisher with execution-truth sync enabled
3. emit operator recovery summary
4. validate reconciliation

Startup exchange reconciliation:

1. inspect stuck `execution_orders`
2. query exchange
3. build corrected `OrderState`
4. persist via execution outbox publisher with execution-truth sync enabled

## Caching and Performance

Publisher post-commit hooks push order/fill hot state to Redis. Repair paths are
low-frequency; additional truth-table sync in the same transaction is acceptable.

## Logging, Monitoring, Auditing

Add explicit logs for rejected direct Postgres writes and successful exchange
reconciliations. Durable `ORDER_UPDATES` outbox rows now exist for repair writes.

## Testing Strategy

- Unit regression for operator stuck-submission resolution using outbox writer.
- Unit regression for startup exchange reconciler using outbox writer and no
  signature mismatch.
- Contract test preventing production services from direct
  `execution_repo.save_order_state` / `save_fill` writes except the legacy guard.
- Existing execution recovery and operator tests remain green.

## Migration, Rollback, Compatibility

No data migration. Rollback is code-only. In-memory unit tests keep direct
fallback compatibility.

## Configuration and Environment Isolation

Execution role already builds `PostgresExecutionOutboxPublisher`; recovery and
operator paths must use the runtime publisher when present.

## Code Organization and Dependencies

Keep changes in execution outbox, recovery control, operator reconciliation
facade, and a small writer guard helper. No new third-party dependency.

## Documentation and Operations Manual

Operators should expect stuck-submission recovery to create durable
`execution.order_updates` outbox-backed events instead of ad-hoc NATS publishes.

## Deployment and Acceptance Criteria

- Operator stuck submission no longer calls direct `execution_repo.save_order_state`.
- Startup exchange reconciler no longer calls the mismatched
  `execution_order_repo.update_order_state(client_order_id=..., updates=...)`.
- Postgres direct fallback fails fast when outbox publisher is missing.
- Unit suite, lint, and narrow integration tests pass.
