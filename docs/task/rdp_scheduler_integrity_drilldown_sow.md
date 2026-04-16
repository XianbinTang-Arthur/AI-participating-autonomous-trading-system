# RDP Scheduler Loop And Workbench Drilldown SOW

## Business objectives and boundaries
- Stop the scheduler from re-enqueuing the same workflow slot repeatedly in a freshly deployed runtime.
- Restore the WSL2 test runtime so the narrowest integration loop can be validated again.
- Add combo-level evidence drill-down on the RDP workbench without reverting to raw log-style payloads.
- Promote integrity blockers and incomplete evidence to first-class approval guards on the UI.

## Module responsibilities and domain model
- `aats/data_platform/operations/workflow_scheduler.py`
  - Canonicalize workflow slot keys before comparing or persisting.
- `aats/api/rdp_control_summary.py`
  - Build combo detail/evidence digests and approval gating metadata.
- `aats/api/rdp_routes.py`
  - Expose combo detail/evidence read endpoints.
- `aats/api/static/modules/views/rdp-control-panel.js`
  - Render combo drill-down and approval-blocked messaging.

## Input/output interfaces
- Add:
  - `GET /rdp/workbench/items/{combo_key}`
  - `GET /rdp/workbench/evidence/{combo_key}`
- Extend workbench item payloads with:
  - `approval_enabled`
  - `approval_blocked_reason`
  - `detail_summary`
  - `evidence_digest`

## Database schema / tables / indexes / constraints
- No schema changes.

## Transactions, consistency, concurrency
- Scheduler slot de-duplication must compare canonical UTC instants, not raw ISO strings with arbitrary offsets.
- No new write path besides existing scheduler state persistence.

## Authorization, authentication, data security
- New read routes remain `require_read_access`.
- No credentials or secret files exposed.

## Error handling and idempotency
- Incomplete evidence remains visible but non-actionable.
- Scheduler state canonicalization must remain backward-compatible with old stored slot strings.

## State transition and lifecycle
- `integrity blocked` workbench items cannot be approved from the UI.
- Combo detail/evidence drill-down is read-only.

## Caching and performance
- Reuse existing operator query helpers and latest snapshots.
- Keep combo detail construction bounded to the small work queue.

## Logging, monitoring, auditing
- Reuse existing scheduler/task logs and route logging.

## Testing strategy
- Add a scheduler unit test covering timezone-equivalent slot de-duplication.
- Add unit/integration tests for combo detail/evidence routes and blocked approval UI copy.

## Migration, rollback, compatibility
- Backward compatible with existing control-summary consumers.

## Configuration and environment isolation
- Rebuild `~/aats-venv` in WSL2 and rerun the narrowest affected integration test there.

## Code organization and dependencies
- Minimal scoped changes only; no unrelated refactors.

## Documentation and operations manual
- This SOW is the only additional doc for this slice.

## Deployment and acceptance criteria
- The scheduler no longer re-enqueues `data_maintenance`/`governance_cycle`/`release_cycle` every loop for the same slot.
- RDP 首页能展开 combo 级 evidence drill-down。
- `integrity blocked` / `incomplete_reason` 在 UI 上直接阻断审批并给出明确原因。
