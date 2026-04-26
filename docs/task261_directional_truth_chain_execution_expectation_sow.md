# Task261 Directional Truth Chain Execution Expectation SOW

## Business Objectives And Boundaries

Expose a read-only truth-chain field that explains whether the latest directional decision should have produced order, fill, or position lifecycle evidence.

This task is limited to OKX + BTC-USDT-SWAP derivatives live runtime truth reporting. It does not tune strategy logic, change risk gates, change execution behavior, add symbols, add venues, or promote any benchmark.

## Module Responsibilities And Domain Model

- `scripts/runtime_truth_report.py` owns sanitized runtime evidence projection.
- `portfolio_allocation_decisions` is the decision source.
- `decision_audit_records` provides provenance reference counts.
- `execution_orders`, `order_states`, `execution_fills`, and `fill_events` provide order/fill evidence counts.
- Directional `hold_current` with zero composed delta and zero execution legs should be reported as no order/fill/lifecycle transition expected.

## Input / Output Interfaces

Input:
- Latest allocation decision payload.
- Latest decision audit refs.
- Latest decision order/state/fill counts.

Output:
- `database_truth.latest_decision.execution_truth_chain`.
- `runtime.live_runtime_facts.latest_decision_execution_truth_status`.
- Expected order/fill/lifecycle booleans and smallest missing field if execution evidence is expected but absent.

## Database Schema / Tables / Indexes / Constraints

No schema change. Read-only queries continue to use existing tables and existing count probes.

## Transactions, Consistency, Concurrency

The report remains read-only. It reads a point-in-time best-effort snapshot and does not write to the database.

## Authorization, Authentication, Data Security

The runtime report continues to run inside the gateway container and does not print database URLs, tokens, passwords, API keys, or raw payloads.

## Error Handling And Idempotency

Missing or partial evidence degrades to `needs_manual_review` or a concrete missing field rather than raising. The script is idempotent because it only reads.

## State Transition And Lifecycle

No runtime state transition is changed. The new lifecycle field only states whether a position lifecycle transition should be expected from the latest decision.

## Caching And Performance

No new database query is added. The logic reuses existing latest decision payload, audit refs, and count results.

## Logging, Monitoring, Auditing

The new truth-chain field improves auditability by separating:
- verified no-order `hold_current` zero-delta decisions
- expected execution surfaces with missing plan/order/fill refs
- unexpected order/fill evidence for hold-current decisions

## Testing Strategy

Focused unit tests cover:
- directional `hold_current` with zero delta and no orders/fills
- expected execution with missing plan/order/fill evidence

## Migration, Rollback, Compatibility

No migration. Rollback is reverting the script/test/doc changes.

## Configuration And Environment Isolation

No configuration or environment variable changes.

## Code Organization And Dependencies

No dependency changes. Helper logic lives beside existing runtime truth summarizers.

## Documentation And Operations Manual

Operators should use `execution_truth_chain.status` before treating lack of latest order/fill as a blocker. For directional `hold_current` zero-delta, no order/fill is expected.

## Deployment And Acceptance Criteria

Acceptance criteria:
- Runtime truth report exposes `execution_truth_chain`.
- Directional hold-current zero-delta decisions report `verified_no_order_expected_hold_current_zero_delta`.
- Focused unit tests pass.
- No live order behavior changes.
