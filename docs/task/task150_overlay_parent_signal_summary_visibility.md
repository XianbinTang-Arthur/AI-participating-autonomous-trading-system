# Task 150: Overlay Parent-Signal Summary Visibility

## Goal

Expose overlay-family parent exposure signals directly in runtime/operator summaries so protective and opportunistic decisions no longer require reading raw candidate metrics to understand:

- `parent_target_signal`
- `parent_current_signal`
- `parent_effective_signal`
- `signal_source`

## What Changed

- Added the four fields to:
  - `StrategyExecutionSummary`
  - `HedgeOverlayDecision`
- `StrategyCoordinatorService` now copies these values from overlay candidate metrics into:
  - `family_execution_summary`
  - synthesized `hedge_overlay_decision`
- `OperatorQueryService._overlay_audit_summary()` now forwards the same fields into operator audit payloads.
- Strategy UI and decision drawer now render a unified Chinese summary:
  - `父腿目标 ... / 当前库存 ... / 生效方向 ... / 来源 ...`

## Validation

- Unit:
  - `test_strategy_coordinator.py`
  - `test_operator_position_states.py`
- Integration:
  - `test_dashboard_ui.py`
