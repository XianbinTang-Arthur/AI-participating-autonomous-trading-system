# Task 115: Independent 实盘阈值重校准

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 1. 业务目标与边界

本次只调整 `derivatives_live` 的 `independent` 开仓 / 加仓阈值，让系统保持保守，但不再出现“连续多轮永远不开仓”的配置死区。

本次不做：

- 不改 `independent` 打分公式
- 不改执行、风控、恢复主链
- 不改 `protective / opportunistic` 模式
- 不改数据库结构或公开 API

## 2. 当前行为

实盘运行证据表明，`independent` 最近 120 轮决策里：

- `long_score` 最大值约 `0.309`
- `short_score` 最大值约 `0.326`
- 达到 `0.66` 的次数为 `0`

因此当前 live 配置：

- `strategy_hedge_independent_long_entry_threshold = 0.66`
- `strategy_hedge_independent_short_entry_threshold = 0.66`
- `strategy_hedge_independent_long_scale_in_threshold = 0.66`
- `strategy_hedge_independent_short_scale_in_threshold = 0.66`

在实际运行中等价于“几乎永久不触发”。

## 3. 修改方案

仅调整 [derivatives_live.yaml](/D:/文件/project/AIParticipatingAutonomousTradingSystem/configs/strategy_profiles/derivatives_live.yaml)：

- `long_entry_threshold`: `0.66 -> 0.20`
- `short_entry_threshold`: `0.66 -> 0.20`
- `long_scale_in_threshold`: `0.66 -> 0.28`
- `short_scale_in_threshold`: `0.66 -> 0.28`

选择原则：

- `0.20` 仍显著高于最近 120 轮分布的中位数与 75 分位数，属于保守开仓
- `0.28` 仍高于大部分历史样本，只允许在更强信号下加仓
- 保持 long / short 对称，避免先人为引入方向偏置

## 4. 一致性与风险控制

- 不修改 `independent` 的评分口径，只修 live 阈值与实际分布失配
- `scale_in_threshold >= entry_threshold` 继续成立
- 不影响托管 profile 的 `hedge / independent / smart_arbitrage off` 运行约束
- 不改变 live 账户、风控限额、命令流和恢复语义

## 5. 测试策略

- 更新 `tests/unit/test_env_profiles.py`，确保 `derivatives_live` 托管 profile 加载出的阈值与配置一致
- 跑最窄 unit test 验证配置加载
- 跑最窄 integration test 验证 runtime 仍正确暴露 `independent` 阈值与双书状态

## 6. 回滚

如需回滚，只恢复 [derivatives_live.yaml](/D:/文件/project/AIParticipatingAutonomousTradingSystem/configs/strategy_profiles/derivatives_live.yaml) 中这 4 个阈值。

## 7. 验收标准

- `derivatives_live` 加载后 `independent` 阈值为 `0.20 / 0.20 / 0.28 / 0.28`
- lint 通过
- 相关 unit test 通过
- 最窄 strategy runtime integration test 通过
