# Directional Order Submission Gap SOW - 2026-04-26

## Business Objectives and Boundaries
- Objective: make the runtime truth report identify the root cause of executable directional orders that remain `CREATED` with no submit command or venue-submitted state.
- Boundary: read-only runtime truth and automation state only. This task does not enable command flow, submit orders, relax risk gates, edit strategy logic, or mutate live database rows.

## Module Responsibilities and Domain Model
- `scripts/runtime_truth_report.py` owns no-secret operator runtime truth.
- `execution_orders` / `order_states` represent local order lifecycle truth.
- `execution_commands` represents command-flow submission truth.
- The current live gap is pre-fill: the order was locally created but has no submission command and no venue order id.

## Input/Output Interfaces
- Input: existing gateway-container environment flags and scoped aggregate SQL for the latest executable directional decision.
- Output: an explicit `submission_gap_root_cause` and actionable smallest missing field in runtime truth.

## Database Schema / Tables / Indexes / Constraints
- Existing tables only: `portfolio_allocation_decisions`, `decision_audit_records`, `execution_orders`, `order_states`, `execution_commands`, `execution_fills`, `fill_events`.
- No schema, index, or migration changes.

## Transactions, Consistency, Concurrency
- Read-only point-in-time queries only.
- No transaction writes, no order resubmission, no state correction.

## Authorization, Authentication, Data Security
- The report reads non-secret boolean runtime flags from the container environment.
- It never prints database URLs, passwords, tokens, or full connection strings.

## Error Handling and Idempotency
- Missing environment flags are treated as default false for the command-flow flag, matching application settings.
- Unknown optional flags degrade to null-safe runtime truth fields.

## State Transition and Lifecycle
- `CREATED` / `SUBMITTING` orders without command or venue state are classified as `expected_order_submission_missing`.
- When command-flow is disabled, the root cause is surfaced as `execution_command_flow_disabled_direct_submit_interruption_window`.

## Caching and Performance
- Adds no cache writes.
- Uses bounded counts scoped to a single decision id.

## Logging, Monitoring, Auditing
- Adds explicit runtime truth evidence so PM Loop can stop chasing fill ingestion for a pre-submit gap.
- Next runtime-affecting task should decide whether to enable command-flow and/or recover existing stranded `CREATED` orders.

## Testing Strategy
- Unit tests cover direct-submit disabled command-flow root cause.
- Existing tests cover command-enabled missing command and fill-via-order truth.

## Migration, Rollback, Compatibility
- Rollback: revert the report-only commit and redeploy.
- Compatibility: existing `status` stays `expected_order_submission_missing`; root-cause fields are additive.

## Configuration and Environment Isolation
- No configuration changes in this task.
- Command-flow enablement remains a separate runtime-affecting decision.

## Code Organization and Dependencies
- Limited to `scripts/runtime_truth_report.py`, unit tests, and this SOW.
- No new dependencies.

## Documentation and Operations Manual
- Operators should interpret `enable_execution_command_flow_or_recover_created_order` as: command-flow is disabled and an existing locally created order is stranded before submission truth.
- Do not treat this as fill collection failure.

## Deployment and Acceptance Criteria
- Acceptance criteria:
  1. Runtime truth remains `ok=true` with no git/deploy blockers.
  2. Latest executable directional gap exposes `submission_gap_root_cause=execution_command_flow_disabled_direct_submit_interruption_window`.
  3. Focused and required unit tests pass before commit/deploy.
