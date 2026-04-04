# Task 200 - Auto Parallel Phase 5 Runtime Compatibility and Execution Behavior

## Business objectives and boundaries
- Continue shrinking deprecated-key exposure so the full `/strategy/runtime` payload treats `strategy_sleeve_auto_execution_enabled` as the only first-class config knob.
- Keep the deprecated key available only inside a read-only compatibility window for migration diagnostics.
- Promote `budget_zero_suppressed` from a payload-only marker into an explicit runtime and allocator-visible execution behavior semantic.
- Keep allocator route semantics unchanged in this phase; do not introduce a new route action.

## Module responsibilities and domain model
- `strategy_runtime.py`: defines canonical `execution_behavior` values on automation decisions and sleeve intents.
- `sleeve_routing_models.py` / `sleeve_routing_composer.py`: assign canonical execution behavior alongside control mode.
- `auto_parallel.py`: persists execution behavior into automation decisions, sleeve intents, metrics, and control trace.
- `allocator.py`: surfaces `suppressed_after_approval` as a first-class blocked reason/operator summary instead of letting it hide behind generic zero-delta behavior.
- `query_service.py`: exposes execution behavior counts and summaries; moves deprecated auto-execution config to compatibility metadata in full runtime payloads.
- `strategy-view.js`: renders execution behavior summaries and distributions in clean UTF-8 Chinese.

## Input/output interfaces
- Inputs:
  - canonical permission decision
  - budget decision and zero-suppression state
  - composed sleeve route action
  - recent sleeve intents stored in strategy runtime repo
- Outputs:
  - `StrategySleeveAutomationDecision.execution_behavior`
  - `StrategySleeveIntent.execution_behavior`
  - `summary.execution_behavior_counts`
  - `summary.execution_behavior_summary`
  - allocator blocked reason `allocator_sleeve_suppressed_after_approval`
  - `configured_parameters.compatibility.deprecated_auto_execution_*`

## State transition and lifecycle
- `permission_denied` stays a control-mode concept and still composes to `advisory_only` or `hold_current`.
- `budget_zero_suppressed` now also maps to explicit execution behavior `suppressed_after_approval`.
- `protective_override` maps to execution behavior `protective_execute`.
- `approved` plus actionable route keeps execution behavior `execute_target`.

## Logging, monitoring, auditing
- `control_trace` now carries both `execution_control_mode` and `execution_behavior`.
- operator/runtime summaries can distinguish:
  - permission denied
  - approved but suppressed after approval
  - protective execution
  - direct execution

## Migration, rollback, compatibility
- `strategy_sleeve_auto_execution_enabled` remains the canonical config field.
- Historical note: at phase 5 the old key still survived in compatibility metadata. It has since been removed from active inputs.
- Rollback is low risk because route semantics are unchanged; existing downstream allocator selection still keys off `route_action` and quantities.

## Testing strategy
- Update runtime integration tests to assert the deprecated key is no longer top-level in full `/strategy/runtime`.
- Add assertions for `execution_behavior_counts` and `execution_behavior_summary`.
- Update strategy coordinator and routing composer tests to validate explicit `execution_behavior`.
- Update dashboard UI tests to verify execution behavior summaries and deprecated-key compatibility messaging.

## Acceptance criteria
- Full `/strategy/runtime` exposes the deprecated auto-execution key only inside `configured_parameters.compatibility`.
- Recent runtime/operator summaries can explicitly tell whether a sleeve was suppressed after approval.
- Strategy page renders both control-mode and execution-behavior summaries without mojibake or mixed semantics.
- Allocator blocked summaries distinguish permission rejection from approved-but-suppressed behavior.
