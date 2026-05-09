# Operator API Retry Limit Lookup Cluster SOW - 2026-05-09

## Bounded Task

- task_type: runtime-reliability-fix
- input: `runtime_reliability_fix_operator_api_retry_limit_lookup_cluster_2026_05_09T08_27_12Z`, current heartbeat runtime truth, focused failing `operator_api` integration test.
- output: clear the remaining retry-limit lookup startup-snapshot anchor-state `operator_api` gate failure.
- impact scope: retrying unresolved serial exit execution parents after `resume_limit_lookup_failed`; no strategy, venue, symbol, risk, execution gate, kill switch, or runtime timeframe behavior changes.
- validation: focused WSL2 retry-limit lookup regression, full WSL2 `tests/integration/test_operator_api.py`, Windows lint, Windows unit suite.
- rollback: revert the code change and this SOW document.
- binary acceptance criteria: focused retry-limit lookup regression returns HTTP 200, dispatches the remaining split quantities, clears the parent blocker, and full `operator_api` no longer rejects on this cluster.

## Domain Model

The parent exit execution intent is the aggregate lifecycle record for serial risk-reducing exit orders. A `resume_limit_lookup_failed` issue is stored in `parent.metadata.resume_issue` when the exchange max-size lookup is unavailable. The operator `retry_limit_lookup` action should clear that issue only after a fresh max-size lookup succeeds, then continue serial split dispatch using the parent aggregate state.

## Failure Chain

The failing path reproduced as `502 exchange_operation_failed: serial_exit_split_missing_anchor_state`. Probe evidence showed:

- `retry_exit_execution_limit_lookup()` found the startup snapshot parent correctly.
- `_resume_exit_execution_parent()` obtained a valid max-size limit.
- Clearing `resume_issue` through a plain parent save used the same aggregate version.
- `ExitExecutionWriter` treated the incoming parent as stale and preserved the previous `resume_issue`, turning the parent back into `REVIEW_REQUIRED`.
- `_execute_serial_exit_split()` then observed a review-required parent before dispatching the next slice and tried to return a missing base anchor state.

## Change Plan

Use `ExitExecutionWriter.recompute_parent()` when clearing the resume issue for retry-limit lookup. This advances the aggregate version while recomputing from child refs, so sticky stale protection remains intact and the parent re-enters `PARTIALLY_FILLED` with remaining dispatchable quantity before serial split dispatch.

## Security And Safety

No credentials, secrets, live exchange commands, deployment, or database schema changes are involved. The change preserves writer sticky semantics and does not bypass operator review: if child refs still require review, recomputation will keep `operator_review_required`.

## Actual Result

- files: `aats/services/execution_engine/order_manager.py`, `docs/operator_api_retry_limit_lookup_cluster_sow_2026_05_09.md`
- tests:
  - focused WSL2 retry-limit lookup regression: passed
  - full WSL2 `tests/integration/test_operator_api.py`: passed
  - Windows lint: passed
  - Windows unit suite: passed
- commit: validation passed; final commit hash is recorded in the PM-loop artifact after commit.
- deployment: pending post-commit runtime truth and deploy-gate reevaluation.
