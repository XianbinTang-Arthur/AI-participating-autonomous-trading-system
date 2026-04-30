# Decision Lifecycle Execution Science Continuity SOW

## Business Objectives and Boundaries

Add a read-only runtime truth surface that verifies whether decision lifecycle/provenance continuity is backed by execution-science evidence: orderbook depth, snapshot/diff sequence, local fill feasibility, slippage baseline, and terminal no-fill microstructure.

This task does not change strategy logic, risk gates, execution paths, AI provider behavior, symbols, venues, timeframes, schemas, release gates, tuning, or live order behavior.

## Module Responsibilities and Domain Model

- `scripts/runtime_truth_report.py` owns sanitized runtime truth projection.
- Existing runtime truth surfaces remain the evidence sources.
- The new surface aggregates P1 lifecycle/provenance continuity with P2 execution-science readiness.

## Input/Output Interfaces

Inputs:
- `decision_lifecycle_provenance_continuity_truth`
- `directional_executable_terminal_no_fill_pretrade_microstructure_truth`
- `orderbook_payload_depth_truth`
- `latest_decision_fill_feasibility_truth`
- `depth_slippage_lifecycle_truth`
- `execution_science_truth`
- `slippage_cost_calibration_truth`

Outputs:
- A new `decision_lifecycle_execution_science_continuity_truth` object.
- Selected live facts for operator and automation consumption.

## Database Schema / Tables / Indexes / Constraints

No schema, table, index, or constraint changes.

## Transactions, Consistency, Concurrency

No transactions are opened by this change. The report summarizes already-loaded runtime truth objects.

## Authorization, Authentication, Data Security

No new authorization path is introduced. The surface does not expose raw exchange payloads, secrets, credentials, tokens, or connection strings.

## Error Handling and Idempotency

Missing evidence is classified with explicit `smallest_missing_field` values. Re-running the report is idempotent.

## State Transition and Lifecycle

No live runtime state changes. The truth surface only classifies continuity between lifecycle evidence and execution-science evidence.

## Caching and Performance

No cache is added. The implementation reuses in-memory runtime truth objects.

## Logging, Monitoring, Auditing

The new surface records compact status and evidence counts suitable for automation state and operator review.

## Testing Strategy

Add focused unit tests for:
- verified no-order plus terminal-no-fill execution-science continuity
- missing terminal no-fill microstructure classification
- live fact projection of the new truth surface

## Migration, Rollback, Compatibility

No migration is required. Rollback is a normal git revert plus deployment through `scripts/deploy.sh --profile derivatives-live --skip-commit`.

## Configuration and Environment Isolation

No configuration changes.

## Code Organization and Dependencies

Keep implementation in `scripts/runtime_truth_report.py`; no new dependencies.

## Documentation and Operations Manual

Operators can use this truth surface to see whether current no-order decisions and the latest executable terminal no-fill episode are supported by execution-science evidence without inferring profitability.

## Deployment and Acceptance Criteria

Acceptance criteria:
- Runtime truth includes `decision_lifecycle_execution_science_continuity_truth`.
- The surface does not expose raw payloads or secrets.
- Focused and required validation pass.
- Deployment, if performed, uses only `scripts/deploy.sh --profile derivatives-live --skip-commit`.
