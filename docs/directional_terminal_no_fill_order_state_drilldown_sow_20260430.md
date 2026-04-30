# Directional Terminal No-Fill Order-State Drilldown SOW

## Business Objectives and Boundaries

Expose the latest executable `directional` BTC-USDT-SWAP terminal no-fill episode as a per-order truth chain. The output must help an operator inspect why an executable decision produced terminal order surfaces but no fill, without changing strategy, risk, execution, provider behavior, symbols, venues, schemas, tuning, or live order behavior.

## Module Responsibilities and Domain Model

`scripts/runtime_truth_report.py` remains the only module changed. It reads existing live database evidence through the current runtime truth entrypoint and reports sanitized fields for:

- decision and allocation identity
- order intent identity
- execution command state
- execution order terminal state
- order_state terminal status
- source system, execution style, position intent
- exchange acknowledgement presence or absence
- fill and legacy fill-event absence

## Input and Output Interfaces

Input is the existing env-loaded runtime truth database probe. Output is a new read-only `terminal_no_fill_drilldown` object under `directional_executable_episode_truth`, plus projected live runtime fact fields for automation checks.

## Database Schema / Tables / Indexes / Constraints

The implementation reads existing `portfolio_allocation_decisions`, `decision_audit_records`, `execution_orders`, `order_states`, `execution_commands`, `execution_fills`, and `fill_events`. It adds no tables, indexes, constraints, or migrations.

## Transactions, Consistency, Concurrency

The report uses read-only SELECTs. It does not mutate order state, Redis, or JSON payloads. Evidence is a point-in-time snapshot and is interpreted as diagnostic truth, not a recovery action.

## Authorization, Authentication, Data Security

No secrets or env contents are printed. Raw order payloads and command payload bodies are not exposed. Only bounded identifiers, states, counts, booleans, and classification fields are reported.

## Error Handling and Idempotency

Missing order drilldown evidence is surfaced as `smallest_missing_field` rather than silently treated as success. Running the report repeatedly is idempotent.

## State Transition and Lifecycle

The feature only classifies existing terminal order/order_state lifecycle evidence. It does not alter OrderState persistence in Postgres, JSON payloads, or Redis.

## Caching and Performance

The drilldown is limited to a small latest executable decision and at most eight terminal rows. It uses existing indexed decision/order/client-order relationships and does not add background jobs.

## Logging, Monitoring, Auditing

The runtime truth JSON and automation artifacts provide the audit trail. The new fields are intended for PM Loop and operator inspection.

## Testing Strategy

Add unit coverage for terminal no-fill drilldown classification and projected live runtime facts. Run focused tests for `scripts/runtime_truth_report.py`, ruff, and the full unit suite before deployment.

## Migration, Rollback, Compatibility

No migration is required. Rollback is a normal git revert and redeploy. Existing runtime truth fields remain backward compatible.

## Configuration and Environment Isolation

No new configuration or env variables are added. The fixed live scope remains OKX BTC-USDT-SWAP with `directional` as live carrier and `none_verified` as shadow benchmark.

## Code Organization and Dependencies

The change stays inside the runtime truth script and its unit tests. No dependencies are added.

## Documentation and Operations Manual

Operators should inspect `directional_executable_episode_truth.terminal_no_fill_drilldown.per_order` when the latest executable directional decision has `fill_expected=false` because all visible order surfaces are terminal before fill.

## Deployment and Acceptance Criteria

Acceptance requires:

1. Runtime truth exposes per-order terminal no-fill drilldown for the latest executable directional decision.
2. Drilldown rows link decision, allocation, order intent, execution command, execution order, order_state, exchange ack presence, and fill absence.
3. Existing aggregate terminal no-fill fields remain unchanged.
4. Focused and full unit validations pass.
5. Safe-readonly deployment succeeds through `scripts/deploy.sh --profile derivatives-live --skip-commit`.
