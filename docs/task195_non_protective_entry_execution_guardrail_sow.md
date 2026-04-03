# Task 195 - Non-Protective Entry Execution Guardrail

## Business objectives and boundaries
- Restore `derivatives_live` non-protective automatic entry execution by re-enabling `strategy_sleeve_auto_execution_enabled`.
- Make the “entry execution has been downgraded to advisory-only” state explicit at startup and in runtime/operator surfaces.
- Keep the change scoped to configuration, observability, and UI messaging.
- Do not refactor `auto_parallel` architecture or change allocator / execution semantics.

## Module responsibilities and domain model
- `configs/strategy_profiles/derivatives_live.yaml`: live runtime toggle for sleeve auto parallel / non-protective entry execution.
- `aats/services/strategy_engines/auto_parallel.py`: authoritative helper that explains whether non-protective entry execution is active or downgraded.
- `aats/bootstrap/config.py`: startup-time warning surface.
- `aats/services/operator/strategy_profiles.py`: profile-control summary surface.
- `aats/services/operator/query_service.py`: strategy runtime/operator API surface.
- `aats/api/static/modules/views/strategy-view.js`: operator-facing runtime warning in the dashboard.

## Input/output interfaces
- Input: existing runtime setting `strategy_sleeve_auto_execution_enabled`.
- Output:
  - startup structured warning log when disabled
  - runtime summary field `entry_execution_guard`
  - profile-control summary field `entry_execution_guard`
  - strategy dashboard warning copy

## Database schema / tables / indexes / constraints
- No database schema changes.
- No new tables, indexes, or constraints.

## Transactions, Consistency, Concurrency
- Read-only summary generation from runtime settings.
- No transactional write path changes.
- No new concurrency-sensitive state.

## Authorization, Authentication, Data Security
- No auth model changes.
- Warning content is operational metadata only; no secrets are introduced.

## Error Handling and Idempotency
- Guard summary generation is deterministic from settings.
- Startup warning is safe to emit repeatedly per runtime start.
- Runtime/operator summaries remain backward compatible by adding fields only.

## State Transition and Lifecycle
- When `strategy_sleeve_auto_execution_enabled=true`, non-protective opening / scale-in intents may proceed to allocator as usual.
- When `strategy_sleeve_auto_execution_enabled=false`, non-protective entry execution is treated as advisory-only; protection/de-risking remains allowed.

## Caching and Performance
- Guard summary is constant-time and settings-derived.
- No new heavy queries or cache invalidation paths.

## Logging, Monitoring, Auditing
- Add a high-priority startup warning with a stable warning code and human-readable Chinese summary.
- Expose the same warning semantics in operator/runtime payloads for ongoing monitoring.

## Testing Strategy
- Unit test startup warning emission.
- Unit test profile-control summary includes the guard object.
- Integration test `/strategy/runtime` exposes the guard when disabled.
- Integration/UI test strategy dashboard renders the advisory-only warning.

## Migration, Rollback, Compatibility
- No migration required.
- Rollback is trivial: revert config toggle and remove added summary fields if necessary.
- Public API compatibility preserved by adding optional fields only.

## Configuration and Environment Isolation
- Only `derivatives_live.yaml` runtime toggle changes behavior.
- Test environments continue to control the same behavior through `AATSSettings`.

## Code Organization and Dependencies
- Reuse existing modules; no new service layer or external dependency.
- Keep guard-summary logic centralized so logs/API/UI use one source of truth.

## Documentation and Operations Manual
- Operators should interpret the warning as: new non-protective entries will not auto-submit while protective reductions can still run.
- If the warning appears unexpectedly in live, verify the active runtime profile and `strategy_sleeve_auto_execution_enabled`.

## Deployment and Acceptance Criteria
- `derivatives_live` restores `strategy_sleeve_auto_execution_enabled: true`.
- Runtime start emits no advisory-only guard warning when enabled.
- When disabled in tests, startup log, runtime API, profile-control summary, and strategy page all clearly state the advisory-only downgrade.
