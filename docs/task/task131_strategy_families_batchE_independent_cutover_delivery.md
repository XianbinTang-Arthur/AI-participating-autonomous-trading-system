# Task 131 / Batch E：Independent Family Cutover Delivery

## 本轮范围

- 让 `independent family` 真正进入 allocator / apply 主路径
- 统一 top-level control plane 与 leg-level execution plane 语义
- 让 `weak_edge_execution_mode / passive_first_enabled` 落到真实 planner/executor 参数翻译链

## 本轮完成项

- `coordinator` 现在支持在 `directional` 固定配置下，对 `independent family` 做兼容切流
- `allocator` 现在能在衍生品主线上审批 `independent`，并把旧 `directional` 影子化
- `apply_selected_target()` 现在会写回：
  - `strategy_family_action`
  - `decision_outcome.selected_strategy_family_action`
  - 与 family action 对齐的 `decision_outcome.final_action`
- `independent` 的 weak-edge report-only 模式现在会给腿级执行意图打上 passive-first 偏好
- `execution planner` 现在会把显式腿级执行偏好翻译成真实 `limit / bounded_limit_ioc`
- `derivatives_live` 托管配置现在显式开启：
  - `strategy_family_independent_enabled`
  - `strategy_family_independent_live_execution_enabled`

## 仍未切的范围

- `protective / opportunistic` 仍然没有进入 allocator / apply 主路径
- `independent` 的 passive-first 只是落到 planner / order intent，不包含新的撮合算法
- legacy directional path 仍然保留，尚未清理
