# Task 204 - Auto Parallel Compatibility and Naming Cleanup

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## Business objectives and boundaries
- Further demote `automation_state` to a compatibility-only coarse projection.
- Make the internal “runtime supported” signal self-descriptive, so it is not confused with execution compatibility.
- Improve leg-level audit notes so hold/suppression causes remain visible in per-leg traces.
- Do not change allocator routing or live trading behavior.

## Module responsibilities and domain model
- `sleeve_routing_models.py`
  - Renames the internal candidate-state support flag to `state_runtime_supported`.
- `sleeve_execution_permission.py`
  - Uses the renamed field and keeps permission semantics unchanged.
- `auto_parallel.py`
  - Continues exposing the public `runtime_supported` field on automation decisions, but records the internal state-derived source more explicitly.
  - Writes extra compatibility metadata showing that `legacy_automation_state` is only a coarse projection.
- `sleeve_routing_composer.py`
  - Adds explicit hold/suppression context to leg notes.
- `query_service.py`
  - Keeps legacy automation-state summaries in the compatibility area and labels them as coarse compatibility projections.

## Input/output interfaces
- Inputs:
  - raw sleeve candidate inputs
  - permission / budget / composition decisions
- Outputs:
  - unchanged public route behavior
  - clearer internal naming
  - richer compatibility metadata
  - more specific leg notes for suppressed/held outcomes

## Database schema / tables / indexes / constraints
- No database schema changes.
- Event payloads remain backward compatible.

## Transactions, consistency, concurrency
- No transaction or concurrency changes.

## Authorization, authentication, data security
- No auth or security changes.

## Error handling and idempotency
- Historical payloads without the new compatibility metadata still work.
- Query helpers continue falling back to top-level legacy fields for old payloads.

## State transition and lifecycle
- No control-mode or execution-behavior lifecycle changes.
- Only compatibility labeling and audit detail change.

## Caching and performance
- No meaningful performance impact.

## Logging, monitoring, auditing
- Compatibility metadata now makes it explicit that legacy automation states are coarse projections.
- Leg notes now distinguish permission-denied holds from budget-zero suppression.

## Testing strategy
- Update unit tests for the renamed internal field.
- Add composer tests asserting hold-note context.
- Run full unit suite plus the narrowest runtime integration tests affected by the compatibility summary.

## Migration, rollback, compatibility
- Backward compatible.
- Rollback is straightforward because public schema fields remain present.

## Configuration and environment isolation
- No config changes.

## Code organization and dependencies
- Minimal internal cleanup only; no unrelated refactors.

## Documentation and operations manual
- This task closes the remaining semantic gap between coarse legacy automation states and canonical execution diagnostics.

## Deployment and acceptance criteria
- `automation_state` is clearly documented as a compatibility-only coarse projection.
- Internal naming no longer conflates candidate state support with broader runtime support.
- Leg notes distinguish permission-denied holds from budget-zero suppression.
