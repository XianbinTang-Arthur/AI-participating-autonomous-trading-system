# Operator-gated claimed submit recovery SOW (2026-04-28)

## Business objectives and boundaries

Objective: prevent a `CLAIMED` submit command attached to a local
`CREATED`/`SUBMITTING` no-venue order from being force-resolved by a generic
operator stuck-submission action.

Boundary: this task does not resubmit, cancel, tune, release, promote, mutate
exchange state, or change strategy/risk/provider/symbol/venue behavior. It only
adds an explicit confirmation gate and audit facts to the existing protected
operator recovery action.

## Module responsibilities and domain model

- `OperatorQueryService` identifies whether an order has a matched claimed
  submit command.
- `ReconciliationSystemQueryFacade.resolve_stuck_submission` remains the single
  operator recovery entrypoint.
- `PostgresExecutionOutboxPublisher` remains the production state writer when a
  resolution is actually eligible.

## Input/output interfaces

Input:
- Existing `reason`
- New optional `operator_confirmation`

For claimed submit orders the confirmation must equal:
`resolve_claimed_submit_as_failed:<client_order_id>`.

Output:
- Existing order, resolution, reconciliation, and recovery fields remain.
- `resolution` includes claimed submit command metadata and whether explicit
  confirmation was required.
- Operator action details include the same confirmation gate evidence.

## Database schema / tables / indexes / constraints

No schema changes. The gate reads existing `execution_commands` through the
execution command repository.

## Transactions, consistency, concurrency

The confirmation check runs before any order-state write. If confirmation is
missing or mismatched, no single-writer persist path is called.

## Authorization, authentication, data security

The endpoint keeps existing admin authorization. The new confirmation string is
not a secret. No credentials or connection strings are read or logged.

## Error handling and idempotency

Missing confirmation raises:
`stuck_submission_resolution_blocked:claimed_submit_requires_operator_confirmation`.

The recovery action remains idempotent after the order leaves pre-submit states.

## State transition and lifecycle

No new state transition is introduced. Existing eligible resolution remains:
`CREATED/SUBMITTING -> FAILED`.

The new gate only applies when the order has a matched `CLAIMED` submit command.

## Caching and performance

One low-frequency command lookup is added on the protected operator recovery
path. No hot-path trading code is affected.

## Logging, monitoring, auditing

Resolution responses and operator action details now include:
- `claimed_submit_command_present`
- `claimed_submit_command_id`
- `claimed_submit_idempotency_key`
- `operator_confirmation_required`

## Testing strategy

Focused unit tests cover:
- claimed submit resolution is rejected without exact confirmation.
- claimed submit resolution succeeds with exact confirmation and persists via
  the single-writer outbox path.

## Migration, rollback, compatibility

No migration. Non-claimed historical stuck submission recovery remains backward
compatible with the existing request shape.

Rollback is a code revert before deployment.

## Configuration and environment isolation

No new configuration. Applies to the fixed OKX `BTC-USDT-SWAP` recovery class
through existing operator recovery paths.

## Code organization and dependencies

Changes stay in:
- `aats/api/routes.py`
- `aats/services/operator/query_service.py`
- `aats/services/operator/reconciliation_system_queries.py`
- `aats/api/static/modules/actions/execution-actions.js`
- focused tests

No new third-party dependency.

## Deployment and acceptance criteria

Deployment is deferred because this changes runtime recovery behavior.

Acceptance:
- claimed submit/no-venue orders cannot be force-resolved without exact
  operator confirmation.
- eligible non-claimed stuck submission recovery keeps its current behavior.
- state repair still uses single-writer outbox when executed.
- focused tests and lint pass.
