# Execution Order Truth Sync Recovery SOW - 2026-04-27

## Business Objectives and Boundaries
- Objective: resolve the current BTC-USDT-SWAP directional runtime blocker where `execution_orders` remains `SUBMITTING` while command, fill, and legacy order-state truth prove the order reached `FILLED`.
- Boundary: no order replay, no exchange cancel/submit, no strategy tuning, no risk gate bypass, no symbol/venue/family change, and no timeframe plumbing change.

## Module Responsibilities and Domain Model
- `order_states` is the legacy/control-plane order-state truth.
- `execution_orders` is the execution-ledger order lifecycle truth used by runtime truth and recovery views.
- `execution_fills` and `execution_commands` provide submitted/fill evidence.
- This recovery synchronizes execution-ledger truth from already-terminal order-state truth; it does not invent a terminal status.

## Input/Output Interfaces
- Input: one scoped client/order id, its `order_states` row, `execution_orders` row, `execution_commands` rows, `execution_fills` rows, and execution-order state history.
- Output: `execution_orders.state=FILLED`, `execution_orders.venue_order_id` populated, `execution_orders.raw_payload.order_state.status=FILLED`, and an appended execution-order state-history transition.
- Code output: converged execution repository now also overwrites `execution_orders.raw_payload.status` and `execution_orders.raw_payload.exchange_order_id` on every order-state sync.

## Database Schema / Tables / Indexes / Constraints
- No schema, index, migration, or constraint change.
- Existing tables touched by the operational recovery: `execution_orders` and `execution_order_state_history`.
- Existing evidence tables read: `order_states`, `execution_commands`, `execution_fills`, and `reconciliation_reports`.

## Transactions, Consistency, Concurrency
- The sync uses existing repository code with row-level locking and `state_version` optimistic checking.
- The transition is single-order and idempotent once `execution_orders` leaves `SUBMITTING`.
- It only runs when legacy order state is terminal `FILLED` and local fill evidence exists.

## Authorization, Authentication, Data Security
- No credentials, tokens, API keys, or database URLs are printed.
- The operation uses the already-running container environment for implicit database access.

## Error Handling and Idempotency
- Abort if the order has no matching legacy `order_states` row.
- Abort if legacy status is not `FILLED`.
- Abort if local `execution_fills` does not contain matching fill evidence.
- Abort if `execution_orders` no longer remains in a pre-terminal state.

## State Transition and Lifecycle
- Intended transition: `execution_orders.SUBMITTING -> FILLED`.
- No `CREATED/SUBMITTING -> FAILED` stuck-submission terminalization is applied because the order has ACKED command and fill evidence.

## Caching and Performance
- No hot-path query or cache behavior change.
- Legacy `order_states` is already terminal; this task does not alter Redis order-state cache.

## Logging, Monitoring, Auditing
- `execution_order_state_history` records the transition through the existing converged repository path.
- Runtime truth report should stop reporting a current open `execution_orders` blocker for this order after sync.

## Testing Strategy
- Focused unit coverage for top-level `execution_orders.raw_payload.status` / `exchange_order_id` sync on terminal updates.
- Live validation: scoped DB evidence before/after, plus `scripts/runtime_truth_report.py --output json`.
- Required validation after code change: ruff and full unit tests.

## Migration, Rollback, Compatibility
- No migration required.
- Rollback, if evidence is later disproved, requires an explicit audited operator correction because the live fill and exchange order id already prove terminal execution truth.
- Compatibility is preserved: the recovery uses existing repository APIs.

## Configuration and Environment Isolation
- No environment variable change.
- No AI provider, execution submit path, risk configuration, strategy profile, symbol, venue, or strategy family change.

## Code Organization and Dependencies
- Code change is limited to `aats/storage/execution_repo_converged_postgres.py`.
- No new runtime dependency.

## Documentation and Operations Manual
- Treat this class as `execution_order_truth_lag_after_fill`, not `execution_command_missing_for_created_order`.
- Do not use stuck-submission recovery when command ACK and fill evidence exist.

## Deployment and Acceptance Criteria
- Acceptance criteria:
  1. The scoped order's `execution_orders.state` changes from `SUBMITTING` to `FILLED`.
  2. The scoped order's `execution_orders.venue_order_id` matches the known OKX order id from `order_states` / `execution_fills`.
  3. Runtime truth reports zero current open `execution_orders` for the target-convergence guard surface.
  4. Git remains clean except for intentional documentation/automation state changes.
