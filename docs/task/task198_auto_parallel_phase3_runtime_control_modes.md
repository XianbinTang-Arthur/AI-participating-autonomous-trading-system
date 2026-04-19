# Task 198 - Auto Parallel Phase 3 Runtime Control Modes

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## Business objectives and boundaries
- Promote sleeve execution control outcomes into a first-class runtime mode instead of inferring them from `route_action` plus nested traces.
- Keep allocator and execution route semantics unchanged in this phase.
- Continue preserving backward compatibility for existing payload consumers while making operator/runtime/UI diagnostics more explicit.

## Module responsibilities and domain model
- `sleeve_routing_composer.py`: emits the canonical `execution_control_mode`.
- `auto_parallel.py`: persists that mode onto automation decisions, sleeve intents, metrics, and control traces.
- `query_service.py`: summarizes recent control-mode distribution and exposes config-source metadata.
- `strategy-view.js`: renders the control-mode distribution and deprecated config-source warning in a compact operator-facing way.

## Input/output interfaces
- Inputs:
  - phase 1 permission / budget / composition outputs
  - recent sleeve intents
  - latest automation decisions
  - effective auto-execution config source
- Outputs:
  - `execution_control_mode` on automation decisions and sleeve intents
  - `execution_control_mode_counts` in runtime summary
  - `entry_auto_execution_config_source` and `entry_auto_execution_uses_deprecated_key`
  - strategy UI callouts and summaries that distinguish permission denial, budget-zero suppression, protective override, and approved execution

## Database schema / tables / indexes / constraints
- No database schema changes.
- New fields ride inside existing strategy runtime payloads only.

## Transactions, Consistency, Concurrency
- No transactional behavior changes.
- Control mode remains a deterministic pure function of the existing phase 1 permission/budget/composition inputs.

## Authorization, Authentication, Data Security
- No auth changes.
- Exposed fields are operational diagnostics only.

## Error Handling and Idempotency
- Historical payloads without the new field still deserialize through fallback inference in `query_service.py`.
- Re-running the same coordinator cycle should emit the same `execution_control_mode`.

## State Transition and Lifecycle
- Phase 3 defines four canonical modes:
  - `approved`
  - `permission_denied`
  - `budget_zero_suppressed`
  - `protective_override`
- These modes do not change allocator-facing route actions in this phase.

## Caching and Performance
- Runtime summary adds only lightweight in-memory counting over recent sleeve intents.
- No new storage scans beyond the existing runtime summary inputs.

## Logging, Monitoring, Auditing
- Control mode is now explicit in runtime payloads and control traces.
- Runtime summary exposes deprecated config-source usage so operators can tell whether the old key still drives behavior.

## Testing Strategy
- Extend composer tests to lock the new canonical control mode.
- Extend coordinator/runtime tests to verify persisted control mode and runtime counts.
- Extend dashboard tests to verify control-mode distribution and deprecated config-source warning.

## Migration, Rollback, Compatibility
- Fully backward compatible with phase 1/2 payloads.
- Rollback is straightforward: remove the new fields and revert runtime summary/UI usage.

## Configuration and Environment Isolation
- Phase 3 keeps the phase 2 dual-read config behavior.
- Runtime surfaces now show whether the deprecated key is still the effective source.

## Code Organization and Dependencies
- No new external dependencies.
- Control mode stays inside the existing strategy-engine/runtime/operator layers.

## Documentation and Operations Manual
- Operators should now be able to answer whether a no-order sample was approved, permission-denied, budget-zero suppressed, or protective-only directly from runtime payloads and the strategy page.

## Deployment and Acceptance Criteria
- Runtime payloads expose `execution_control_mode` and `execution_control_mode_counts`.
- Strategy page distinguishes the four control modes and shows deprecated config-source usage.
- Lint, targeted unit tests, and targeted integration tests pass.
