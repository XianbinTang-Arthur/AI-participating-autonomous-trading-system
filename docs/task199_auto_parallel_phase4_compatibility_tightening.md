# Task 199 - Auto Parallel Phase 4 Compatibility Tightening

## Business objectives and boundaries
- Tighten the primary operator-facing surfaces so `strategy_sleeve_auto_execution_enabled` is the only first-class config key on the strategy page bundle.
- Keep the deprecated key available only as compatibility metadata and runtime-source diagnostics.
- Promote a concise `execution_control_summary` so operators can see the dominant recent control outcome without reverse-engineering counts.
- Keep allocator route semantics unchanged in this phase.

## Module responsibilities and domain model
- `query_service.py`: produces `execution_control_summary` and preserves deprecated-key compatibility metadata separately from the primary config fields.
- `auth_routes.py`: trims the strategy-page runtime payload so the deprecated key is no longer a first-class configured parameter.
- `strategy-view.js`: treats the new key as the canonical runtime knob and renders the new execution-control summary.
- `sleeve_execution_permission.py`: ensures operator-visible guard text is clean UTF-8 Chinese.

## Input/output interfaces
- Inputs:
  - recent sleeve intents and their canonical `execution_control_mode`
  - runtime config-source metadata from phase 2/3
  - configured strategy runtime parameters
- Outputs:
  - `summary.execution_control_summary`
  - trimmed strategy-view payload with `configured_parameters.compatibility`
  - strategy UI summary/callout that surfaces the dominant recent control outcome

## Compatibility and migration
- Full `/strategy/runtime` keeps the deprecated key for backward compatibility.
- The strategy-page bundle stops treating the deprecated key as a primary configured parameter.
- Operators still see whether the deprecated key is driving the runtime via:
  - `entry_auto_execution_config_source`
  - `entry_auto_execution_uses_deprecated_key`
  - `configured_parameters.compatibility`

## Testing strategy
- Update runtime integration tests to assert `execution_control_summary`.
- Update dashboard bundle trimming tests to verify the deprecated key is moved under compatibility metadata.
- Update dashboard UI tests to verify the new execution-control summary renders correctly.
