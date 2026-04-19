# Task 133 / Batch G：Target Position Legacy Overlay 拆出主路径交付

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 本轮范围

- 开始把 [target_position.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/decision_engine/target_position.py) 里的 legacy overlay 内嵌逻辑从主路径上拆出去
- 让 `protective / opportunistic / independent` 在 family live cutover 生效时，不再依赖 `directional` 内部 hedge path 才能运行
- 保持最终 applied target 的 overlay 摘要仍可被 runtime / audit / UI 读取

## 本轮完成

- `TargetPositionEngine` 现在新增了 family cutover 判定：
  - 当 `protective / opportunistic / independent` 对应的 family `enabled + live_execution_enabled` 成立时
  - `_build()` 不再进入 `_hedge_mode_strategy_legs()`
  - 因而不再由 `target_position.py` 生成 legacy overlay legs / `hedge_overlay_decision`
- `StrategyCoordinatorService.apply_selected_target()` 现在会在 selected family 为：
  - `protective`
  - `opportunistic`
  - `independent`
  且 base target 已不再携带 legacy overlay 摘要时，从 selected family candidate 的 metrics 回填最终 `hedge_overlay_decision`
- 因此 final applied target 现在同时满足：
  - overlay family 的执行腿来自 family candidate / allocator / apply 主链
  - runtime / audit / UI 仍能读取 `hedge_overlay_decision`

## 本轮验证

- unit：
  - `protective / opportunistic / independent` 各补 1 条 cutover 旁路测试
  - 确认 family live cutover 生效时，`target_position` 不再生成 legacy overlay legs
- integration：
  - 新增 1 条 mainline 测试
  - 确认 protective family 在真实 `run_cycle` 中可以：
    - 让 raw directional target 保持无 legacy overlay legs
    - 同时由 family candidate 接管并生成最终执行腿与 overlay 摘要

## 本轮未做

- 还没有删除 `target_position.py` 中的 legacy overlay helper 本体
- 还没有把 `protective / opportunistic / independent` 的旧 helper 引用全部移除
- 还没有切 `smart_arbitrage / spot_grid / dca`

## 结果

Batch G 现在完成到“主路径已脱离、旧 helper 仍保留作兼容”的阶段：

- `protective / opportunistic / independent` 已经可以不依赖 `directional` 内嵌 hedge path 运行
- legacy overlay 逻辑仍在代码里，但不再是 family live cutover 下的执行前提
