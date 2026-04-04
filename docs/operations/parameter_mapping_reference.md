# RDP 参数映射参考

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

## Independent Family 映射

| RDP 参数 | 生产字段 | 类型 | 单位 | 说明 |
|----------|---------|------|------|------|
| `signal_edge_scale_bps` | `strategy_hedge_independent_de_risk_net_edge_bps` | DIRECT | bps | 信号净收益边际阈值 |
| `min_confirm_ticks` | `strategy_hedge_independent_min_confirm_ticks` | DIRECT | count | 信号确认最少 tick 数 |
| `min_safe_net_edge_bps` | `strategy_hedge_independent_min_safe_net_edge_bps` | DIRECT | bps | 交易执行的净边际安全线 |
| `score_stability_threshold` | `strategy_hedge_independent_min_score_drawdown_bps` | APPROXIMATE | 见下 | 分数稳定性 → 回撤阈值 |

### score_stability_threshold 映射说明

**RDP 端含义:** Phase 2 回测优化的分数稳定性容忍度，衡量参数在不同市场状态下分数的波动程度。

**生产端含义:** `min_score_drawdown_bps` 是策略分数从峰值回撤到触发 de-risk 操作的 bps 阈值。

**语义差异:**
- RDP 端是"越高越能容忍波动"（稳定性容忍度）
- 生产端是"越高越允许更大回撤"（bps 阈值）
- 方向一致（越高 → 越宽松），但量纲可能不同

**当前假设:** RDP Phase 2 在输出此参数时已将其校准为 bps 量级。如果 RDP 输出的是 0~1 无量纲比率，需要在映射层增加 `× 100` 的换算。

**TODO:** 确认 Phase 2 `score_stability_threshold` 的输出单位。

## Directional Family 映射

| RDP 参数 | 生产字段 | 类型 | 单位 | 说明 |
|----------|---------|------|------|------|
| `directional_trend_weight` | `strategy_entry_alpha_min` | PLACEHOLDER | 见下 | 趋势权重 → 入场 alpha 阈值 |
| `taker_fee_bps` | `trade_cost_derivatives_taker_fee_bps` | DIRECT | bps | taker 手续费 |
| `slippage_bps` | `trade_cost_derivatives_slippage_bps` | DIRECT | bps | 滑点估计 |

### directional_trend_weight 映射说明

**RDP 端含义:** 方向性策略中趋势信号在综合评分中的权重 (0~1)。

**生产端含义:** `strategy_entry_alpha_min` 是入场信号的最小 alpha 阈值（数值越大越严格）。

**语义差异:**
- RDP 端是"趋势信号的权重"（占比概念）
- 生产端是"最低 alpha 要求"（门槛概念）
- 这不是同一个概念

**为什么暂时这样映射:**
第一版中，directional family 的研究主要关注趋势信号强度，`trend_weight` 越高意味着对趋势的依赖越强，间接要求更高的 alpha 才能入场。这是一种间接近似。

**TODO:**
1. 明确 `trend_weight` 和 `alpha_min` 的数学关系
2. 考虑是否需要拆成两个独立参数
3. 或者在 RDP Phase 2 中直接输出 `entry_alpha_min` 参数

## 安全检查清单

在修改映射前，确认:

- [ ] RDP 参数和生产字段的**单位**一致（bps, count, ratio, etc.）
- [ ] RDP 参数和生产字段的**方向**一致（越大越好 vs 越小越好）
- [ ] RDP 参数的**取值范围**在生产字段的合理区间内
- [ ] 如果是 APPROXIMATE/PLACEHOLDER，有明确的换算公式或假设说明
- [ ] 本文档已同步更新

## 未来扩展

### 新增映射步骤

1. 在 `active_parameters.py` 的 `PARAMETER_MAPPING_*` dict 中添加新条目
2. 添加注释说明映射类型和语义关系
3. 在本文档对应 family 表格中添加行
4. 如果是 APPROXIMATE/PLACEHOLDER，写清楚 TODO
5. 在 dev 环境验证映射后的 settings 值合理
