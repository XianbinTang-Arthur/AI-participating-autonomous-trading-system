# Microstructure Runtime Growth Truth Surface SOW

## Bounded Task

- task_type: execution-science-implementation
- input: existing runtime truth report, RDP microstructure probe, collector container state, and automation backlog item `microstructure_runtime_growth_truth_surface_2026_04_30T15_07_19Z`
- output: read-only runtime truth fields that answer whether the microstructure daemon is alive, the heartbeat is fresh, bronze tables are growing, the payload table is growing, the 15m silver workflow is recently done, and silver market tables are fresh
- impact_scope: `scripts/runtime_truth_report.py`, focused unit tests, and automation evidence only
- non_goals: no strategy, risk, execution, provider, schema, symbol, venue, release, promotion, tuning, or live order behavior changes

## Acceptance Criteria

- `runtime_truth_report.py` exposes `microstructure_runtime_growth_truth`.
- `project_live_runtime_facts` exposes flattened fields for collector, heartbeat, bronze growth, payload sequence, workflow, and silver update evidence.
- Raw orderbook or market payload contents are not emitted.
- Focused unit tests cover verified growth, stale heartbeat blocking, and live fact flattening.
- Runtime truth generation remains read-only and uses the existing container/runtime entrypoints.

## Validation

- Focused test: `.venv\Scripts\python.exe -m pytest tests\unit\scripts\test_runtime_truth_report.py -k "microstructure_runtime_growth" -q`
- Script lint: `.venv\Scripts\python.exe -m ruff check scripts\runtime_truth_report.py tests\unit\scripts\test_runtime_truth_report.py --fix`
- Required repo checks: `.venv\Scripts\python.exe -m ruff check aats/ --fix` and `.venv\Scripts\python.exe -m pytest tests\unit\ -x -q`
- Runtime evidence: generate a new runtime truth artifact and inspect only non-secret status fields.

## Rollback

- Revert the commit that adds this truth surface and doc.
- Because this is read-only reporting, rollback does not require market, execution, risk, provider, or database migration changes.
