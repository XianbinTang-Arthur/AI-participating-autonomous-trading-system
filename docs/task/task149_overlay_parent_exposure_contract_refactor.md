# Task 149 - Overlay Parent Exposure Contract Refactor

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## Scope
- 将 `protective / opportunistic` 从“各自读取 directional target 字段，再在状态机里局部 inventory fallback”收敛成显式的 parent-exposure contract。
- 保持 allocator / apply / execution 主链不变，不改 overlay family 依附主腿的业务边界。

## What changed
- 当时在 `protective_family.py` 新增 `OverlayParentExposureContract`；该历史文件后续被移除，当前兼容类型位于 [overlay_parent_exposure.py](../../aats/services/strategy_engines/overlay_parent_exposure.py)。
- 新增 `_resolve_overlay_parent_exposure_contract(...)`，集中解析：
  - `symbol`
  - `target_leverage`
  - `margin_mode`
  - `target_long_qty / target_short_qty`
  - `current_long_qty / current_short_qty`
  - `target_signal / current_signal / effective_signal`
  - `signal_source`
- `protective_candidate_from_directional_target(...)` 与 `opportunistic_candidate_from_directional_target(...)` 现在都优先消费该 contract。
- `evaluate_protective_overlay_decision(...)` / `evaluate_opportunistic_overlay_decision(...)` 现在直接接受 `parent_exposure`，不再要求调用方只传 `long_target_qty / short_target_qty`。
- 原 `_resolve_overlay_main_leg_contract(...)` 仍保留为兼容壳，返回 legacy 子集。

## Behavior impact
- 当 directional target 变 flat，但真实 residual inventory 仍存在时：
  - overlay family 不再是“先 directional、后局部 fallback”的临时逻辑
  - 而是通过统一 parent-exposure contract 显式表达“target flat + inventory long/short”的父腿状态
- candidate metrics 现在会额外暴露：
  - `parent_exposure_signal_source`
  - `parent_target_signal`
  - `parent_current_signal`
  - `parent_effective_signal`

## Validation
- `tests/unit/test_protective_family.py`
  - 覆盖 parent-exposure contract 在 `target flat + residual inventory` 下优先保留 inventory signal
- `tests/unit/test_opportunistic_family.py`
  - 覆盖 opportunistic 状态机直接消费 `parent_exposure`
- `tests/integration/test_mainline_chain.py`
  - 复核 protective / opportunistic cutover 主链仍可运行
