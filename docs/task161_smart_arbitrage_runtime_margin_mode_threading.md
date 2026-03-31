# Task 161 - Smart Arbitrage Runtime Margin Mode Threading

## Business objectives and boundaries
- 修复 `smart_arbitrage` 在 derivatives hedge leg metadata 和 derivatives cost scope 上仍硬读全局 `settings.margin_mode` 的问题。
- 优先复用 engine 已经解析出的 runtime `directional_target.margin_mode`，不要新增第二套来源。
- 保持对现有 helper 直接调用的向后兼容；仅新增显式参数，不移除旧调用方式。

## Current behavior summary
- `engine.py` 已经在 sleeve inventory 读取时使用 `directional_target.margin_mode or settings.margin_mode`。
- 但 `leg_planner.py` 仍直接把 hedge leg 的 `margin_mode` 写成 `settings.margin_mode`。
- `cost_model.py` 里 derivatives hedge 的 fee / spread / slippage scope 也仍直接使用 `settings.margin_mode`。

## Implementation plan
1. 给 `build_cost_breakdown(...)` 和 `build_legs(...)` 增加显式 `hedge_margin_mode` 参数，并保留 `None -> settings.margin_mode` 兼容回退。
2. 在 `SmartArbitrageStrategyEngine` 中统一解析 runtime hedge margin mode，并沿：
   - `evaluate_pair`
   - `_build_opportunity`
   - `_build_negative_basis_opportunity`
   - `_unsupported_execution_mode_opportunity`
   - `_candidate_from_opportunity`
   显式传递。
3. 补 unit / integration 回归，确认：
   - cost model 优先使用显式 hedge margin mode
   - hedge leg metadata 优先使用显式 hedge margin mode
   - runtime payload 中 hedge leg margin mode 与 candidate metrics 对齐

## Testing strategy
- unit:
  - `tests/unit/test_smart_arbitrage_v2_components.py`
- integration:
  - `tests/integration/test_strategy_runtime_integration.py -k smart_arbitrage_runtime_endpoint_exposes_executable_bundle_snapshot`

## Acceptance criteria
- lint 通过
- 相关 unit tests 通过
- 最窄 smart arbitrage runtime integration 通过
- smart arbitrage derivatives hedge side 不再直接依赖全局 `settings.margin_mode`
