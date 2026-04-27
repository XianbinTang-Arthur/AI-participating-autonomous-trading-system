# Directional Order Intent Projection Gap SOW - 2026-04-27

## Business Objectives and Boundaries
- Objective: make runtime truth identify the exact gap when a directional decision has execution plan and order intent references but no persisted execution order, order state, command, or fill surface.
- Boundary: read-only reporting only. No strategy, risk, execution, provider, schema, symbol, venue, or live order behavior changes.

## Module Responsibilities and Domain Model
- `scripts/runtime_truth_report.py` owns operator-facing runtime truth projection.
- `decision_audit_records.execution_plan_refs` and `order_intent_refs` prove the allocator produced an executable plan.
- `execution_orders`, `order_states`, and `execution_commands` prove the plan entered the execution surface.
- `execution_fills` and `fill_events` prove post-submission fill evidence.

## Input/Output Interfaces
- Input: existing read-only runtime truth database probe fields.
- Output: `execution_truth_chain` reports the smallest missing field as an order-intent projection gap before fill evidence is expected.

## Database Schema / Tables / Indexes / Constraints
- Uses existing tables only: `portfolio_allocation_decisions`, `decision_audit_records`, `execution_orders`, `order_states`, `execution_commands`, `execution_fills`, `fill_events`.
- No migration, index, or schema change.

## Transactions, Consistency, Concurrency
- Runtime truth is a point-in-time read-only projection and tolerates concurrent live writes.
- The classification must not infer orders or fills from intent records.

## Authorization, Authentication, Data Security
- No credentials, connection strings, tokens, or secrets are printed.
- Report output remains aggregate counts and non-sensitive identifiers.

## Error Handling and Idempotency
- Missing optional counts continue to degrade to zero.
- The reporting function remains deterministic for the same probe payload.

## State Transition and Lifecycle
- If plan/order intent references exist but no order, order state, command, or fill surface exists, classify the gap before fill ingestion.
- Fill evidence is not expected until an execution order, submitted state, command, or fill surface exists.

## Caching and Performance
- No new database query is required.
- Classification uses existing counts already collected by the runtime truth probe.

## Logging, Monitoring, Auditing
- `submission_gap_root_cause` becomes `execution_order_missing_for_order_intent`.
- `smallest_missing_field` becomes `execution_order_or_order_state_from_order_intent_refs`.

## Testing Strategy
- Add a unit test for the exact plan/order-intent-without-order-surface case.
- Preserve existing classifications for complete execution, created-without-submit, terminal-no-fill, and no-order decisions.

## Migration, Rollback, Compatibility
- Rollback: revert this reporting-only patch.
- Existing top-level fields remain backward compatible.

## Configuration and Environment Isolation
- No configuration changes.
- No environment file reads or credential output.

## Code Organization and Dependencies
- Change is limited to `scripts/runtime_truth_report.py`, its unit tests, and this SOW.
- No new dependency.

## Documentation and Operations Manual
- Operators should treat this as a pre-order projection gap: the decision intended execution, but no execution order/order state/command/fill surface is available.
- It must not be interpreted as a fill collection failure.

## Deployment and Acceptance Criteria
- Acceptance criteria:
  1. The focused runtime truth unit test passes.
  2. Runtime truth no longer labels this case as `fill_event_refs_or_execution_fills`.
  3. `submission_gap_root_cause` identifies `execution_order_missing_for_order_intent`.
  4. No live order behavior is changed.

