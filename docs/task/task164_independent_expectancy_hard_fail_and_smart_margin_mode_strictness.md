# Task 164 - Independent Expectancy Hard Fail And Smart Margin Mode Strictness

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## Business objectives and boundaries
- 把 `independent` 的 expectancy 失败语义从“返回零值 fallback expectancy”进一步收紧成显式失败。
- 把 `smart_arbitrage` 的 runtime 对冲保证金模式从“主链显式传透但 helper 可静默 fallback”收紧成：engine 主链必须显式提供，否则直接阻断 candidate。
- 保持向后兼容，不强行打断仓库内仍直接调用 helper 的旧测试或旁路代码。

## Current behavior summary
- `independent` 之前在 expectancy 解析失败时会返回 `resolution_failed=True` 的零值 expectancy。
- `smart_arbitrage` 之前虽然 engine 会优先传 runtime `margin_mode`，但 `leg_planner.py` 和 `cost_model.py` 仍会在漏传时静默回退 `settings.margin_mode`。

## Implementation plan
1. 把 `IndependentBookExpectancyResolver` 改成可返回 `None`。
2. `_resolve_independent_book_expectancy(...)` 失败时直接返回 `None`。
3. `_evaluate_independent_book(...)` 明确把 `expectancy is None` 视为 fail-closed：
   - 新开仓 / 加仓 blocked
   - 既有持仓不再读取 fallback net edge
4. 给 `smart_arbitrage` helper 增加 `require_explicit_hedge_margin_mode`：
   - 兼容模式下仍允许 fallback
   - engine 主链调用时强制要求显式传值
5. `SmartArbitrageStrategyEngine._evaluate_pair(...)` 在 runtime margin mode 缺失时直接返回 blocked candidate。

## Testing strategy
- unit
  - `tests/unit/test_independent_family.py`
  - `tests/unit/test_smart_arbitrage_v2_components.py`
- integration
  - `tests/integration/test_strategy_runtime_integration.py`

## Acceptance criteria
- lint 通过
- 相关 unit tests 通过
- 最窄 integration tests 通过
- `independent` expectancy 失败不再向后传零值 fallback expectancy 对象
- `smart_arbitrage` engine 主链缺少 runtime `hedge_margin_mode` 时会显式阻断
