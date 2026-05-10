# 合约 Overlay 灰度运行手册

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 1. 适用范围

本文档只适用于：

- `trading_product_type=derivatives`
- `derivatives_position_mode=hedge`
- directional 的 `independent` overlay 灰度放开

## 2. 当前阶段约束

- `protective` / `opportunistic`：已退役，运行配置不再允许启用；历史 payload 仅保留只读兼容。
- `independent`：当前仓库已经具备回放、恢复、审计与主链接管闭环；`derivatives_live` 现阶段允许直接作为实盘主策略运行。

## 3. 上线前检查

1. 先确认交易所 `posMode=long_short_mode`，且本地 `derivatives_position_mode=hedge`。
2. 先确认 `/strategy/runtime` 的 `configured_parameters.directional.hedge_rollout` 没有当前模式阻断。
3. 至少准备 2 组历史回放样本。
4. 至少准备 1 组 dry-run 观察样本。
5. 再确认 `strategy_family_active=independent`、`strategy_hedge_overlay_mode=independent`、`strategy_family_auto_selection_enabled=false`。
6. 再确认 live 数据库已经应用到当前最新 migration，至少包含 `execution_attempt_id` 相关列。

## 4. 推荐灰度顺序

1. 先确认 `independent` 的 runtime / replay / recovery 摘要链正常。
2. 再以 `derivatives_live` 显式 `independent` 主策略运行。
3. 不再对 `protective` / `opportunistic` 做新灰度；如需恢复，必须重新走设计和代码评审。

## 5. 回滚顺序

1. 先把 `strategy_family_active` 切回 `directional`
2. 再关闭 `strategy_hedge_independent_enabled`
3. 确认 `strategy_hedge_overlay_enabled=false`
4. 如需进一步收紧，再关闭 `strategy_family_independent_live_execution_enabled`

## 6. 观察重点

- `independent` 两条腿的 trial guard 是否互相污染
- runtime / replay / recovery 看到的主策略家族是否一致为 `independent`
- live 数据库 schema 是否仍落后于当前代码版本
- 恢复页是否把腿级异常解释清楚
- operator 审计是否能回答“这条腿为什么开、为什么没关、为什么被拦”
