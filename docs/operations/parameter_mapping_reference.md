# RDP 参数映射参考

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 概述

本文档说明 RDP 研究层参数名与主系统 `AATSSettings` 字段名之间的映射关系。
映射定义在 `aats/bootstrap/active_parameters.py` 的 `FAMILY_PARAMETER_MAPPINGS` 中。

**修改映射时，必须同步更新本文档。**

## 映射类型

| 标记 | 含义 | 风险等级 |
|------|------|---------|
| `[DIRECT]` | 同义映射，RDP 参数与生产字段描述同一概念，单位一致 | 低 |
| `[APPROXIMATE]` | 近似映射，语义接近但不完全等同，需要换算关系 | 中 |
| `[PLACEHOLDER]` | 第一版占位，语义对接尚未确认，需后续验证 | 高 |

## 未映射的 RDP 内部参数

以下参数仅在 RDP replay 引擎内部使用，**不注入生产配置**：

| RDP 参数 | 原因 |
|----------|------|
| `signal_edge_scale_bps` | score → bps 缩放系数，生产端无等价概念。原映射到 `de_risk_net_edge_bps` 是语义错误（scale=15 → de_risk=15bps，远超正常 net_edge 2-3bps） |
| `score_stability_threshold` | replay 内部稳定性容忍度（无量纲）。原映射到 `min_score_drawdown_bps` 是语义错误（RDP 值 2.0 → 生产 2.0bps，正常信号波动 3-5bps 就会被 block） |

## Independent Family 映射

共 18 个映射，分为 6 个主题组。

### 原有映射（Phase 2）

| RDP 参数 | 生产字段 | 类型 | 单位 | 说明 |
|----------|---------|------|------|------|
| `min_confirm_ticks` | `strategy_hedge_independent_min_confirm_ticks` | DIRECT | count | 信号确认所需最少 tick 数 |
| `min_safe_net_edge_bps` | `strategy_hedge_independent_min_safe_net_edge_bps` | DIRECT | bps | 交易执行的净边际安全线 |

### 进出场阈值

| RDP 参数 | 生产字段 | 类型 | 单位 | 说明 |
|----------|---------|------|------|------|
| `entry_threshold` | `strategy_hedge_independent_long_entry_threshold` | DIRECT | ratio 0~1 | 开仓评分阈值（long） |
| `close_threshold` | `strategy_hedge_independent_long_close_threshold` | DIRECT | ratio 0~1 | 平仓评分阈值（long） |
| `scale_in_threshold` | `strategy_hedge_independent_long_scale_in_threshold` | DIRECT | ratio 0~1 | 加仓评分阈值（long）。⚠️ **REPLAY 未模拟**：replay 无 scale-in 逻辑 |
| `short_entry_threshold` | `strategy_hedge_independent_short_entry_threshold` | DIRECT | ratio 0~1 | 开仓评分阈值（short，非对称） |
| `short_close_threshold` | `strategy_hedge_independent_short_close_threshold` | DIRECT | ratio 0~1 | 平仓评分阈值（short，非对称） |

**约束**: `close_threshold <= entry_threshold`，`short_close_threshold <= short_entry_threshold`

### 持仓时间管理

| RDP 参数 | 生产字段 | 类型 | 单位 | 说明 |
|----------|---------|------|------|------|
| `min_hold_seconds` | `strategy_hedge_independent_long_min_hold_seconds` | DIRECT | seconds | 最小持仓秒数。⚠️ 仅映射 long 方向 |
| `rebalance_cooldown_seconds` | `strategy_hedge_independent_rebalance_cooldown_seconds` | DIRECT | seconds | 平仓后冷却秒数 |
| `max_thesis_age_seconds` | `strategy_hedge_independent_max_thesis_age_seconds` | DIRECT | seconds | thesis 最长存活秒数 |

### 风险管理阈值

| RDP 参数 | 生产字段 | 类型 | 单位 | 说明 |
|----------|---------|------|------|------|
| `de_risk_net_edge_bps` | `strategy_hedge_independent_de_risk_net_edge_bps` | DIRECT | bps | 净边际变薄时触发降风险 |
| `failed_thesis_net_edge_bps` | `strategy_hedge_independent_failed_thesis_net_edge_bps` | DIRECT | bps | 净边际低于此值 thesis 失效退出 |

**约束**: `failed_thesis_net_edge_bps <= de_risk_net_edge_bps`

### 成本缓冲

| RDP 参数 | 生产字段 | 类型 | 单位 | 说明 |
|----------|---------|------|------|------|
| `expected_slippage_buffer_bps` | `strategy_hedge_independent_expected_slippage_buffer_bps` | DIRECT | bps | 开仓预期滑点缓冲 |
| `expected_execution_buffer_bps` | `strategy_hedge_independent_expected_execution_buffer_bps` | DIRECT | bps | 开仓执行缓冲 |
| `max_acceptable_cost_bps` | `strategy_hedge_independent_max_acceptable_cost_bps` | DIRECT | bps | 最大允许单边成本 |

