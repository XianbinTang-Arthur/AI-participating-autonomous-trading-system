# Task 132 / Batch F：Protective 与 Opportunistic Family Cutover Delivery

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 本轮范围

- 让 `protective / opportunistic` 真正进入 allocator / apply 主路径
- 统一 top-level control plane 与 leg-level execution plane 语义
- 开始清理 legacy `directional` 内嵌 overlay 路径对主链的影响

## 本轮完成项

- `protective family` 与 `opportunistic family` 现在会产出可切流的真实 candidate：
  - `selectable`
  - `route_action`
  - `family_action`
- `coordinator` 现在支持按当前 `strategy_hedge_overlay_mode` 做 overlay family cutover：
  - `protective`
  - `opportunistic`
  - `independent`
- 当 overlay family 切流生效时：
  - `directional` 会被 shadow 成 `hold_current`
  - legacy overlay legs 不再继续通过 `directional` 主路径重复下发
- `allocator` 现在会在衍生品主线上审批：
  - `protective`
  - `opportunistic`
  - `independent`
  并在需要时阻断 `directional`
- `apply_selected_target()` 现在会优先根据真实执行腿推导：
  - `final_action`
  - `final_direction`
  不再只依赖抽象 `family_action`
- 非 `directional` overlay sleeve 的当前仓位/目标仓位语义已补齐，`hold_current` 不再因缺失 inventory 失真
- 托管 profile 里新增了显式开关：
  - `strategy_family_protective_enabled`
  - `strategy_family_protective_live_execution_enabled`
  - `strategy_family_opportunistic_enabled`
  - `strategy_family_opportunistic_live_execution_enabled`

## 对 legacy directional 路径的清理边界

本轮只做了“切流时 shadow directional”这一层清理：

- `target_position.py` 里的 legacy overlay 评估逻辑仍然保留
- 但当 family cutover 生效时，真正进入 allocator / apply / execution 的主路径已不再由 `directional` 承接

## 本轮未做

- 未切 `protective / opportunistic` 的执行算法重写
- 未移除 `target_position.py` 里 legacy overlay helper
- 未切 `smart_arbitrage / spot_grid / dca` 的选择规则
- 未做完整的 legacy overlay dead-code 删除
