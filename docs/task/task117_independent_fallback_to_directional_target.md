# Task117 Independent 阻断回退修复

## 目标

- 修复 `independent` 在被禁用或 rollout 阻塞时，错误回退到 `current_net_position_qty` 的问题。
- 保证这类阻断只关闭双书腿级执行，不吞掉原本已经算出来的 `directional_target_qty`。

## 现状问题

当前 `TargetPositionEngine._independent_books_strategy_legs()` 在以下两种场景会提前返回：

- `strategy_hedge_independent_enabled = false`
- `independent` rollout 对当前 runtime 不放行

但返回的 target 是 `context.current_net_position_qty`。  
这会把原本已经生成的 directional 目标仓位直接冻结成当前净仓，导致：

- directional 想开仓或调仓时，被 independent gate 意外吞掉
- 外部看到的是 `independent` 被 block，实质上却连 directional 主线也一起失效

## 修复

- 将上述两个分支的 fallback target 改为 `directional_target_qty`
- 保留：
  - `strategy_execution_legs = []`
  - `hedge_overlay_decision.state = blocked`
  - `guardrail_flags += independent_books_blocked`

## 预期行为

- `independent` 可用：继续按双书腿级逻辑运行
- `independent` 被 block：只停双书腿，不吞掉 directional 主目标

## 验证

- 单测覆盖：
  - `independent` disabled 时回退到 directional target
  - `independent` rollout blocked 时回退到 directional target
- 最窄 integration：
  - runtime rollout blocker 相关路径继续通过
