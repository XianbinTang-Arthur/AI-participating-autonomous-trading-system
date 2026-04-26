# Directional Fill Provenance Gap SOW - 2026-04-26

## Business Objectives and Boundaries
- Objective: close the misleading `fill_event_refs_or_execution_fills` runtime truth gap for the latest executable directional decision.
- Boundary: read-only reporting only. No strategy, risk, execution, provider, schema, symbol, venue, or live order behavior changes.

## Module Responsibilities and Domain Model
- `scripts/runtime_truth_report.py` owns operator-facing runtime truth projection.
- `execution_orders` and `order_states` represent order creation and lifecycle state.
- `execution_commands` represents submission/cancel command intent.
- `execution_fills` and `fill_events` represent observed fills.

## Input/Output Interfaces
- Input: existing gateway-container database environment and read-only SQL queries.
- Output: runtime truth report fields that distinguish created-but-not-submitted orders from missing fill evidence.

## Database Schema / Tables / Indexes / Constraints
- Uses existing tables only: `portfolio_allocation_decisions`, `decision_audit_records`, `execution_orders`, `order_states`, `execution_commands`, `execution_fills`, `fill_events`.
- No migration, index, or schema change.

## Transactions, Consistency, Concurrency
- Queries are read-only and tolerate concurrent live writes.
- Counts are point-in-time evidence, not a transactional execution replay.

## Authorization, Authentication, Data Security
- The probe runs inside the gateway container and uses existing environment variables without printing connection strings or secrets.
- Returned facts are aggregate counts and non-sensitive identifiers only.

## Error Handling and Idempotency
- Existing probe failure handling is preserved.
- Missing optional counts degrade to zero in summary code.

## State Transition and Lifecycle
- A decision with plan/order surfaces but only `CREATED` or `SUBMITTING` order states and no submit command is classified as `expected_order_submission_missing`.
- Fill evidence is not treated as currently expected until submit/venue/fill surface exists.

## Caching and Performance
- Adds bounded aggregate queries scoped by `decision_id`.
- No cache writes or long scans are introduced.

## Logging, Monitoring, Auditing
- The runtime truth report now exposes the smallest missing execution-chain field as `execution_command_or_submitted_order_state` for this case.
- This improves PM Loop task selection and avoids mislabeling a submission gap as a fill ingestion gap.

## Testing Strategy
- Unit tests cover created-without-submit classification.
- Unit tests cover fill evidence joined through `execution_orders`.
- Existing runtime truth tests remain authoritative for no-order and complete-surface cases.

## Migration, Rollback, Compatibility
- Rollback: revert this report-only patch.
- Compatibility: existing fields remain; new count fields are additive inside `execution_chain`.

## Configuration and Environment Isolation
- No configuration changes.
- No environment file reads or credential output.

## Code Organization and Dependencies
- Change is limited to `scripts/runtime_truth_report.py` and its unit tests.
- No new dependencies.

## Documentation and Operations Manual
- Operators should interpret `expected_order_submission_missing` as a pre-fill blocker: order intent exists, but submit command or submitted venue state is absent.
- It is distinct from fill collection failure.

## Deployment and Acceptance Criteria
- Acceptance criteria:
  1. Unit tests for runtime truth report pass.
  2. Live runtime truth no longer reports `fill_event_refs_or_execution_fills` for a decision whose only observed surface is `CREATED` order/order_state and no submit command.
  3. Deployment uses the standard WSL2 deploy entrypoint only.
