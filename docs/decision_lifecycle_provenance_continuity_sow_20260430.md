# Decision Lifecycle Provenance Continuity SOW

## Business Objectives and Boundaries

Add a read-only runtime truth surface that summarizes whether the current decision, the latest executable directional episode, and recent directional lifecycle/provenance evidence form a coherent microscope chain.

This task does not change strategy logic, risk gates, execution paths, provider behavior, symbols, venues, timeframes, schemas, release gates, tuning, or live order behavior.

## Module Responsibilities and Domain Model

- `scripts/runtime_truth_report.py` owns sanitized runtime truth projection.
- Existing live DB projections remain the evidence source for decisions, order surfaces, fills, lifecycle, and provenance.
- Existing execution-science truth surfaces remain the evidence source for depth, sequence, fill feasibility, and slippage/cost context.

## Input/Output Interfaces

Inputs:
- `database_truth.latest_decision`
- `directional_episode_attribution_truth`
- `directional_executable_episode_truth`
- `latest_decision_fill_feasibility_truth`
- `directional_command_flow_provenance_truth`
- `depth_slippage_lifecycle_truth`

Outputs:
- A new `decision_lifecycle_provenance_continuity_truth` object.
- Selected live facts for operator/automation consumption.

## Database Schema / Tables / Indexes / Constraints

No schema, table, index, or constraint changes.

## Transactions, Consistency, Concurrency

No transactions are opened by this change. The report only summarizes already-loaded runtime truth evidence.

## Authorization, Authentication, Data Security

No new authorization path is introduced. No raw payloads, secrets, credentials, or connection strings are printed or persisted.

## Error Handling and Idempotency

Missing evidence is classified with explicit `smallest_missing_field` values. Re-running the report is idempotent.

## State Transition and Lifecycle

No live runtime state changes. The truth surface classifies lifecycle continuity only.

## Caching and Performance

No cache is added. The implementation reuses in-memory runtime truth objects.

## Logging, Monitoring, Auditing

The new truth surface records compact lane-level status and coverage counts suitable for automation state and operator review.

## Testing Strategy

Add focused unit tests for:
- verified current no-order plus latest executable terminal no-fill continuity
- missing latest decision lifecycle evidence classification
- live fact projection of the new truth surface

## Migration, Rollback, Compatibility

No migration is required. Rollback is a normal git revert plus deployment through `scripts/deploy.sh --profile derivatives-live --skip-commit`.

## Configuration and Environment Isolation

No configuration changes.

## Code Organization and Dependencies

Keep implementation in `scripts/runtime_truth_report.py`; no new dependencies.

## Documentation and Operations Manual

Operators can use this truth surface to see whether current no-order decisions, executable terminal no-fill episodes, and recent lifecycle/provenance evidence remain coherent without inferring profitability.

## Deployment and Acceptance Criteria

Acceptance criteria:
- Runtime truth includes `decision_lifecycle_provenance_continuity_truth`.
- The surface does not expose raw payloads or secrets.
- Focused and required validation pass.
- Deployment, if performed, uses only `scripts/deploy.sh --profile derivatives-live --skip-commit`.
