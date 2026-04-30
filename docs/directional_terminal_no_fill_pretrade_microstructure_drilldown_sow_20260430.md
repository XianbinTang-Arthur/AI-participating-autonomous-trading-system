# Directional Terminal No-Fill Pretrade Microstructure Drilldown SOW

## Business Objectives And Boundaries

Expose a read-only execution-science truth surface for the latest executable directional terminal no-fill episode on OKX BTC-USDT-SWAP. The goal is to connect the already verified decision/order/order_state/no-fill chain to decision-time microstructure, snapshot/diff sequence validation, local fill-feasibility classification, and slippage baseline context.

This task must not change strategy logic, risk gates, execution behavior, provider behavior, symbols, venues, schema, release/promotion/tuning, or timeframe plumbing.

## Module Responsibilities And Domain Model

`scripts/runtime_truth_report.py` remains the single reporting surface for this bounded task. It reads existing sanitized runtime facts and returns a derived truth object:

- latest executable directional decision
- terminal no-fill order-state drilldown
- RDP silver orderbook/trade-flow context at decision time
- current orderbook payload sequence and depth readiness
- slippage/cost calibration baseline

## Input/Output Interfaces

Inputs are existing runtime truth probes: live DB aggregates inside the running container and RDP microstructure tables. Outputs are JSON fields under `directional_executable_terminal_no_fill_pretrade_microstructure_truth` and projected live facts.

No raw orderbook payloads, credentials, connection strings, or request bodies are emitted.

## Database Schema / Tables / Indexes / Constraints

No schema changes. The report reads existing tables only.

Relevant sources:

- `portfolio_allocation_decisions`
- `execution_orders`
- `order_states`
- `execution_commands`
- `execution_fills`
- `silver.market_orderbook_metrics_15m`
- `silver.market_trade_flow_15m`
- `bronze.market_orderbook_payloads`

## Transactions, Consistency, Concurrency

No writes and no transactions are introduced. The report is a point-in-time diagnostic snapshot and does not coordinate live execution.

## Authorization, Authentication, Data Security

Runtime DB access stays inside the existing container environment. The report must not print secrets, raw payloads, tokens, passwords, or full connection strings.

## Error Handling And Idempotency

Missing evidence is represented as `status` plus `smallest_missing_field`. Re-running the report is idempotent and only regenerates JSON output.

## State Transition And Lifecycle

This task does not mutate order state, position state, fill outcome state, Redis, or exchange state.

## Caching And Performance

The RDP silver window is widened enough to cover the latest executable terminal no-fill episode while staying bounded. This is read-only and limited to compact silver rows, not raw payload bodies.

## Logging, Monitoring, Auditing

The output becomes part of the runtime truth chain and automation artifacts. It should distinguish:

- decision-time microstructure context
- current snapshot/diff sequence readiness
- local terminal no-fill before exchange ack
- slippage baseline availability

## Testing Strategy

Focused unit tests cover:

- verified executable terminal no-fill pretrade microstructure drilldown
- projected live facts for status, decision id, sequence, depth, fill-feasibility, and slippage baseline

Runtime validation generates a fresh runtime truth report and checks the new fields against live evidence.

## Migration, Rollback, Compatibility

No migration. Rollback is `git revert` of the reporting commit and redeploy through `scripts/deploy.sh --profile derivatives-live --skip-commit`.

Existing runtime truth fields remain backward compatible.

## Configuration And Environment Isolation

The optional `AATS_EXECUTION_SCIENCE_RECENT_SILVER_BARS` controls the bounded RDP silver window. Defaults remain local to the report probe.

## Code Organization And Dependencies

No new dependency is introduced. The implementation reuses existing runtime truth helpers and microstructure context functions.

## Documentation And Operations Manual

Operators should treat this as diagnostic evidence only. It is not alpha/profitability proof and must not be used to bypass risk, kill switch, execution gates, or release gates.

## Deployment And Acceptance Criteria

Acceptance criteria:

- Runtime truth contains `directional_executable_terminal_no_fill_pretrade_microstructure_truth`.
- The surface links the latest executable terminal no-fill decision to decision-time silver orderbook/trade-flow context when available.
- Snapshot/diff sequence and orderbook depth readiness are included without raw payload exposure.
- Local fill feasibility is explicitly classified as terminal no-fill before exchange ack when applicable.
- Focused tests, lint, full unit tests, deploy, and post-deploy runtime truth pass.
