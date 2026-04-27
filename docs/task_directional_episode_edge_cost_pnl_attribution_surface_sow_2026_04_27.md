# Directional Episode Edge/Cost/PnL Attribution Surface SOW

## Objective

Add a read-only runtime truth surface for recent `directional` decisions on `BTC-USDT-SWAP` so operators can inspect edge, modeled cost, realized fee/slippage, guard reasons, order state, fill evidence, and realized PnL outcome without manual SQL.

## Scope

- Safe-readonly runtime truth reporting only.
- No strategy, risk, execution, provider, symbol, venue, schema, release, promotion, or tuning behavior changes.
- Data source remains the existing gateway container environment and live database probe path; no credentials are printed or embedded.

## Acceptance Criteria

- Runtime truth report includes a sanitized `directional_episode_attribution_truth` object.
- The object covers recent directional decisions and exposes expected edge/cost, order/fill counts, realized fee/slippage proxy, fill outcome PnL evidence, and guard reason codes.
- `runtime.live_runtime_facts` projects the attribution status and smallest missing field.
- Unit tests cover a verified attribution case and runtime fact projection.

## Rollback

Revert the commit that changes `scripts/runtime_truth_report.py`, `tests/unit/scripts/test_runtime_truth_report.py`, and this SOW document.
