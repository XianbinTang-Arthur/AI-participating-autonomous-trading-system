# Dashboard Snapshot Mutation Refresh SOW

## Business objectives and boundaries
Reduce operator UI stalls caused by successful auth/session POST requests and bursty mutation-triggered dashboard snapshot refreshes. The change is limited to API gateway dashboard cache invalidation and snapshot refresh scheduling. Trading decisions, execution, risk controls, database schema, and order state persistence are out of scope.

## Module responsibilities and domain model
`apps/api_gateway/main.py` owns request-level cache invalidation after successful mutating HTTP requests. `aats.services.operator.dashboard_snapshot.DashboardSnapshotPlane` owns snapshot lifecycle, stale marking, and background refresh enqueueing. Snapshot policies already classify expensive panels by priority and `scheduled_refresh`.

## Input/output interfaces
Inputs are HTTP method, path, response status, and the in-process `dashboard_snapshot_plane`. Outputs are bundle-cache invalidation and optional snapshot refresh enqueueing. Public API response payloads are unchanged.

## Database schema / tables / indexes / constraints
No schema, table, index, or constraint changes.

## Transactions, consistency, concurrency
Bundle cache is still cleared after every successful mutating request. Snapshot data is marked stale for every true dashboard-data mutation, even during refresh cooldown windows. Only scheduled panels and a small set of path-specific directly affected panels are eagerly enqueued. A short per-process cooldown prevents mutation bursts from repeatedly enqueuing the same background refresh wave.

## Authorization, authentication, data security
Auth/session paths do not trigger dashboard snapshot refresh. They still invalidate the bundle cache so session/auth panels cannot reuse stale identity state. No secrets or credentials are read or logged.

## Error handling and idempotency
Failed HTTP mutations keep existing behavior and do not invalidate cache. Snapshot refresh enqueueing remains best effort and idempotent through existing inflight de-duplication. Cooldown skips redundant refresh waves without changing the original mutation response.

## State transition and lifecycle
For dashboard-data mutations, existing snapshots transition to stale. Fast scheduled panels refresh in the background. Expensive non-scheduled panels such as strategy attribution and AI latest/overview remain stale until the dashboard read path requests them, preventing login-triggered background slow reads. Non-scheduled panels directly affected by known mutation families, such as RDP control overview or AI config summary, receive targeted eager refresh without pulling every P2/P3 panel.

## Caching and performance
The performance target is to stop `POST /auth/login` from triggering full dashboard snapshot refreshes and to prevent one mutation from eagerly reading every slow P2/P3 panel. The bundle cache TTL and dashboard bundle response format are unchanged.

## Logging, monitoring, auditing
Existing dashboard snapshot refresh logs continue to show `reason=<method>_mutation` for background refreshes that are actually enqueued. No new audit records are introduced.

## Testing Strategy
Add focused unit tests for gateway mutation refresh classification/cooldown, always-stale invalidation, path-specific eager refresh selection, and snapshot scheduled-only invalidation. Run ruff, unit tests, and the narrowest affected integration test in WSL2.

## Migration, rollback, compatibility
No migration required. Rollback is a code revert. Public APIs and persisted data remain compatible.

## Configuration and environment isolation
No environment variable or deployment configuration changes.

## Code organization and dependencies
No new dependencies. New helpers stay beside the existing gateway middleware; snapshot lifecycle helpers stay inside `DashboardSnapshotPlane`.

## Documentation and operations manual
This SOW documents the operational intent. Operators should expect login/logout/user auth changes to clear UI bundle cache but not trigger snapshot refresh storms.

## Deployment and acceptance criteria
Acceptance criteria: auth/session POST skips snapshot refresh, dashboard-data mutation always marks snapshots stale, cooldown only suppresses enqueue work, path-specific non-scheduled panels can refresh eagerly, and targeted tests pass.
