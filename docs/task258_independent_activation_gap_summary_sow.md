# Task258: Independent Activation Gap Summary

## Business Objectives and Boundaries
- Objective: make runtime truth show how far independent long/short books are from activation without requiring manual arithmetic.
- Boundary: read-only runtime truth projection only. No strategy threshold, risk, execution, provider, symbol, venue, strategy family, release, promotion, tuning, schema, or live order behavior changes.
- Fixed scope remains OKX + BTC-USDT-SWAP; independent remains a live truth sampler and not final alpha.

## Module Responsibilities and Domain Model
- `scripts/runtime_truth_report.py` owns sanitized runtime truth projection.
- Existing `candidate_execution_drilldown.book_runtime_states` exposes per-leg score, threshold, signal edge, cost, and health.
- New `activation_gap` adds derived read-only values:
  - score gap to entry threshold.
  - score minus entry threshold.
  - signal edge minus expected cost.
  - signal edge gap to expected cost.
  - boolean readiness summaries for these two dimensions.

## Input / Output Interfaces
- Input: latest allocation payload, specifically independent book runtime states already present in sanitized drilldown.
- Output: no-secret JSON fields under each book runtime state.
- Existing fields remain backward-compatible.

## Database Schema / Tables / Indexes / Constraints
- No schema, table, index, or constraint changes.
- Reads continue to use existing allocation/audit/order/fill tables.

## Transactions, Consistency, Concurrency
- No writes and no transaction lifecycle changes.
- Runtime truth remains a point-in-time read projection.

## Authorization, Authentication, Data Security
- DB access uses existing container environment and never prints connection strings or secrets.
- Derived values are computed from already-sanitized numeric fields.

## Error Handling and Idempotency
- Missing or malformed numeric fields produce `null` derived values instead of raising.
- Re-running the report is idempotent.

## State Transition and Lifecycle
- No trading state transition change.
- This summary is explanatory only and must not be used as an execution gate.

## Caching and Performance
- No cache changes.
- Calculation is bounded to the latest decision and at most a few book summaries.

## Logging, Monitoring, Auditing
- Auditability improves by showing exact distance-to-activation values.
- No additional logging sink is added.

## Testing Strategy
- Add focused unit coverage for score/threshold and signal/cost gap calculations.
- Run script-level tests, required `aats/` lint, full unit suite, and live runtime smoke.

## Migration, Rollback, Compatibility
- No migration required.
- Rollback: revert this read-only projection commit and redeploy with `scripts/deploy.sh`.

## Configuration and Environment Isolation
- No configuration or environment variable changes.
- No AI provider, profile control, or timeframe plumbing changes.

## Code Organization and Dependencies
- Keep changes inside runtime truth script and focused tests.
- Use Python standard library decimal arithmetic only.

## Documentation and Operations Manual
- This SOW defines the activation-gap scope and safety boundaries.
- Operators should interpret gaps as evidence density, not as a recommendation to tune thresholds.

## Deployment and Acceptance Criteria
- Acceptance:
  1. Runtime truth shows `score_gap_to_entry_threshold` and `signal_edge_minus_cost_bps` for independent long/short books.
  2. Missing numeric fields degrade safely.
  3. Focused tests and runtime truth smoke pass.
  4. No live order behavior changes.
- Deployment: after tests and commit, deploy only through `scripts/deploy.sh`.
