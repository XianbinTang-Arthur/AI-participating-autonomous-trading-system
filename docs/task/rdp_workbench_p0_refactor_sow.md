# RDP Workbench P0 Refactor SOW

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## Business objectives and boundaries
- Replace the current RDP home panel's system-centric presentation with a task-centric workbench.
- Surface the next operator action, pending governance approvals, data-integrity alerts, and tuning proposals on the main RDP panel.
- Preserve existing write APIs and `GET /rdp/control-summary` for compatibility.
- Scope for this change is limited to new read-side workbench/tuning APIs plus the AI Config RDP panel.

## Module responsibilities and domain model
- `aats/api/rdp_control_summary.py`
  - Continue serving the legacy control summary.
  - Add new task-oriented workbench/tuning view models.
- `aats/api/rdp_routes.py`
  - Expose new read routes and tuning review write routes.
- `aats/api/auth_routes.py`
  - Register dashboard bundle panel keys for the new RDP workbench payloads.
- `aats/api/static/modules/store.js`
  - Fetch the new workbench/tuning panels for `aiConfig`.
- `aats/api/static/modules/views/rdp-control-panel.js`
  - Render the new workbench layout.
- `aats/api/static/modules/actions/rdp-actions.js`
  - Add tuning proposal review actions.

## Input/output interfaces
- Add:
  - `GET /rdp/workbench/overview`
  - `GET /rdp/workbench/items`
  - `GET /rdp/workbench/alerts`
  - `GET /rdp/tuning/overview`
  - `GET /rdp/tuning/proposals`
  - `POST /rdp/tuning/proposals/{proposal_id}/approve`
  - `POST /rdp/tuning/proposals/{proposal_id}/reject`
- Keep existing routes unchanged.

## Database schema / tables / indexes / constraints
- Reuse existing governance DB tables and DB-first loaders.
- No schema migration in this change.

## Transactions, consistency, concurrency
- Read-side payloads are assembled from existing registries and snapshots; no new concurrency model.
- Tuning review actions reuse existing registry persistence and DB-first save ordering.

## Authorization, authentication, data security
- New read routes require `require_read_access`.
- New tuning review write routes require `require_write_access`.
- No credentials or secret files are read or exposed.

## Error handling and idempotency
- Read routes should degrade to empty payloads rather than crash the dashboard.
- Tuning approve/reject must return structured failure when the proposal is missing or already reviewed.

## State transition and lifecycle
- Workbench items must only represent current actionable combos.
- Incomplete research snapshots must remain visible as alerts, not as actionable approvals.
- Tuning proposals are displayed by status and can transition `pending_review -> approved/rejected`.

## Caching and performance
- Keep payload assembly bounded.
- Avoid introducing new DB writes on read paths.

## Logging, monitoring, auditing
- Reuse current route logging and registry audit trails.
- No new logging channel required for this slice.

## Testing strategy
- Add/update unit tests for:
  - workbench overview/items/alerts payloads
  - tuning overview/proposal read payloads
  - tuning approve/reject routes
- Update the narrowest UI smoke test for the new RDP layout.

## Migration, rollback, compatibility
- Backward compatible.
- Existing `/rdp/control-summary` remains available.
- Front-end will consume new panels incrementally.

## Configuration and environment isolation
- No new config required.
- Existing RDP environment guards and DB-first loaders remain authoritative.

## Code organization and dependencies
- Keep new view-model assembly in `rdp_control_summary.py` to minimize duplicated query logic.
- Avoid unrelated refactors.

## Documentation and operations manual
- This SOW documents the change scope; no additional manual in this slice.

## Deployment and acceptance criteria
- AI Config RDP panel shows:
  - current round headline
  - actionable work queue
  - integrity alerts
  - runtime rail
  - tuning summary
- Raw evidence strings no longer dominate homepage cards.
- Existing approval/release/rollback actions still work.
