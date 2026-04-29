# UI/backend contract fix - 2026-04-29

## Business objectives and boundaries

Fix two operator UI contract breaks exposed by the dashboard audit:

- Dashboard bundle must serve the `positionLifecycleAttribution` panel requested by strategy and execution views.
- RDP rollback from the UI must honor the backend token gate before calling `/rdp/parameters/rollback`.

Out of scope: redesigning RDP governance, changing trading behavior, changing auth policy, or touching deployment scripts.

## Module responsibilities and domain model

- `aats.api.auth_routes` owns dashboard bundle panel-key dispatch.
- `aats.api.static.modules.actions.rdp-actions` owns browser-side RDP operator actions.
- `aats.api.rdp_routes` remains the source of truth for rollback token requirements.

## Input/output interfaces

- Frontend bundle input: `panel=positionLifecycleAttribution` with `view=strategy` or `view=execution`.
- Backend bundle output: same panel envelope format as other panels: `{data, error}`.
- RDP rollback UI input: combo key `family/timeframe`.
- RDP rollback backend input: POST `/rdp/operator-tokens` with `{action: "rollback"}`, then POST `/rdp/parameters/rollback` with `X-Rdp-Apply-Token`.

## Database schema / tables / indexes / constraints

No schema, table, index, or constraint changes.

## Transactions, consistency, concurrency

Dashboard bundle remains read-only and cached per existing bundle cache rules. RDP rollback keeps the existing backend token and session-actor consistency checks.

## Authorization, authentication, data security

No auth relaxation. The RDP rollback fix tightens the UI to match the backend's existing HMAC-bound token gate.

## Error handling and idempotency

Panel lookup errors remain panel-level bundle errors. Token issuance or rollback failures surface through the existing localized `requestJson` error path.

## State transition and lifecycle

No trading-state lifecycle changes. Rollback continues to be executed only by the backend rollback route after token validation.

## Caching and performance

Bundle cache key already includes `view`, so strategy and execution can safely use different lifecycle attribution limits for the same panel key.

## Logging, monitoring, auditing

No new log paths. Rollback audit remains backend-owned.

## Testing strategy

Update dashboard UI integration coverage for the new bundle panel key and RDP rollback token flow.

## Migration, rollback, compatibility

No migration required. Rollback is a normal git revert of this small patch.

## Configuration and environment isolation

No new configuration. Existing `RDP_APPLY_TOKEN_SECRET` and token TTL settings remain authoritative.

## Code organization and dependencies

No new dependencies. Changes stay in existing API and static UI modules.

## Documentation and operations manual

This document records the scope and acceptance criteria for the fix.

## Deployment and acceptance criteria

Acceptance criteria:

- Dashboard bundle recognizes `positionLifecycleAttribution`.
- RDP rollback UI obtains a rollback token and sends it in `X-Rdp-Apply-Token`.
- Relevant tests pass.
