# Recent Directional No-Order Bridge Decision Context Truth SOW

## Business Objectives And Boundaries

Objective: increase P1 trading-microscope evidence density by connecting the
recent no-order primary-candidate bridge density surface to the current
decision, lifecycle, and execution-science context.

Boundary: this is a read-only runtime truth projection. It does not change
strategy logic, risk gates, execution behavior, provider behavior, symbols,
venues, strategy families, release/promotion/tuning, timeframe plumbing, or
live order behavior.

## Module Responsibilities And Domain Model

`scripts/runtime_truth_report.py` owns the runtime truth projection. The new
surface summarizes already-built truth surfaces:

- `recent_directional_decision_chain_density_truth`
- `recent_directional_no_order_primary_candidate_bridge_density_truth`
- `latest_directional_no_order_primary_candidate_bridge_truth`
- `decision_lifecycle_provenance_continuity_truth`
- `decision_lifecycle_execution_science_continuity_truth`

The domain model remains decision -> order expectation -> bridge density ->
lifecycle continuity -> execution-science observability.

## Input/Output Interfaces

Input is the in-memory runtime truth report. No new DB query, API call, or
configuration input is added.

Output is a new report section:
`recent_directional_no_order_bridge_decision_context_truth`.

The live-facts projection exposes compact sanitized fields for automation state
without raw exchange payloads.

## Database Schema / Tables / Indexes / Constraints

No schema, table, index, or constraint change.

## Transactions, Consistency, Concurrency

No transaction or write path is introduced. Consistency depends on the existing
runtime truth report snapshot.

## Authorization, Authentication, Data Security

No credentials are read or printed. Raw payload exposure remains false. The AI
effective-runtime boundary remains auth-gated until an operator read credential
is available.

## Error Handling And Idempotency

The summarizer reports deterministic missing-field statuses when upstream truth
surfaces are unavailable or mismatched. Re-running the report is idempotent.

## State Transition And Lifecycle

No runtime state transition changes. The new surface clarifies that the recent
window is a no-order regime and that latest primary-candidate bridge evidence is
limited to the latest decision only.

## Caching And Performance

No cache behavior changes. The surface reuses already materialized report data
and should be negligible relative to the existing runtime truth probe.

## Logging, Monitoring, Auditing

The surface adds audit-friendly status, coverage, current decision context,
chain context, and interpretation fields. It is not alpha or profitability
evidence.

## Testing Strategy

Unit tests cover:

- verified no-order bridge decision context
- missing bridge-density dependency
- live-facts projection

## Migration, Rollback, Compatibility

No migration. Rollback is reverting the report/test/doc commit. Existing report
consumers remain compatible because the new section is additive.

## Configuration And Environment Isolation

No new configuration. Windows local tests use `.venv\Scripts\python.exe`.
Deployment, if needed, continues through `scripts/deploy.sh`.

## Code Organization And Dependencies

No new dependency. Code stays in the existing runtime truth report module and
unit test module.

## Documentation And Operations Manual

This SOW is the operations note for the bounded task.

## Deployment And Acceptance Criteria

Acceptance criteria:

- AC1: runtime truth report includes
  `recent_directional_no_order_bridge_decision_context_truth`
- AC2: verified status requires current latest decision, recent no-order bridge
  density, recent no-order decision chain density, lifecycle continuity, and
  execution-science continuity
- AC3: historical primary-candidate bridge scope remains `latest_decision_only`
  and is not claimed for all recent decisions
- AC4: focused tests and required unit validation pass
- AC5: deploy uses `scripts/deploy.sh` only if the safe-readonly change is
  committed and pushed
