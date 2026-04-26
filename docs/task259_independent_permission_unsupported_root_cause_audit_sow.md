# Task259: Independent Permission Unsupported Root Cause Audit

## Business Objectives and Boundaries
- Objective: make runtime truth explain why independent `permission_mode=unsupported` and `execution_prerequisites_supported=false` block execution.
- Boundary: read-only runtime truth projection only. No strategy threshold, risk, execution, provider, symbol, venue, strategy family, release, promotion, tuning, schema, or live order behavior changes.
- Fixed scope remains OKX + BTC-USDT-SWAP; independent remains a live truth sampler and not final alpha.

## Module Responsibilities and Domain Model
- `scripts/runtime_truth_report.py` owns sanitized runtime truth projection.
- Existing `candidate_execution_drilldown` exposes permission, execution, composition, budget, and book runtime states.
- New `permission_root_cause` connects those existing sanitized fields into a compact root-cause summary:
  - primary permission blocker.
  - classification.
  - blocking evidence.
  - upstream candidate/book reasons.
  - positive context proving the candidate is configured/enabled where applicable.
  - composition effect showing advisory-only output.

## Input / Output Interfaces
- Input: latest allocation payload, specifically independent control trace and candidate reason codes already included in the runtime truth drilldown.
- Output: no-secret JSON under each candidate execution drilldown entry.
- Existing fields remain backward-compatible.

## Database Schema / Tables / Indexes / Constraints
- No schema, table, index, or constraint changes.
- Reads continue through existing decision/allocation/order/fill evidence paths.

## Transactions, Consistency, Concurrency
- No writes and no transaction lifecycle changes.
- Runtime truth remains a point-in-time read projection.

## Authorization, Authentication, Data Security
- DB access uses existing project runtime entrypoints and never prints connection strings or secrets.
- The new summary only uses already-sanitized booleans, modes, and reason codes.

## Error Handling and Idempotency
- Missing fields degrade to `primary=null` and `classification=insufficient_evidence`.
- Re-running the report is idempotent.

## State Transition and Lifecycle
- No trading lifecycle transition change.
- This summary is explanatory only and must not be consumed as an execution gate.

## Caching and Performance
- No cache changes.
- Calculation is bounded to relevant candidate drilldown entries.

## Logging, Monitoring, Auditing
- Auditability improves because future PM loops can distinguish configured/enabled context from actual permission denial.
- No additional logging sink is added.

## Testing Strategy
- Add focused unit coverage for `permission_root_cause`.
- Run script-level tests, required `aats/` lint, full unit suite, and live runtime smoke.

## Migration, Rollback, Compatibility
- No migration required.
- Rollback: revert this read-only projection commit and redeploy with `scripts/deploy.sh`.

## Configuration and Environment Isolation
- No configuration or environment variable changes.
- No AI provider, profile control, or timeframe plumbing changes.

## Code Organization and Dependencies
- Keep changes inside runtime truth script and focused tests.
- Use existing helper functions; add no external dependency.

## Documentation and Operations Manual
- This SOW defines the permission-root-cause audit scope and safety boundaries.
- Operators should treat `permission_root_cause` as audit evidence, not as permission to bypass execution gates.

## Deployment and Acceptance Criteria
- Acceptance:
  1. Runtime truth shows `permission_root_cause.primary=candidate_execution_incompatible` when independent permission is unsupported for execution compatibility.
  2. The summary includes concrete sanitized blocking evidence and composition effect.
  3. Missing evidence degrades safely.
  4. Focused tests and runtime truth smoke pass.
  5. No live order behavior changes.
- Deployment: after tests and commit, deploy only through `scripts/deploy.sh`.
