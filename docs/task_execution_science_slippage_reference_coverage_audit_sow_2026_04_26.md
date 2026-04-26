# Slippage Reference Coverage Audit SOW - 2026-04-26

## Business Objectives And Boundaries

Objective: quantify why some live fills still lack a slippage reference price after command payload reference prices were enabled in runtime truth.

Boundary: this is a read-only execution-science truth-surface task. It does not change strategy logic, risk gates, execution behavior, AI provider behavior, schema, symbol, venue, strategy family, release/promotion/tuning, or live order behavior.

## Module Responsibilities And Domain Model

- `execution_fills` supplies realized fill and fee facts.
- `execution_orders` supplies order creation path, strategy family, order type, state, and any order/raw payload reference prices.
- `execution_commands` supplies submit-command payload evidence for current command-flow orders.
- `scripts/runtime_truth_report.py` must expose aggregate reference-price coverage without returning order ids or raw payload bodies.

## Input/Output Interfaces

Input:
- live DB aggregates over `execution_fills`, `execution_orders`, and `execution_commands`.
- existing runtime truth slippage/cost calibration output.

Output:
- slippage proxy coverage matrix by order path and command presence.
- classification of the remaining missing reference-price coverage.
- automation state update with current counts and next bounded task.

## Database Schema / Tables / Indexes / Constraints

Read-only tables:
- `execution_fills`
- `execution_orders`
- `execution_commands`

No schema, index, or constraint changes.

## Transactions, Consistency, Concurrency

All database access is read-only aggregate querying. No transactions write state. Runtime truth may observe a point-in-time snapshot while live trading continues, so counts can increase between runs.

## Authorization, Authentication, Data Security

Use existing runtime/container environment to connect. Do not print credentials, tokens, complete connection strings, raw payload bodies, client order ids, venue order ids, or order ids.

## Error Handling And Idempotency

If live DB probing fails, runtime truth must continue to report unavailable evidence rather than fabricate coverage. The coverage audit is idempotent because it only reads aggregate state.

## State Transition And Lifecycle

No order state or lifecycle transition is changed. Historical missing coverage is classified only as evidence status.

## Caching And Performance

The added aggregate query is scoped to `BTC-USDT-SWAP` fills and joins by indexed `order_id`. It is acceptable for PM/runtime smoke cadence and not used in the hot execution path.

## Logging, Monitoring, Auditing

Runtime truth becomes the audit surface:
- covered fills from command payload reference prices
- missing fills without submit commands
- order path matrix for missing and covered fill groups

## Testing Strategy

- Focused unit test for summary classification.
- Existing runtime truth unit tests remain authoritative for status degradation and live facts projection.
- Post-change runtime truth smoke verifies live aggregate counts.

## Migration, Rollback, Compatibility

No migration required. Rollback by reverting the commit and redeploying with `bash scripts/deploy.sh --skip-commit`.

## Configuration And Environment Isolation

No configuration changes. Scope remains OKX + BTC-USDT-SWAP with active live family directional and shadow benchmark `none_verified`.

## Code Organization And Dependencies

Only `scripts/runtime_truth_report.py` and its unit tests should change. No new dependencies.

## Documentation And Operations Manual

This SOW documents the evidence interpretation:
- covered current command-flow fills use `execution_commands.command_payload.intent.reference_price`
- missing historical fills have no submit command and therefore no command payload reference price

## Deployment And Acceptance Criteria

Acceptance:
- runtime truth remains `ok=true` with no blocking findings.
- coverage matrix shows missing reference-price fills grouped by creation path.
- current command-flow covered fills are separated from historical/no-submit-command missing fills.
- focused tests pass.
