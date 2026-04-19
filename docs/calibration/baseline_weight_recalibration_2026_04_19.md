# Baseline composite_alpha_score 权重重分配后的等分位标定报告

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


生成日期: 2026-04-19  
标的: `BTC-USDT-SWAP`  
采样期间: `2026-03-20 14:45 UTC` ~ `2026-04-19 14:15 UTC`  
有效样本数: `2849` 个 15m bar (前 `30` 根 prewarm, ROC(5)/ATR(14) ready 后开始采集)

## 权重对比

| Alpha 分量 | 旧权重 (P0 前) | 新权重 (P2.7 后) |
|---|---|---|
| momentum | 0.34 | 0.24 |
| trend | 0.22 | 0.17 |
| regime | 0.17 | 0.12 |
| multi_tf | 0.12 | 0.08 |
| micro | 0.15 | 0.09 |
| basis | — | 0.1 |
| funding | — | 0.07 |
| oi | — | 0.07 |
| ls | — | 0.06 |

## `|composite_alpha|` 分布摘要

| 分位 | 旧公式 | 新公式 | 比值 (新/旧) |
|---|---|---|---|
| min | 0.0000 | 0.0000 | 14.11 |
| P5 | 0.0117 | 0.0085 | 0.72 |
| P10 | 0.0243 | 0.0169 | 0.70 |
| P25 | 0.0597 | 0.0418 | 0.70 |
| P50 | 0.1363 | 0.0951 | 0.70 |
| P60 | 0.1728 | 0.1205 | 0.70 |
| P65 | 0.1929 | 0.1350 | 0.70 |
| P70 | 0.2217 | 0.1553 | 0.70 |
| P75 | 0.2474 | 0.1728 | 0.70 |
| P80 | 0.2790 | 0.1960 | 0.70 |
| P85 | 0.3163 | 0.2239 | 0.71 |
| P90 | 0.3546 | 0.2528 | 0.71 |
| P95 | 0.4562 | 0.3242 | 0.71 |
| P99 | 0.5349 | 0.3798 | 0.71 |
| max | 0.6252 | 0.4488 | 0.72 |

## T_old → T_new 映射 (等分位)

查阅方式: `T_old` 在旧分布里对应分位 `q`, 新分布里同 `q` 分位给出 `T_new`。

| Name | T_old | 旧分位 q | T_new | 使用位置 |
|---|---|---|---|---|
| `baseline_breakout` | 0.0800 | 32.1% | 0.0559 | derivatives_live.yaml:209 strategy_baseline_breakout_alpha_threshold |
| `baseline_trend` | 0.1400 | 51.1% | 0.0983 | derivatives_live.yaml:210 strategy_baseline_trend_alpha_threshold |
| `baseline_range` | 0.1600 | 56.9% | 0.1119 | derivatives_live.yaml:211 strategy_baseline_range_alpha_threshold |
| `baseline_uncertain` | 0.2600 | 77.5% | 0.1827 | derivatives_live.yaml:212 strategy_baseline_uncertain_alpha_threshold |
| `alpha_decay_reduce` | 0.1200 | 44.8% | 0.0837 | settings.py:536 strategy_position_alpha_decay_reduce_alpha |
| `alpha_decay_exit` | 0.0600 | 25.2% | 0.0422 | settings.py:538 strategy_position_alpha_decay_exit_alpha |
| `profile_high_vol_ceiling` | 0.4500 | 94.8% | 0.3200 | strategy_profiles.py:899 high_volatility_defensive 触发 |composite| < 0.45 |
| `profile_defensive` | 0.1400 | 51.1% | 0.0983 | strategy_profiles.py:906 range_defensive 触发 |composite| < 0.14 |
| `profile_aggressive` | 0.5500 | 99.3% | 0.3889 | strategy_profiles.py:913 trend_aggressive 触发 composite >= 0.55 |
| `profile_normal` | 0.2400 | 73.6% | 0.1671 | strategy_profiles.py:920 trend_normal 触发 composite >= 0.24 |
| `intent_fit_band_low` | 0.1200 | 44.8% | 0.0837 | strategy_profiles.py:1834 trend_strict 区间下限 |
| `intent_fit_band_high` | 0.2200 | 69.7% | 0.1536 | strategy_profiles.py:1834/1836 trend_strict 上限 / trend_normal 下限 |

## 建议改动

### `configs/strategy_profiles/derivatives_live.yaml`

```yaml
strategy_baseline_breakout_alpha_threshold: 0.0559  # 原 0.0800
strategy_baseline_trend_alpha_threshold: 0.0983  # 原 0.1400
strategy_baseline_range_alpha_threshold: 0.1119  # 原 0.1600
strategy_baseline_uncertain_alpha_threshold: 0.1827  # 原 0.2600
```

### `aats/bootstrap/settings.py` (alpha_decay defaults)

```python
strategy_position_alpha_decay_reduce_alpha: float = 0.0837  # 原 0.1200
strategy_position_alpha_decay_exit_alpha: float = 0.0422  # 原 0.0600
```

### `aats/bootstrap/settings.py` (新增 profile auto-switch 字段, default = 标定值)

```python
strategy_profile_auto_switch_high_vol_alpha_ceiling: float = 0.3200  # 原硬编码 0.4500
strategy_profile_auto_switch_alpha_defensive_threshold: float = 0.0983  # 原硬编码 0.1400
strategy_profile_auto_switch_alpha_aggressive_threshold: float = 0.3889  # 原硬编码 0.5500
strategy_profile_auto_switch_alpha_normal_threshold: float = 0.1671  # 原硬编码 0.2400
strategy_profile_intent_fit_alpha_band_low: float = 0.0837  # 原硬编码 0.1200
strategy_profile_intent_fit_alpha_band_high: float = 0.1536  # 原硬编码 0.2200
```

## 假设与风险

- 回放中 `basis_alpha = 0` (mark_price = last_price, RDP 暂无 mark-price 历史).
- 回放中 `oi_alpha = 0` (RDP 暂无 open-interest 历史).
- 回放中 `ls_alpha = 0` (long-short ratio 依赖 poller, 默认 flag 关).
- `funding_alpha` 使用真实 RDP funding 数据, 每 bar 取最近一条 `silver.market_swap_funding`.
- 场景 B (旧权重) 使用新 ROC(5)/ATR(14) 路径得到的 momentum/trend/regime/multi_tf/micro, 非 P0 前瞬时路径 (任务文档 §二 接受此简化).
- 因此新分布相对真实生产分布偏窄 (缺 basis/oi 贡献) → 标定结果偏紧, 属保守方向.

## 采样元数据

- liquidity_scale 样本 p10=0.846 p50=0.846 p90=0.846
- funding_alpha 均值 -0.0356 (真实贡献, 其余四个 optional alpha 恒 0)

