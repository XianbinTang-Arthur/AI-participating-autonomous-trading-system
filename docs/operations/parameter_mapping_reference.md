# RDP 参数映射参考

> 项目定位声明：本文件服从 [AATS 项目定位声明](../project_positioning.md)。
> 文档状态：现行参考
> 最后核对：2026-08-25（起始 HEAD `00b6df0f8a8d2665d6cae3e88996843767cd1f56`；包含 Phase 3A–3W 整改提交候选）
> 静态真源：`aats/bootstrap/active_parameters.py`、`aats/data_platform/replay/core/replay_context.py`、`aats/bootstrap/settings.py`
> 运行时边界：本文不证明当前数据库 active set、目标进程有效 settings、账户、订单、仓位或 trading-ready 状态

## 1. 用途与裁决规则

本文说明 RDP/replay 参数如何进入生产 `AATSSettings`。唯一可执行映射定义是
`FAMILY_PARAMETER_MAPPINGS`；本文是人工可读参考，发生冲突时以当前代码和测试为准。

生产启动从 PostgreSQL `governance.active_parameter_sets` 读取 active sets，按 family 选择
映射并生成 settings overrides。数据库不可用或数据无效时的具体降级/失败语义必须以
`active_parameters.py` 和启动日志为准，不能从本文推断某个参数已经在运行进程生效。

## 2. 映射分类与失败语义

| 分类 | 含义 | 当前行为 |
|---|---|---|
| required + mapped | 研究参数与生产字段存在明确映射 | 写入对应 settings override |
| required + unmapped | 该 family 必须接入生产但映射缺失 | 记录 ERROR，整个 combo 失败关闭并跳过 |
| non-required + mapped | 对该 family 有有效生产落点，但不是完整研究闭环硬要求 | 写入对应 settings override |
| replay-only | 只用于回放、成本模型或目标配置快照 | 不注入生产 settings，不报映射缺失 |
| 其他 unmapped | 研究层存在、生产无等价消费点 | 记录 INFO 后丢弃，不伪装成已生效 |

当前 required 集合：

- `independent`：下表全部 21 个参数；
- `directional`：只有 `min_hold_seconds`；
- 其他 family：当前未纳入该 RDP 自动发布路径。

## 3. Independent family：21 个 required 映射

### 3.1 信号、稳定性与资格门槛

| RDP 参数 | 生产字段 | 单位/语义 |
|---|---|---|
| `signal_edge_scale_bps` | `strategy_signal_edge_scale_bps` | score 到 bps 的缩放；生产 score-based edge 路径 |
| `score_stability_threshold` | `strategy_hedge_independent_min_score_stability_bps` | score 回撤容忍，bps；生产端可被更专用的 drawdown 字段覆盖 |
| `min_confirm_ticks` | `strategy_hedge_independent_min_confirm_ticks` | 信号确认 tick 数 |
| `min_safe_net_edge_bps` | `strategy_hedge_independent_min_safe_net_edge_bps` | 最小安全净边际，bps |

`signal_edge_scale_bps` 过去曾错误映射到 de-risk threshold；当前已经映射到专用的
`strategy_signal_edge_scale_bps`。`score_stability_threshold` 当前单位已与生产的 `×100`
评分差异语义对齐，不再属于“未映射参数”。

### 3.2 进出场阈值

| RDP 参数 | 生产字段 | 单位/语义 |
|---|---|---|
| `entry_threshold` | `strategy_hedge_independent_long_entry_threshold` | long 开仓评分，ratio |
| `close_threshold` | `strategy_hedge_independent_long_close_threshold` | long 平仓评分，ratio |
| `scale_in_threshold` | `strategy_hedge_independent_long_scale_in_threshold` | long 加仓评分；replay 当前不模拟 scale-in |
| `short_entry_threshold` | `strategy_hedge_independent_short_entry_threshold` | short 开仓评分，ratio |
| `short_close_threshold` | `strategy_hedge_independent_short_close_threshold` | short 平仓评分，ratio |

约束：`close_threshold <= entry_threshold`；short 两个阈值均非空时，
`short_close_threshold <= short_entry_threshold`。映射存在不等于 replay 已验证 scale-in。

### 3.3 持仓生命周期

| RDP 参数 | 生产字段 | 单位/语义 |
|---|---|---|
| `min_hold_seconds` | `strategy_hedge_independent_long_min_hold_seconds` | long 最小持仓秒数 |
| `rebalance_cooldown_seconds` | `strategy_hedge_independent_rebalance_cooldown_seconds` | 平仓后冷却秒数 |
| `max_thesis_age_seconds` | `strategy_hedge_independent_max_thesis_age_seconds` | thesis 最大存活秒数 |

`min_hold_seconds` 当前只映射 long 专属字段；不能据此声称 short 使用完全相同的生产
参数。约束：`min_hold_seconds <= max_thesis_age_seconds`。

### 3.4 风险阈值

| RDP 参数 | 生产字段 | 单位/语义 |
|---|---|---|
| `de_risk_net_edge_bps` | `strategy_hedge_independent_de_risk_net_edge_bps` | 降风险阈值，bps |
| `failed_thesis_net_edge_bps` | `strategy_hedge_independent_failed_thesis_net_edge_bps` | thesis 失效阈值，bps |
| `catastrophic_failed_thesis_buffer_bps` | `strategy_hedge_independent_catastrophic_failed_thesis_buffer_bps` | 灾难性失效额外缓冲，bps |

