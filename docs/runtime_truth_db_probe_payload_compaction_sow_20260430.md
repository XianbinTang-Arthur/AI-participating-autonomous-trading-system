# Runtime Truth DB Probe Payload Compaction SOW - 2026-04-30

## Business Objectives And Boundaries

Objective: restore reliable runtime truth generation for the live OKX BTC-USDT-SWAP microscope by reducing oversized read-only DB probe payloads that can hit the 45 second report deadline.

Boundary: this is a read-only reporting change. It does not change strategy logic, risk gates, execution behavior, provider behavior, symbols, venues, schemas, release gates, promotion gates, or order behavior.

## Module Responsibilities And Domain Model

`scripts/runtime_truth_report.py` owns the automation runtime truth surface. The affected domain object is `directional_episode_attribution.recent_decisions`, which links allocation decisions to order, fill, PnL, lifecycle, and microstructure evidence.

## Input/Output Interfaces

Input remains the live Postgres state loaded inside the project runtime/container environment. Output remains JSON runtime truth artifacts under `artifacts/automation/`.

The change keeps the `payload` field shape consumed by the sanitizer but changes the SQL projection to a compact payload subset instead of the full `portfolio_allocation_decisions.payload`.

## Database Schema / Tables / Indexes / Constraints

No schema, index, or constraint changes. The query remains read-only against existing tables including `portfolio_allocation_decisions`, `execution_orders`, `execution_commands`, `execution_fills`, `fill_outcomes`, `position_lots`, and `lot_events`.

## Transactions, Consistency, Concurrency

No writes and no explicit transaction changes. The probe keeps existing read consistency semantics from the DB connection.

## Authorization, Authentication, Data Security

No credentials are printed. DB access continues through existing runtime/container environment loading. Full allocation payloads are no longer returned for recent directional episodes, reducing accidental diagnostic payload exposure.

## Error Handling And Idempotency

The report remains idempotent and read-only. The acceptance criterion is that a normal runtime truth report no longer fails with `database_truth_unavailable` caused by `command_timeout_after_45s` when the DB is otherwise reachable.

## State Transition And Lifecycle

No runtime state transition changes. Automation state may be updated after validation to record this P0 runtime truth reliability fix.

## Caching And Performance

Performance target: shrink `directional_episode_attribution` output from roughly 1.1 MB to a compact evidence projection. After compaction, if measured live DB probe runtime remains close to the old 45 second command budget due to the existing broad truth query workload, use an explicit DB truth timeout constant sized from measurement rather than a hidden default.

## Logging, Monitoring, Auditing

Runtime truth artifacts remain the audit trail. The post-change report should continue to surface microstructure growth, latest decision/fill counts, AI runtime gates, git/deploy status, and health blockers.

## Testing Strategy

Add a focused unit regression preventing raw `rd.payload` from being selected in the directional attribution projection. Run focused script tests, lint, and required unit validation.

## Migration, Rollback, Compatibility

No migration. Rollback is a git revert of this read-only reporting patch. Existing downstream sanitizer compatibility is preserved by still producing a compact `payload` object.

## Configuration And Environment Isolation

No config changes. The live DB probe continues to use implicit runtime/container environment.

## Code Organization And Dependencies

No new dependencies. Keep changes in `scripts/runtime_truth_report.py`, its unit tests, and this SOW document.

## Documentation And Operations Manual

This document records the bounded task, acceptance criteria, and rollback path for the automation loop.

## Deployment And Acceptance Criteria

Deploy with `bash scripts/deploy.sh --profile derivatives-live --skip-commit` only after tests, commit, and push.

Acceptance criteria:
- Runtime truth DB probe succeeds without `command_timeout_after_45s` under the existing report timeout.
- DB truth timeout budget is explicit and justified by measured live probe runtime after payload compaction.
- `directional_episode_attribution` keeps edge/cost, reason-code, sleeve, order, fill, PnL, lifecycle, and microstructure attribution semantics.
- No strategy, risk, execution, schema, provider, symbol, venue, release, promotion, or order behavior changes.
