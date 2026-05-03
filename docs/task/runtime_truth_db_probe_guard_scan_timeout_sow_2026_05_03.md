# Runtime Truth DB Probe Guard Scan Timeout SOW

## Bounded Task

- task_type: runtime-reliability-fix
- input: `scripts/runtime_truth_report.py` DB probe times out after 75 seconds while the database is reachable.
- output: runtime truth DB probe avoids repeated full-table JSON text scans for guard coverage.
- impact scope: read-only automation truth reporting only; no strategy, risk, execution, provider, symbol, venue, schema, or live order behavior changes.
- validation: focused unit tests plus a live runtime truth report using the existing gateway/container environment without printing credentials.
- rollback: revert the script/test/doc changes from this task.

## Acceptance Criteria

- PASS if `scripts/runtime_truth_report.py --pretty` returns database truth before `DATABASE_TRUTH_PROBE_TIMEOUT_SECONDS` and no longer reports `database_truth_unavailable` from the guard scan timeout.
- PASS if the unit test covering the DB probe guard scan SQL passes.
- FAIL if the probe still uses repeated full-history `portfolio_allocation_decisions.payload::text` guard scans or exceeds the existing 75 second timeout.
