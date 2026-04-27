# Target Convergence Guard Truth Surface SOW - 2026-04-27

## Business Objectives and Boundaries
- Objective: make the runtime truth report prove whether the deployed directional target convergence guard has triggered, or why it has not.
- Boundary: read-only reporting only. No strategy, risk, execution, provider, symbol, venue, schema, release, promotion, or live order behavior changes.
- Scope: OKX BTC-USDT-SWAP derivatives live carrier `directional`.

## Module Responsibilities and Domain Model
- `scripts/runtime_truth_report.py` owns no-secret runtime evidence aggregation.
- The target convergence guard flag is `target_convergence_open_orders_block_exposure_increase`.
- A guard trigger means a directional decision payload contains that flag after an exposure-increasing target is evaluated while current open orders exist.

## Input / Output Interfaces
- Input: existing gateway container environment and database tables through the existing runtime truth probe path.
- Output: aggregate JSON under `database_truth.target_convergence_guard`, summarized under `target_convergence_guard_truth`, then projected into `runtime.live_runtime_facts`.
- No raw decision payloads, credentials, or connection strings are emitted.

## Database Schema / Tables / Indexes / Constraints
- Read-only tables: `portfolio_allocation_decisions`, `execution_orders`, `order_states`.
- No schema changes, migrations, new indexes, or constraints.
- Queries use existing indexed fields where possible: `symbol`, `primary_family`, `created_at`, `state`, `status`.

## Transactions, Consistency, Concurrency
- The report is a point-in-time read-only snapshot.
- It deliberately separates guard-hit counts from current open-order counts because they can change independently during live execution.
- No locks or writes are introduced.

## Authorization, Authentication, Data Security
- The probe runs inside the existing gateway container and uses its implicit environment.
- The report redaction layer remains in place.
- It must never print database URLs, passwords, tokens, API keys, or full connection strings.

## Error Handling and Idempotency
- Missing database truth reports `missing_database_truth`.
- Missing guard probe reports `missing_target_convergence_guard_probe`.
- Re-running the report is idempotent and has no side effects.

## State Transition and Lifecycle
- No order, decision, fill, or position lifecycle state is modified.
- Status classifications are reporting-only:
  - `verified_guard_triggered`
  - `deployed_no_trigger_no_recent_decisions_no_open_orders`
  - `deployed_no_trigger_no_current_open_orders`
  - `deployed_no_trigger_no_recent_decisions`
  - `pending_open_orders_no_guard_hit`
  - `deployment_mismatch_guard_truth_not_authoritative`

## Caching and Performance
- Counts are aggregate-only and bounded by symbol/family filters.
- No payload bodies are returned; the payload text scan is used only to count guard-flag occurrences.

## Logging, Monitoring, Auditing
- The audit surface is the runtime truth JSON and `runtime.live_runtime_facts`.
- This lets automation distinguish "guard deployed but no trigger condition" from "guard missing".

## Testing Strategy
- Unit tests verify:
  - DB probe contains the guard flag and open-order terminal status filters.
  - The summary reports exact no-trigger reason when there are no recent decisions and no open orders.
  - The summary reports `verified_guard_triggered` when a guard hit exists.
  - Live fact projection includes the guard truth fields.

## Migration, Rollback, Compatibility
- No migration is required.
- Rollback is a normal code revert of the reporting changes.
- Existing report consumers remain compatible because only additive fields are introduced.

## Configuration and Environment Isolation
- No new configuration is introduced.
- The symbol continues to use `AATS_EXECUTION_SCIENCE_SYMBOL` with `BTC-USDT-SWAP` fallback.

## Code Organization and Dependencies
- No new dependency is added.
- Implementation stays in `scripts/runtime_truth_report.py` and its unit tests.

## Documentation and Operations Manual
- Operators can inspect `target_convergence_guard_truth.status`:
  - `verified_guard_triggered`: guard has blocked at least one exposure-increase target.
  - `deployed_no_trigger_*`: no current trigger condition was visible; keep monitoring.
  - `pending_open_orders_no_guard_hit`: investigate whether current open-order truth is reaching decision context.

## Deployment and Acceptance Criteria
- Acceptance criteria:
  - Runtime truth report emits `target_convergence_guard_truth`.
  - Live facts expose guard status, guard-hit counts, current open-order count, and latest guard-hit decision id.
  - Focused and full unit tests pass.
  - Deployment, if performed, uses only `scripts/deploy.sh`.
