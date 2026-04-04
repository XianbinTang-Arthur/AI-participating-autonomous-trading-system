# Task 88: 现货 / 合约 Profile 历史耦合清理

## 目标

把 `spot*.yaml` 和 `derivatives*.yaml` 里历史上共用文件遗留下来的跨运行域配置拆干净，避免：

- 现货 profile 继续维护合约专属参数
- 合约 profile 继续维护现货专属参数
- `/strategy/runtime` 和策略页面继续把这些无关参数暴露给操作端

本次只做最小清理，不重构 `AATSSettings` schema，也不改公开 API 字段名。

## 当前行为摘要

清理前存在三类历史耦合：

1. `spot.yaml` / `spot_live.yaml` 仍显式维护整套 `smart_arbitrage_*` 参数，以及 directional 的 `strategy_short_*`、`strategy_short_bias_enabled`、`strategy_dynamic_leverage_enabled`。
2. `derivatives.yaml` / `derivatives_live.yaml` 仍显式维护 `spot_grid_*` 和 `dca_*`。
3. `/strategy/runtime` 的 `configured_parameters` 会把这些跨运行域参数一起返回，策略页“配置参考”也会照单全收。

## 方案

### 1. Profile 文件清理

- 现货 profile 删除 `smart_arbitrage_*` 和合约专属 directional short 配置。
- 合约 profile 删除 `spot_grid_*` 和 `dca_*`。
- 删除后统一回退到 `settings` 默认值，避免继续把无关配置带进 managed profile。

### 2. Runtime 输出裁剪

- `configured_parameters.directional` 在现货 runtime 下只保留 long/共享阈值。
- `configured_parameters.smart_arbitrage` 仅在合约 runtime 暴露。
- `configured_parameters.spot_grid` / `configured_parameters.dca` 仅在现货 runtime 暴露。

### 3. 策略页参考区裁剪

- 现货 runtime 不再显示智能套利配置卡和成本卡。
- 现货 runtime 的 directional 做空卡改为“能力说明 + long/共享阈值”，不再展示 `strategy_short_*`。

## 兼容性

- 没有修改 `AATSSettings` 字段定义。
- 没有新增或删除接口路由。
- 主要行为变化是 managed profile 和 runtime 展示更加按运行模式分离。

## 风险

- 现货 profile 删除 `smart_arbitrage_*` 后，这些值不再从现货 YAML 覆盖；如果未来真的要让现货重新支持相关链路，需要明确回到合约 profile 或重做独立配置。
- 合约 profile 删除 `spot_grid_*` / `dca_*` 后，任何仍错误读取这些值的隐藏代码路径都会回退到默认值；本次通过 runtime 和页面测试覆盖了主链路，但没有扩大成全仓库配置审计。

## 验证

- `tests/unit/test_env_profiles.py`
- `tests/integration/test_strategy_runtime_integration.py`
- `tests/integration/test_dashboard_ui.py`
