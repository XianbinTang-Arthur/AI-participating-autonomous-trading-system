# Operator API Profile Read Surface Cluster SOW

## Business Objectives And Boundaries
Clear the current operator API profile/read-surface reliability cluster without changing live strategy behavior, venue, symbol, execution gates, risk controls, or runtime timeframe plumbing. Scope remains OKX `BTC-USDT-SWAP` derivatives live readiness evidence only.

## Module Responsibilities And Domain Model
The affected surface is `tests/integration/test_operator_api.py`. The tests validate operator-facing read models for managed derivatives profile configuration, portfolio position aggregation, and profile-control execution gating.

## Input/Output Interfaces
Inputs are in-memory test runtime configuration and injected `PortfolioSnapshot` objects. Outputs are authenticated operator API responses for `/strategy-profiles` and `/positions`, plus profile-control call evidence.

## Database Schema / Tables / Indexes / Constraints
No schema, migration, table, index, or constraint changes.

## Transactions, Consistency, Concurrency
The position read-surface test must update the same runtime snapshot cache used by the operator query path after saving a manual repository snapshot. Background decision triggers are stopped before mock assertions to avoid concurrent control-loop calls.

## Authorization, Authentication, Data Security
No credentials are read or printed. Existing test-only operator users remain local to the test runtime.

## Error Handling And Idempotency
The change is test-only and idempotent. Repeated runs should produce the same cache-backed operator read response.

## State Transition And Lifecycle
No production lifecycle state changes. Test setup explicitly transitions the in-memory portfolio snapshot cache to the injected snapshot before reading `/positions`.

## Caching And Performance
The change aligns the test with the cache-first operator read path. No production cache behavior is changed.

## Logging, Monitoring, Auditing
Runtime truth, focused validation, and full operator API gate results are recorded under `artifacts/automation/`.

## Testing Strategy
Run ruff, the focused WSL operator API cluster, full `tests/integration/test_operator_api.py`, and Windows unit tests. Deploy remains blocked unless the full operator API gate passes.

## Migration, Rollback, Compatibility
Rollback is a test-file revert plus removal of this SOW and generated automation artifacts. No public API or config compatibility impact.

## Configuration And Environment Isolation
No config changes. WSL validation uses the existing `~/aats-venv`; Windows validation uses `.venv\Scripts\python.exe`.

## Code Organization And Dependencies
No dependency changes. The patch stays in the existing integration test module.

## Documentation And Operations Manual
This SOW records the bounded task. The operational deployment rule remains `scripts/deploy.sh` only.

## Deployment And Acceptance Criteria
Acceptance is binary: focused operator API profile/read-surface cluster passes, artifacts are updated, and any remaining full operator API failures are captured as the next blocker. No deployment is attempted while full gate failures remain.
