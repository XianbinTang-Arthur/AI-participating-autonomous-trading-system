# Task 202 - Demote Legacy Automation State Counts to Runtime Compatibility

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## Business objectives and boundaries
- Make `execution_control_mode` and `execution_behavior` the only primary diagnostic signals on operator/runtime surfaces.
- Demote the remaining legacy `automation_state` counts to a compatibility-only runtime section.
- Keep per-decision `automation_state` for backward compatibility, but stop using it as a primary operator or dashboard summary signal.

## Module responsibilities and domain model
- `query_service.py`: moves legacy automation-state counts and family maps under `summary.compatibility`.
- `strategy-view.js`: renders execution behavior and execution control mode as the primary table and summary semantics.
- `strategy_runtime.py`: keeps `automation_state` in the schema strictly as a compatibility field.

## Input/output interfaces
- Inputs:
  - recent automation decisions in the latest strategy snapshot
  - canonical `execution_control_mode`
  - canonical `execution_behavior`
- Outputs:
  - `summary.compatibility.legacy_automation_state_counts`
  - `summary.compatibility.legacy_latest_automation_states`
  - dashboard sections that no longer rely on `automation_active_count / contracted / paused`

## Compatibility and migration
- Top-level runtime summary no longer exposes `automation_active_count`, `automation_contracted_count`, or `automation_paused_count`.
- Older payload consumers can still read legacy values from the compatibility area during the migration window.
- UI keeps historical fallbacks only where necessary for old payload rendering, not as a primary interpretation path.

## Testing strategy
- Update runtime integration tests to assert:
  - top-level legacy automation counts are absent
  - compatibility substructure still exposes them
- Update dashboard UI tests to ensure the strategy table no longer surfaces raw `automation_state` labels as the primary status line.

## Acceptance criteria
- Runtime summary diagnostics are driven by `entry_auto_execution_enabled`, `execution_control_mode`, and `execution_behavior`.
- Legacy automation-state counts remain readable only from the compatibility section.
- Strategy dashboard no longer relies on `automation_state` to explain why a sleeve does or does not trade.