### 评分质量

| RDP 参数 | 生产字段 | 类型 | 单位 | 说明 |
|----------|---------|------|------|------|
| `min_score_drawdown_bps` | `strategy_hedge_independent_min_score_drawdown_bps` | DIRECT | bps | 评分最大回撤容忍度 |
| `min_liquidity_quality` | `strategy_hedge_independent_min_liquidity_quality` | APPROXIMATE | ratio 0~1 | 最低流动性质量分。⚠️ replay 默认 liq=1.0 |

### 执行策略

| RDP 参数 | 生产字段 | 类型 | 单位 | 说明 |
|----------|---------|------|------|------|
| `limit_offset_bps_entry` | `strategy_hedge_independent_limit_offset_bps_entry` | DIRECT | bps | 限价偏移。⚠️ **REPLAY 未模拟** |

## Directional Family 映射

| RDP 参数 | 生产字段 | 类型 | 单位 | 说明 |
|----------|---------|------|------|------|
| `directional_trend_weight` | `strategy_entry_alpha_min` | PLACEHOLDER | 见下 | 趋势权重 → 入场 alpha 阈值 |
| `taker_fee_bps` | `trade_cost_derivatives_taker_fee_bps` | DIRECT | bps | taker 手续费 |
| `slippage_bps` | `trade_cost_derivatives_slippage_bps` | DIRECT | bps | 滑点估计 |

### directional_trend_weight 映射说明

**RDP 端含义:** 方向性策略中趋势信号在综合评分中的权重 (0~1)。

**生产端含义:** `strategy_entry_alpha_min` 是入场信号的最小 alpha 阈值（数值越大越严格）。

**语义张力:**
- RDP 端是「趋势信号的权重」（占比概念）
- 生产端是「最低 alpha 要求」（门槛概念）
- 这不是同一个概念

**为什么暂时这样映射:**
第一版中，directional family 的研究主要关注趋势信号强度，`trend_weight` 越高意味着对趋势的依赖越强，间接要求更高的 alpha 才能入场。这是一种间接近似。

**TODO:**
1. 明确 `trend_weight` 和 `alpha_min` 的数学关系
2. 考虑是否需要拆成两个独立参数
3. 或者在 RDP 中直接输出 `entry_alpha_min` 参数

### directional 家族默认阈值说明

directional 适配器原始硬编码默认值与 independent **不同**：
- directional: `entry_threshold=0.45`, `close_threshold=0.20`
- independent: `entry_threshold=0.25`, `close_threshold=0.15`, `scale_in_threshold=0.25`
  (2026-04-19 calibration + Round 3 下调；旧值 entry 0.40 / scale_in 0.40 已失效)

使用 `ReplayParameterOverrides.for_family("directional")` 获取正确默认值。

**⚠️ 运维操作员注意**：approve 任何关于 `entry_threshold` / `scale_in_threshold` /
`composite_alpha` 的 RDP parameter_upgrade recommendation **前**，必须核对该
recommendation 的 source_round 是否基于 2026-04-19 之后的 9-alpha baseline 分布。
否则可能误覆盖当前 calibration 结果。详见
`docs/calibration/baseline_weight_recalibration_2026_04_19.md` 与
`docs/review/allocator_budget_zero_root_cause_2026_04_19.md`。

## 安全检查清单

在修改映射前，确认:

- [ ] RDP 参数和生产字段的**单位**一致（bps, count, ratio 等）
- [ ] RDP 参数和生产字段的**方向**一致（越大越好 vs 越小越好）
- [ ] RDP 参数的**取值范围**在生产字段的合理区间内
- [ ] 如果是 APPROXIMATE/PLACEHOLDER，有明确的换算公式或假设说明
- [ ] 本文档已同步更新
- [ ] `failed_thesis_net_edge_bps <= de_risk_net_edge_bps` 约束满足
- [ ] `close_threshold <= entry_threshold` 约束满足
- [ ] 标注为「REPLAY 未模拟」的参数不作为回测结论的依据

## 新增映射步骤

1. 在 `active_parameters.py` 的 `PARAMETER_MAPPING_*` dict 中添加新条目
2. 添加注释说明映射类型（DIRECT/APPROXIMATE/PLACEHOLDER）和语义关系
3. 在本文档对应 family 表格中添加行
4. 如果是 APPROXIMATE/PLACEHOLDER，写清楚 TODO
5. 如果参数有约束关系，在 `ReplayParameterOverrides.__post_init__()` 中添加校验
6. 如果 replay 不模拟该参数，标注「REPLAY 未模拟」
7. 在 dev 环境验证映射后的 settings 值合理
