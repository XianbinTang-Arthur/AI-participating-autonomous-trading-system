# 2026-04-21 · Baseline 信号与成本分析

> **结论先行**：
> 系统不交易的**第一原因是 score 阈值**，不是 net_edge 为负。
> 当前 score = 0.018~0.046 < entry_threshold = 0.25 — **信号强度根本没达到门槛**。
> net_edge = -7 bps 是第二道 gate，但主 gate 是 score gate，score 没过就不用看 net_edge。

---

## 完整公式链

```
composite_score =        0.34 × baseline_alpha       (最大权重)
                + 0.24 × momentum_alpha
                + 0.18 × trend_alpha
                + 0.12 × microstructure_alpha
                + 0.12 × confidence (只有 direction_bias 匹配才计)
                + bonus components (当前 mode_A 系数 34.6/20.8/16.6)

expected_signal_edge_bps = composite_score × strategy_signal_edge_scale_bps
                        = 0.018 × 20
                        = **0.36 bps**（实测 0.22 对 long）

expected_cost_bps = fee_bps (~1)
                + slippage_bps (~5.6, = 20 × 0.28)
                + size_impact_bps (~0)
                = **6.0 bps**

expected_net_edge_bps = signal - cost - noise_buffer (4 bps)
                    = 0.36 - 6.0 - 4.0
                    = **-9.64 bps**（实测 -7.78，buffer 可能 1-2 bps）

# Gate sequence
IF score < entry_threshold (0.25):
    → book_state = inactive, book_action = inactive  ← **当前卡在这**
ELSE IF net_edge < safe_threshold (0.0):
    → hard block "net_edge_below_safe_threshold"
ELSE:
    → proceed to trade
```

---

## 关键数字的来源

| 参数 | 当前值 | 配置位置 | 可调 |
|------|--------|---------|------|
| `strategy_signal_edge_scale_bps` | 20 | RDP-pinned | 🟡 RDP calibration 改 |
| `strategy_alpha_edge_bps_scale` | 100.0 | settings.py:567 | ✅ 配置 |
| `strategy_expected_slippage_bps_fraction` | 0.28 | settings.py:578 | ✅ 配置 |
| `strategy_edge_noise_buffer_bps` | 4.0 | settings.py:579 | ✅ 配置 |
| `max_slippage_tolerance_bps` | 20 | settings.py:252 | ✅ 配置（来自 target） |
| `strategy_hedge_independent_long_entry_threshold` | 0.25 | `configs/strategy_profiles/derivatives_live.yaml` | ✅ 配置 |
| `min_safe_net_edge_bps` | 0.0 | settings | ✅ 配置 |
| `entry_execution_mode` | ? | yaml | ✅ 配置（taker / bounded_limit / post_only） |

---

## 为什么信号这么弱

`composite_score ≈ 0.02` 在 mode_A (baseline_only) 下几乎都贡献来自
`baseline.composite_alpha_score ≈ -0.014`（实测最新 outcome）。

**可能的根因**（按概率）：
1. **市场在 `regime=range`** — 当前 BTC-USDT-SWAP 横盘，baseline 的方向性 alpha
   在 range 里会被系统性压低（`baseline_regime_range_threshold_not_met`）
2. **Direction bias 总是 flat** — confidence 需要 leg == bias 才计分，如果
   bias 一直 flat，long/short 两边都只拿默认值（0.5 × 0.12 = 0.06 max）
3. **Microstructure_alpha 很弱** — 因为 market 活跃度不够，微观结构信号也
   没给足 bonus

---

## 给您的 4 个可行方向（不是推荐，是选项）

### 方向 A · 纯调 config（最快 / 最低风险 / 可 rollback）

改 `configs/strategy_profiles/derivatives_live.yaml` 里：

```yaml
# 方案 A1：降低 score 阈值（假设 backtest 支持）
strategy_hedge_independent_long_entry_threshold: 0.15  # 从 0.25 降
strategy_hedge_independent_short_entry_threshold: 0.15

# 方案 A2：降 noise buffer（更激进）
strategy_edge_noise_buffer_bps: 2.0  # 从 4.0 降
```

**前提**：必须先在历史数据上 backtest 验证 "降低 0.25 → 0.15 会不会触发过多
亏损小单"。未做 backtest 直接改是**赌博**。

### 方向 B · 换执行风格让 cost 降下来

```yaml
strategy_hedge_independent_entry_execution_mode: "bounded_limit"  # 可能从 taker
```

- Taker 0.02% (2 bps) → maker 可能有 rebate
- 如果 OKX VIP 等级够 → 甚至可能得 maker rebate (-0.015%)
- 实测需在 cost estimate 里**加上 maker rebate 减法**（当前代码未考虑，见 latent finding）

### 方向 C · 增强信号（慢 / 高不确定性）

改 `aats/services/strategy_engines/independent/scoring.py` 的 composite 权重：
- 增加 `momentum_alpha` 或 `trend_alpha` 权重
- 加入新特征（crypto 特有的：资金费率信号 / 持仓量变化 / 清算热图）

**这是**策略研究**工作，不是配置调整；需要 backtest + paper trade 至少 2 周。

### 方向 D · 开 AI（风险最高但最快试错）

参见 `2026_04_21_ai_shadow_review.md`。最低成本路径是 Stage 0（短窗口 shadow 跑 1 天，看实际账单）。

---

## 诚实的评估

**当前不交易不是 bug**，是系统在一个 **weak signal + conservative cost model**
的组合下正确选择了 hold。3 种解决都有 tradeoff：

- A（调阈值）：最快但可能触发小亏损频繁
- B（调执行）：需要 OKX 账户支持 maker rebate，不是所有品种都有
- C（改策略）：长期正确但需要研究
- D（开 AI）：月成本 ~$127（账户 32%）

**我的直觉推荐**：A 的**反向**思路 — 不是降阈值，而是**保持阈值**但**让 cost
model 更准** —— 把 maker rebate 加进来、把 `strategy_expected_slippage_bps_fraction`
降下来（当前 0.28 意味着预期滑点等于 max_slippage_tolerance 的 28%，可能太保守）。

## Latent findings（已记录到 10_latent_findings.md）

- LF-20260421-021: 成本模型没扣 maker rebate（cost 估高 ~2 bps）
- LF-20260421-022: direction_bias 一直 flat 时 confidence 永远 0.06 封顶（对称性问题）
- LF-20260421-023: score gate 和 net_edge gate 阈值不联动（低 score 永远过不了 score gate，net_edge 优化白做）

---

## 参考

- 信号算法：`aats/services/strategy_engines/independent/scoring.py`
- 成本算法：`aats/services/strategy_engines/independent/economics.py` + `trade_costs.py`
- 门禁逻辑：`aats/services/strategy_engines/independent/gates.py` + `engine.py:284`
- 配置：`configs/strategy_profiles/derivatives_live.yaml`
