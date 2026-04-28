# Truth Chain: Primary Candidate No-Order Explanation

## Business Objectives And Boundaries
Clarify why the latest OKX `BTC-USDT-SWAP` decision produced no new order when global no-trade reason codes include blockers from disabled or non-primary candidates. This is a read-only truth-chain improvement and does not change live trading behavior.

## Module Responsibilities And Domain Model
`scripts/runtime_truth_report.py` owns the runtime truth projection. It now separates the global no-trade blocker from the primary family candidate truth for the active live family.

## Input/Output Interfaces
Input is the existing portfolio allocation decision payload and candidate drilldown. Output is `primary_family_candidate_truth` inside `no_trade_attribution` and projected `live_runtime_facts` fields.

## Database Schema / Tables / Indexes / Constraints
No schema, index, or constraint changes. The implementation uses already-collected decision payload evidence.

## Transactions, Consistency, Concurrency
No writes and no locks. The change is deterministic report generation only.

## Authorization, Authentication, Data Security
No credentials are read or printed. The runtime truth report continues to redact sensitive values.

## Error Handling And Idempotency
If the primary candidate is missing, the report returns `missing_primary_family_candidate_truth` with the smallest missing field.

## State Transition And Lifecycle
No order, position, recovery, or strategy lifecycle transitions are changed.

## Caching And Performance
No cache changes. The summary is computed from already-loaded payload data.

## Logging, Monitoring, Auditing
The report now audits whether the global primary blocker applies to the primary family candidate, plus the candidate's route action, execution behavior, zero-delta state, and order expectation.

## Testing Strategy
Unit tests cover the current directional hold-current zero-delta case and live fact projection.

## Migration, Rollback, Compatibility
The change is additive. Rollback is reverting the script, tests, and this document.

## Configuration And Environment Isolation
No configuration changes. Scope remains OKX `BTC-USDT-SWAP`, live carrier `directional`, shadow benchmark `none_verified`.

## Code Organization And Dependencies
No new dependencies. The helper follows existing runtime truth summarizer patterns.

## Documentation And Operations Manual
Operators should read `latest_decision_primary_candidate_*` live facts before treating a global no-trade blocker as the directional candidate's blocker.

## Deployment And Acceptance Criteria
Acceptance requires focused unit tests, full unit validation, runtime truth regeneration, and deploy through `scripts/deploy.sh --profile derivatives-live --skip-commit` if the working tree is clean and the change remains read-only.