约束：`failed_thesis_net_edge_bps <= de_risk_net_edge_bps`；灾难性缓冲不得为负。

### 3.5 成本、评分质量与执行

| RDP 参数 | 生产字段 | 单位/语义 |
|---|---|---|
| `expected_slippage_buffer_bps` | `strategy_hedge_independent_expected_slippage_buffer_bps` | 开仓滑点缓冲，bps |
| `expected_execution_buffer_bps` | `strategy_hedge_independent_expected_execution_buffer_bps` | 执行缓冲，bps |
| `max_acceptable_cost_bps` | `strategy_hedge_independent_max_acceptable_cost_bps` | 最大允许单边成本，bps |
| `min_score_drawdown_bps` | `strategy_hedge_independent_min_score_drawdown_bps` | 评分回撤阈值，bps |
| `min_liquidity_quality` | `strategy_hedge_independent_min_liquidity_quality` | 流动性质量，ratio；replay 的简化输入不等价于真实盘口 |
| `limit_offset_bps_entry` | `strategy_hedge_independent_limit_offset_bps_entry` | 开仓限价偏移，bps；replay 当前不模拟订单簿 offset 匹配 |

safe-edge 约束由 `ReplayParameterOverrides.__post_init__()` 执行；具体公式应直接核对
该函数。成本或 limit 参数被传入生产不代表 OHLCV 回测已经证明真实 queue、spread、
impact 或 latency。

## 4. Directional family：3 个实际映射、1 个 required

| RDP 参数 | 生产字段 | 分类 | 单位/语义 |
|---|---|---|---|
| `min_hold_seconds` | `strategy_min_hold_seconds` | required/direct | 全局最小持仓秒数 |
| `taker_fee_bps` | `trade_cost_derivatives_taker_fee_bps` | mapped/direct | 衍生品 taker 费，bps |
| `slippage_bps` | `trade_cost_derivatives_slippage_bps` | mapped/direct | 衍生品滑点，bps |

`directional_trend_weight` **没有**映射到 `strategy_entry_alpha_min`。两者分别是趋势权重
与入场 alpha 门槛，历史 PLACEHOLDER 已因语义不等价撤除。directional replay 使用的
entry/close、trend weight、return clamp 等参数当前没有对应的 directional 专属生产消费点，
不得把 replay 最优值描述成已自动发布到生产。

## 5. Replay-only 与目标配置快照

当前显式 replay-only 白名单如下：

| 参数 | 原因 |
|---|---|
| `cost_config` | replay 成本对象，不是一个生产 settings 字段 |
| `taker_fee_bps`、`slippage_bps` | 可供 replay 使用；仅在 directional 映射表中有生产落点 |
| `directional_trend_weight`、`directional_return_clamp_bps` | directional 回放模型内部参数 |
| `strategy_short_bias_enabled` | FS-015 后的目标 profile 上下文快照；不是按 combo 调优的参数 |

`strategy_short_bias_enabled` 与生产字段同名。independent replay 在它为 `false` 时于 score
history 和 dominant-leg 选择前把 short score 固定为 `0.0`。它不进入 active-parameter
映射，因为 active sets 按 family/timeframe 分片，而生产开关是全局能力开关；允许多个
combo 自动写入会形成顺序相关覆盖。正式 replay 必须显式记录目标 profile 解析后的实际值。

`noise_buffer_bps` 同样是 replay 参数，但当前不是 active-parameter 自动映射项；生产对应
值来自 managed settings 的 `strategy_edge_noise_buffer_bps`。面向生产配置的研究必须记录
两端取值，不能依赖未声明的默认值。

## 6. Replay 默认值边界

`ReplayParameterOverrides` 当前默认值主要面向 derivatives independent replay：

| family | entry | close | scale-in |
|---|---:|---:|---:|
| independent | `0.30` | `0.15` | `0.40` |
| directional（`for_family("directional")`） | `0.45` | `0.20` | `0.55` |

默认值只用于兼容和实验起点，不证明当前数据库 active set 或目标 worker 的有效值。任何
正式结论都应保存完整 `parameter_overrides`、dataset version、时间窗、代码 commit 和
目标 profile 上下文。

## 7. 变更检查清单

修改映射或 replay 参数时至少确认：

- [ ] 参数单位、方向、范围和空值语义与生产消费点一致；
- [ ] required 集合、映射表和 `_RDP_REPLAY_ONLY_PARAMS` 没有互相掩盖真实断链；
- [ ] 全局生产开关没有被多个 family/timeframe combo 以不确定顺序覆盖；
- [ ] `ReplayParameterOverrides.to_dict()/from_dict()` 可无损复现类型和值；
- [ ] 未模拟的 scale-in、盘口、limit offset、真实流动性没有被写成已验证；
- [ ] 本文、代码注释、相关测试和审计状态同步更新；
- [ ] 运行 focused、active-parameter、replay/backtest、全量 unit 与 Ruff；
- [ ] 目标环境有效值仍由受控运行时读回，不由静态文档推断。

FS-015 的设计边界与验收条件见
[`fs_015_replay_short_bias_parity_sow_2026_08_25.md`](../task/fs_015_replay_short_bias_parity_sow_2026_08_25.md)。
