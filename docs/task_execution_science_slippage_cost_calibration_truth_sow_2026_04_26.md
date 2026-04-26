# Execution Science Slippage Cost Calibration Truth SOW - 2026-04-26

## Business objectives and boundaries
- Objective: expose read-only slippage/cost calibration evidence for directional BTC-USDT-SWAP runtime truth.
- Boundary: no strategy, risk, provider, schema, venue, symbol, release, promotion, tuning, timeframe, or live order behavior changes.

## Module responsibilities and domain model
- `scripts/runtime_truth_report.py` owns the no-secret automation truth projection.
- Live `execution_fills` and `execution_orders` provide realized fee and order/fill price evidence.
- RDP silver orderbook and trade-flow bars provide market microstructure context for calibration quality.

## Input/output interfaces
- Input: gateway container environment-loaded live DB URL and RDP DB URL, queried inside the running container without printing credentials.
- Output: runtime truth gains `slippage_cost_calibration_truth` plus live facts for status and smallest missing field.

## Database schema / tables / indexes / constraints
- No schema, table, index, or constraint changes.
- The live read-only probe touches existing `execution_fills` and `execution_orders`.
- The RDP read-only probe touches existing `silver.market_orderbook_metrics_15m` and `silver.market_trade_flow_15m`.

## Transactions, Consistency, Concurrency
- No write transaction is introduced.
- Probe results are point-in-time aggregates and tolerate concurrent fill/order/market-data writes.

## Authorization, Authentication, Data Security
- Credentials are read only inside the running gateway container process environment.
- The report never emits database URLs, passwords, tokens, raw exchange payloads, client order ids, venue order ids, or fill ids.

## Error Handling and Idempotency
- Missing fill samples, fee samples, reference prices, or silver market context degrade to explicit status and smallest missing field.
- Re-running the report is idempotent for the same runtime state.

## State Transition and Lifecycle
- No live trading state transition is changed.
- Runtime truth only classifies whether realized fee and slippage proxy calibration evidence is available.

## Caching and Performance
- No cache is introduced.
- Queries are bounded aggregates over indexed fill/order timestamps and latest-row silver lookups.

## Logging, Monitoring, Auditing
- Automation can now distinguish verified fee calibration evidence from missing slippage reference evidence.
- Silver trade-flow context is surfaced to support future slippage/cost calibration review.

## Testing Strategy
- Unit tests cover fee/slippage calibration summary, missing reference-price degradation, and live facts projection.
- Runtime smoke verifies the deployed report against live aggregate data.

## Migration, Rollback, Compatibility
- No migration required.
- Rollback is a normal code revert and redeploy through `scripts/deploy.sh --skip-commit`.

## Configuration and Environment Isolation
- No new configuration is required.
- The symbol remains fixed to BTC-USDT-SWAP for the current stage.

## Code Organization and Dependencies
- Changes stay in `scripts/runtime_truth_report.py` and `tests/unit/scripts/test_runtime_truth_report.py`.
- No new dependency.

## Documentation and Operations Manual
- `actual_fee_bps` is derived as `abs(fee_amount) / (fill_qty * fill_price) * 10000`.
- `limit_fill_slippage_bps` is only a proxy and is reported only when an order reference price is available.

## Deployment and Acceptance Criteria
- Acceptance: focused runtime truth tests pass, lint passes, full unit tests pass, deployment via standard script succeeds, and post-deploy runtime truth reports either verified slippage/cost calibration evidence or an exact smallest missing field.
