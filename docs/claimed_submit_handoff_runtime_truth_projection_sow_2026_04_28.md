# Claimed Submit Handoff Runtime Truth Projection SOW

## Business Objectives and Boundaries
Expose the latest claimed-submit operator handoff artifact in runtime truth so the system can distinguish "awaiting external OKX absence confirmation" from a missing handoff or a ready protected-recovery packet. This is read-only and must not mutate order state, exchange state, Redis, Postgres, strategy configuration, risk gates, provider settings, symbols, venues, or strategy families.

## Module Responsibilities and Domain Model
`scripts/runtime_truth_report.py` remains the no-secret runtime truth collector. The new responsibility is artifact projection for `claimed_submit_operator_handoff_*.json`. The domain model is a current claimed-submit stuck order plus a matching handoff artifact, validation status, exact confirmation string, and protected-recovery readiness.

## Input/Output Interfaces
Input is the existing automation artifact directory plus the current `claimed_submit_stuck_submission_truth`. Output is `claimed_submit_operator_handoff_truth` in the report and flattened `claimed_submit_operator_handoff_*` fields in `runtime.live_runtime_facts`.

## Database Schema / Tables / Indexes / Constraints
No schema, table, index, or constraint changes. The projection reads local JSON artifacts only.

## Transactions, Consistency, Concurrency
No transactions. Consistency is enforced by matching the handoff order id and exact confirmation against the current claimed-submit truth before any `ready_for_protected_recovery` flag is surfaced as true.

## Authorization, Authentication, Data Security
No authentication or live API credential is read. The projected artifact contains operational identifiers and exact confirmation text only. Secret redaction remains applied to final runtime truth JSON.

## Error Handling and Idempotency
Missing, malformed, or non-handoff artifacts are skipped. A missing handoff produces `missing_operator_handoff_artifact`; a mismatched handoff produces `stale_or_mismatched_operator_handoff`. Re-running the report is idempotent.

## State Transition and Lifecycle
No lifecycle transition is introduced. `awaiting_external_operator_confirmation` is not permission to recover. `ready_for_protected_recovery` is true only when the verifier artifact is valid, ready, and matches the current order and confirmation.

## Caching and Performance
The scan is limited to `artifacts/automation/claimed_submit_operator_handoff_*.json`, which is small and local. No cache is required.

## Logging, Monitoring, Auditing
Runtime truth now audits the handoff artifact path, source packet/runtime truth, validation status, readiness, match checks, and next action.

## Testing Strategy
Unit tests cover latest matching artifact selection, stale/mismatched order rejection, and live fact projection.

## Migration, Rollback, Compatibility
No migration. Rollback is reverting the commit and redeploying through `scripts/deploy.sh`. Existing runtime truth consumers remain compatible because new fields are additive.

## Configuration and Environment Isolation
No new configuration. The logic is environment-isolated to the repository artifact directory resolved from `--repo-root`.

## Code Organization and Dependencies
No new dependency. Helper functions live next to existing artifact projection code in `scripts/runtime_truth_report.py`.

## Documentation and Operations Manual
Operators should use the handoff projection as a status signal only. Protected recovery still requires exact external OKX absence confirmation and the existing protected writer path.

## Deployment and Acceptance Criteria
Acceptance requires focused runtime truth tests, `ruff check aats/ --fix`, full unit tests, deployment via `scripts/deploy.sh --profile derivatives-live --skip-commit`, and post-deploy runtime truth showing the handoff fields without order/exchange mutation.
