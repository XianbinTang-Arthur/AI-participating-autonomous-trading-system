# SOW: Claimed submit recovery blocker

## Business objective and boundaries

Prevent a stale `CLAIMED` submit command for OKX `BTC-USDT-SWAP` from being
treated as a generic pending command after startup recovery halts the runtime.
The change must not resubmit, cancel, tune, release, or otherwise alter live
trade intent. It only makes the recovery posture distinguish an ambiguous
claimed submit from a normal pending command.

## Module responsibilities and domain model

- `ExecutionLedgerRecoveryService` owns startup recovery posture for execution
  ledger gaps.
- `execution_commands.state=CLAIMED` means a worker claimed the command before
  terminal command acknowledgement.
- A local order in `CREATED` or `SUBMITTING` with no `venue_order_id` and a
  matched `CLAIMED` submit command is not safe for blind replay. It requires
  exchange reconciliation by client order id before automatic continuation.

## Input/output interfaces

Input:
- `execution_order_repo.open_orders()`
- `execution_command_repo.get_by_idempotency_key()`
- `execution_command_repo.command_counts()`

Output:
- `RecoveryStatus.claimed_submit_command_count`
- `resume_blocked_reasons` includes
  `claimed_submit_commands_require_exchange_reconciliation`
- `notes` includes `claimed_submit_commands_require_exchange_reconciliation:N`

## Database schema / tables / indexes / constraints

No schema changes. The logic reads existing execution order and command rows.

## Transactions, consistency, concurrency

This change does not mutate command or order rows. It avoids changing command
state based on insufficient evidence. The conservative invariant is that a
claimed submit attached to an unacknowledged local order remains blocked until
exchange reconciliation proves its terminal or accepted state.

## Authorization, authentication, data security

No credentials are read or emitted. The recovery status exposes only counts and
reason codes.

## Error handling and idempotency

If the command or order repository lacks a required lookup API, behavior falls
back to existing generic pending/stranded handling. The new blocker is derived
idempotently from current repository state.

## State transition and lifecycle

No order state transition is introduced. The lifecycle change is classification
only:

`CLAIMED submit + CREATED/SUBMITTING order + no venue_order_id`
becomes `claimed_submit_commands_require_exchange_reconciliation`.

## Caching and performance

The check reuses the existing `open_orders()` scan and at most the existing two
submit idempotency lookup keys per open order.

## Logging, monitoring, auditing

Recovery status notes and blocker reason now provide a precise audit handle for
operator and UI surfaces.

## Testing strategy

Add unit coverage for the recovery status classification. Run focused unit
tests plus the repository lint/unit validation path.

## Migration, rollback, compatibility

Backward compatible API extension: one optional integer field is added to
`RecoveryStatus`. Rollback is a normal code revert.

## Configuration and environment isolation

No new configuration. Applies only when execution ledger recovery is enabled.

## Code organization and dependencies

The change stays inside recovery posture code and schema definitions. No new
dependency.

## Documentation and operations manual

This document records the bounded task and acceptance criteria.

## Deployment and acceptance criteria

Acceptance criteria:
- A matched stale `CLAIMED` submit command on a `CREATED`/`SUBMITTING` order is
  not reported only as generic `pending_execution_commands`.
- Recovery status contains the exact blocker
  `claimed_submit_commands_require_exchange_reconciliation`.
- Focused tests pass.

Deployment is deferred because this changes runtime recovery behavior.
