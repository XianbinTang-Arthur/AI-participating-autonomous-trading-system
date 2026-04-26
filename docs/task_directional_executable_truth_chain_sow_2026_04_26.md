# Directional executable truth chain runtime surface

## Business Objectives and Boundaries

Expose the most recent executable directional decision in runtime truth so latest `hold_current` decisions do not hide the last decision/order/fill provenance candidate. This is read-only observability for OKX + BTC-USDT-SWAP.

## Module Responsibilities and Domain Model

`scripts/runtime_truth_report.py` owns the automation truth projection. It now reports both the latest allocation decision and the latest directional executable candidate. Strategy, risk, execution, and provider modules remain unchanged.

## Input/Output Interfaces

Input remains the existing in-container database probe. Output adds `database_truth.latest_executable_directional_decision` and flattened `runtime.live_runtime_facts.latest_executable_directional_*` fields.

## Database Schema / Tables / Indexes / Constraints

No schema changes. The probe reads `portfolio_allocation_decisions`, `decision_audit_records`, `execution_orders`, `order_states`, `execution_fills`, and `fill_events`.

## Transactions, Consistency, Concurrency

The report uses read-only SQL in one connection and does not mutate state. It accepts normal live-table race conditions between decisions and fills.

## Authorization, Authentication, Data Security

The probe still runs inside the project runtime and does not print credentials or connection strings.

## Error Handling and Idempotency

If no executable directional candidate exists, the new field is `null`. Existing report generation remains idempotent.

## State Transition and Lifecycle

No trading lifecycle changes. The new surface only classifies whether the latest executable directional candidate has execution evidence or a smallest missing field.

## Caching and Performance

The query reads the latest matching directional decision with `limit 1`; expected overhead is negligible compared with the existing runtime report.

## Logging, Monitoring, Auditing

Automation and PM loops can now distinguish latest no-op `hold_current` from the most recent executable directional candidate.

## Testing Strategy

Unit tests cover DB probe sanitization and flattened live runtime facts for the latest executable directional decision.

## Migration, Rollback, Compatibility

Backward compatible additive JSON fields only. Rollback is reverting the commit and redeploying.

## Configuration and Environment Isolation

No config or environment changes.

## Code Organization and Dependencies

No new dependencies.

## Documentation and Operations Manual

This SOW is the operator-facing scope record.

## Deployment and Acceptance Criteria

Deploy only after focused tests and required validation pass. Acceptance: runtime truth remains clean and exposes executable directional provenance fields without changing live order behavior.
