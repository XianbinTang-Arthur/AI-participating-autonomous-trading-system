# Directional Advisory No-Order Episode Classification SOW

## Business Objectives And Boundaries

Improve the OKX BTC-USDT-SWAP trading microscope by separating recent
directional decisions that explicitly do not expect an order from decisions that
are missing execution-order evidence. This is a read-only runtime truth surface
change. It does not alter strategy selection, risk gates, provider behavior,
schema, execution, symbol, venue, timeframe, recovery, or live order behavior.

## Module Responsibilities And Domain Model

`scripts/runtime_truth_report.py` owns the no-secret runtime truth report. The
directional episode attribution model now includes per-decision
`order_expectation` derived from sanitized allocation route action and observed
order/fill counts.

## Input And Output Interfaces

Input remains the existing DB probe payload under
`directional_episode_attribution.recent_decisions`. Output adds coverage fields
for no-order expected decisions, order-surface-or-no-order completion, and
missing order surface counts.

## Database Schema / Tables / Indexes / Constraints

No database schema, table, index, or constraint changes are required. The report
continues to read existing `portfolio_allocation_decisions`,
`execution_orders`, `execution_fills`, and related lifecycle projections.

## Transactions, Consistency, Concurrency

No writes or transactions are introduced. The report is a point-in-time read and
inherits the existing live DB probe consistency characteristics.

## Authorization, Authentication, Data Security

The report remains no-secret. It does not expose credentials, tokens, full
connection strings, raw env content, or raw orderbook payloads.

## Error Handling And Idempotency

Existing unavailable-DB behavior is preserved. Classification is deterministic:
each recent decision is either order-surface present, no-order expected by route
action, fill-without-order surface, or missing order surface.

## State Transition And Lifecycle

No live trading state transitions are changed. This only changes how advisory
and hold-current no-order states are represented in audit output.

## Caching And Performance

The change operates over the existing 24 recent directional decisions and adds
constant-time per-row classification. No cache behavior changes.

## Logging, Monitoring, Auditing

The runtime truth report gains auditable coverage fields:
`decisions_with_no_order_expected`,
`decisions_with_order_surface_or_no_order_expectation`,
`decisions_requiring_order_surface`,
`decisions_missing_order_surface`, and
`all_recent_decisions_no_order_expected`.

## Testing Strategy

Add focused unit coverage for:
- advisory-only no-order decisions no longer reporting missing execution orders;
- executable directional decisions without an order still reporting an exact
  missing order-surface field.

## Migration, Rollback, Compatibility

No migration is required. Roll back by reverting the commit and redeploying with
`bash scripts/deploy.sh --profile derivatives-live --skip-commit`.

## Configuration And Environment Isolation

No configuration or environment changes are required. The derivatives-live scope
remains OKX BTC-USDT-SWAP with `none_verified` shadow benchmark.

## Code Organization And Dependencies

The implementation is confined to `scripts/runtime_truth_report.py` and its
unit tests. No new runtime dependency is introduced.

## Documentation And Operations Manual

This SOW records the operating intent and acceptance criteria for the PM-loop
bounded task.

## Deployment And Acceptance Criteria

Acceptance criteria:
- Runtime truth reports advisory/hold-current no-order recent directional
  decisions as no-order expected rather than missing execution orders.
- Runtime truth still reports executable directional decisions without orders as
  missing order surface.
- Focused tests and full unit tests pass.
- Deployment, if performed, uses only `scripts/deploy.sh`.
