# 合约 Overlay 灰度运行手册

## 1. 适用范围

本文档只适用于：

- `trading_product_type=derivatives`
- `derivatives_position_mode=hedge`
- directional 的 `protective / opportunistic / independent` overlay 灰度放开

## 2. 当前阶段约束

- `protective`：始终可作为兜底路径保留。
- `opportunistic`：允许按 `replay_only / dry_run / live` 分阶段放开。
- `independent`：当前阶段只允许 `replay_only / dry_run`，不允许直接放开到实盘。

## 3. 上线前检查

1. 先确认交易所 `posMode=long_short_mode`，且本地 `derivatives_position_mode=hedge`。
2. 先确认 `/strategy/runtime` 的 `configured_parameters.directional.hedge_rollout` 没有当前模式阻断。
3. 至少准备 2 组历史回放样本。
4. 至少准备 1 组 dry-run 观察样本。
5. 再决定是否把 `opportunistic` 放开到实盘。

## 4. 推荐灰度顺序

1. 先在回放里验证 `opportunistic`。
2. 再在 dry-run 观察 `opportunistic`。
3. 确认控制面、审计、恢复页都能解释腿来源后，再决定是否进入 `opportunistic live`。
4. `independent` 先停留在回放 / dry-run，不要直接进实盘。

## 5. 回滚顺序

1. 先关闭 `strategy_hedge_opportunistic_enabled`
2. 再关闭 `strategy_hedge_independent_enabled`
3. 保留 `protective` 作为最后兜底
4. 如需彻底回退，再把 `strategy_hedge_overlay_mode` 切回 `protective`

## 6. 观察重点

- 机会腿与主腿的费耗比例是否显著抬升
- `independent` 两条腿的 trial guard 是否互相污染
- 恢复页是否把腿级异常解释清楚
- operator 审计是否能回答“这条腿为什么开、为什么没关、为什么被拦”
