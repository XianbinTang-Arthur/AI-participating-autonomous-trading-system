# No-Order Root Oscillation Truth Surface SOW

## Business Objectives And Boundaries
The trading microscope currently observes repeated latest-decision no-order root switches between verified non-executable states. The objective is to expose a stable runtime truth semantic for these roots so automation can distinguish real order/fill/deploy/health changes from equivalent no-order state changes.

This work is read-only. It must not change strategy selection, risk gates, execution behavior, provider behavior, symbols, venues, timeframes, release gates, promotion gates, schemas, or live order behavior.

## Module Responsibilities And Domain Model
`scripts/runtime_truth_report.py` owns the runtime truth projection. It should classify primary candidate no-order root causes into an explicit semantic equivalence class when the primary candidate is verified no-order expected.

`tests/unit/scripts/test_runtime_truth_report.py` owns focused validation for the truth projection.

## Input/Output Interfaces
Input remains the existing parsed DB/runtime truth structure. Output adds stable read-only fields under latest-decision no-order truth and live runtime facts. Existing fields remain backward compatible.

## Database Schema / Tables / Indexes / Constraints
No schema, table, index, or constraint changes.

## Transactions, Consistency, Concurrency
No writes to live databases. The runtime report remains a read-only snapshot.

## Authorization, Authentication, Data Security
No credentials are read or printed. Auth-required dashboard fields remain classified as auth-required rather than inferred.

## Error Handling And Idempotency
Missing or unknown root causes must produce explicit unknown/missing semantic states without raising. Re-running the report must be idempotent.

## State Transition And Lifecycle
Verified no-order expected roots such as hold-current zero delta and advisory-only suppressed after approval should share a semantic class that marks them non-executable/no-order expected. This prevents automation from treating oscillation inside that class as order/fill materiality.

## Caching And Performance
No additional external calls or database queries. Classification is in-memory over already parsed truth.

## Logging, Monitoring, Auditing
Runtime truth artifacts will expose the semantic fields for auditing. No new logs are required.

## Testing Strategy
Add focused unit coverage for no-order semantic equivalence and live runtime facts projection. Run the narrow test selection and ruff for the touched script.

## Migration, Rollback, Compatibility
Rollback is reverting the script/test/doc changes. Output additions are backward-compatible.

## Configuration And Environment Isolation
No configuration changes.

## Code Organization And Dependencies
Keep helper logic inside `scripts/runtime_truth_report.py`; do not add dependencies.

## Documentation And Operations Manual
This SOW documents the operational reason for the truth surface change. The automation state will point to the generated runtime truth artifact.

## Deployment And Acceptance Criteria
Deployment is allowed only after code is committed/pushed and via `scripts/deploy.sh` in a follow-through step if needed. Acceptance criteria:

1. Verified no-order roots expose a stable semantic equivalence class.
2. Existing latest decision and directional episode truth remains backward compatible.
3. Focused tests pass.
4. Runtime truth can be regenerated without blockers.
