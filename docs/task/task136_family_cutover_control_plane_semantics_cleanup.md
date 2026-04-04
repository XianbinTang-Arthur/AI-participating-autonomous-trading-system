# Task 136 - Family Cutover 控制面语义收尾

## 目标

- 修复 overlay family cutover 后顶层 `position_intent` 仍沿用净仓位语义的问题
- 清理 family candidate 中残留的 `legacy_execution_owner / directional 承接` 旧归因

## 范围

- `protective / opportunistic / independent` family cutover 的控制面语义
- 不改 allocator 选主逻辑
- 不改真实下单方向、腿级执行或风控

## 变更点

- `StrategyCoordinatorService.apply_selected_target()` 在非 `directional` family 下优先从真实执行腿推导顶层 `position_intent`
- 仅当腿动作无法被单一 `position_intent` 表达时，才回退到旧的净仓位推导
- family candidate metrics 统一暴露 `execution_owner=<family>`
- 删除 `legacy_execution_owner=directional` 残留
- family `control_summary` 不再声称“当前执行仍由 directional 主链承接”

## 验收

- protective / opportunistic cutover 时，顶层 `position_intent` 应显示 `open_short`
- independent cutover 单腿开仓时，顶层 `position_intent` 仍与真实腿动作一致
- family candidate metrics 中不再出现 `legacy_execution_owner`
- lint、相关 unit tests、相关 integration tests 通过
