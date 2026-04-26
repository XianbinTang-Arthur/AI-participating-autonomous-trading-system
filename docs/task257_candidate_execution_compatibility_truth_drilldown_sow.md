# Task257: Candidate Execution Compatibility Truth Drilldown

## Business Objectives and Boundaries
- Objective: expose why the independent sleeve is inactive, execution-incompatible, and advisory-only in the latest decision.
- Boundary: read-only runtime truth projection only. No strategy threshold, risk, execution, provider, symbol, venue, strategy family, release, promotion, tuning, schema, or live order behavior changes.
- Fixed scope remains OKX + BTC-USDT-SWAP; independent remains a live truth sampler and not final alpha.

## Module Responsibilities and Domain Model
- `scripts/runtime_truth_report.py` owns sanitized runtime truth projection.
- `no_trade_attribution.final_blockers` identifies final no-trade labels.
- New `candidate_execution_drilldown` links those labels to sanitized source evidence:
  - candidate permission and execution compatibility.
  - budget and composition control trace.
  - long/short book state and signal-vs-threshold evidence.

## Input / Output Interfaces
- Input: latest allocation payload, latest audit refs, and per-decision order/fill counts read inside the running gateway container.
- Output: no-secret JSON fields under `latest_decision.no_trade_attribution.candidate_execution_drilldown`.
- Existing fields remain backward-compatible.

## Database Schema / Tables / Indexes / Constraints
- No schema, table, index, or constraint changes.
- Reads continue to use existing allocation/audit/order/fill tables.

## Transactions, Consistency, Concurrency
- No writes and no transaction lifecycle changes.
- Runtime truth remains a point-in-time read projection.

## Authorization, Authentication, Data Security
- DB access uses existing container environment and never prints connection strings or secrets.
- The drilldown intentionally emits compact selected fields, not raw payload bodies.
- Operator-auth-protected dashboard state remains unknown unless verified.

## Error Handling and Idempotency
- Missing nested control-trace fields degrade to empty summaries.
- Re-running the report is idempotent.

## State Transition and Lifecycle
- No trading state transition change.
- Drilldown lifecycle: identify relevant sleeve, summarize permission/composition/budget, then summarize book runtime states.

## Caching and Performance
- No cache changes.
- Processing is bounded to latest decision and at most a small number of sleeve/book summaries.

## Logging, Monitoring, Auditing
- Auditability improves by tying final blocker labels to compact source evidence.
- No additional logging sink is added.

## Testing Strategy
- Add focused unit coverage for candidate execution drilldown.
- Run script-level tests, required `aats/` lint, full unit suite, and live runtime smoke.

## Migration, Rollback, Compatibility
- No migration required.
- Rollback: revert this read-only projection commit and redeploy with `scripts/deploy.sh`.

## Configuration and Environment Isolation
- No configuration or environment variable changes.
- No AI provider, profile control, or timeframe plumbing changes.

## Code Organization and Dependencies
- Keep changes inside runtime truth script and focused tests.
- No new dependency.

## Documentation and Operations Manual
- This SOW defines drilldown scope and safety boundaries.
- Operators should read `candidate_execution_drilldown` as evidence for why no order was emitted, not as a tuning recommendation.

## Deployment and Acceptance Criteria
- Acceptance:
  1. Runtime truth links independent inactive/execution-incompatible/advisory-only labels to sanitized source evidence.
  2. Long/short signal-below-threshold evidence is visible without raw payload exposure.
  3. Focused tests and runtime truth smoke pass.
  4. No live order behavior changes.
- Deployment: after tests and commit, deploy only through `scripts/deploy.sh`.
