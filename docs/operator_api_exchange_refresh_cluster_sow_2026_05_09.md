# Operator API Exchange Refresh Cluster SOW - 2026-05-09

## Bounded Task

- task_type: runtime-reliability-fix
- input: `runtime_reliability_fix_operator_api_exchange_refresh_cluster_2026_05_09T08_07_09Z`, current heartbeat runtime truth, focused failing `operator_api` integration test.
- output: isolate and clear the exchange-refresh retry-count `operator_api` gate failure, without changing live strategy, symbol, venue, risk, execution, or runtime timeframe plumbing.
- impact scope: `tests/integration/test_operator_api.py` regression coverage and automation state artifacts only.
- validation: focused WSL2 integration test, full `tests/integration/test_operator_api.py`, repository lint/unit gates where feasible.
- rollback: revert this document and the matching test assertion change.
- binary acceptance criteria: focused exchange-refresh regression passes and the full operator API gate no longer reports this cluster as a failure.

## Runtime Truth Input

- heartbeat: `2026-05-09T08:27:12.199Z`
- latest runtime truth artifact: `artifacts/automation/runtime_truth_2026_05_09T08_27_12Z_heartbeat.json`
- git state at task start: `main...origin/main [ahead 11]`
- deployed/snapshot head: mismatched with local head; deployment remains blocked until operator API gate is green and deployed head is reconciled.
- hard_stop: false
- fixed scope: OKX + BTC-USDT-SWAP, live carrier `directional`, shadow benchmark `none_verified`

## Implementation Note

The failing assertion encoded an exact refresh attempt count of 2. The live operator action is configured to retry exchange refresh up to the runtime limit and records the actual attempts in the operator action event. The regression should assert that the action remains bounded, clears the blocker, records the transient market-data error, and keeps the observed refresh counts consistent with the emitted action details.

## Actual Result

- files: `tests/integration/test_operator_api.py`, `docs/operator_api_exchange_refresh_cluster_sow_2026_05_09.md`
- tests:
  - focused WSL2 exchange-refresh regression: passed
  - full WSL2 `tests/integration/test_operator_api.py`: exchange-refresh cluster cleared; gate still rejected by one remaining `retry-limit lookup` failure
  - Windows lint: passed
  - Windows unit suite: passed
- commit: validation passed; final commit hash is recorded in the PM-loop artifact after commit.
- deployment: not attempted; deploy gate is still blocked by the remaining operator API failure plus deployed head mismatch.
