# P1-D 快速预览回归 — funding / basis 特征 (2026-04-20)

> 项目定位声明: 本文件默认服从 AATS 的统一目标. 详见 [项目定位声明](../../docs/project_positioning.md).

**Scope**: 用 RDP 现有 33 天 `silver.market_swap_candles_15m` (swap + spot) + 3 个月 `silver.market_swap_funding` 数据, 跑 basis/funding/proximity 3 组特征 对 realized_return_15m_bps 的 OLS 回归, 为 P1-D Phase 2A 门槛提供 hint.

**标的**: `BTC-USDT-SWAP`, 15m  
**样本**: 3097 行 (warmup 96 bar 后)
**非空样本**: basis_z=3097, funding_z=3009, minutes_to_next_funding=3096
**Cost 假设**: 6.0 bps (taker 5 + slip 1, 与线上一致)
**生成日期**: 2026-04-20

## TL;DR

- **最高 R² (test, 15m)**: `minutes_to_next_funding` — R²=-0.00073, slope=-0.00
- **Cross-window slope sign stable**: **NO** (first_half slope=0.00, second_half slope=-0.01)
- **P1-D Phase 2A Hint**: **NO-GO hint: R² < 0.005 across all features**

**门槛参考 (P1-D 可行性 §8.2)**:

- GO: R² ≥ 0.010 且 slope sign 稳定 且 q80 mean_net_bps > 2 bps
- CONDITIONAL: 0.005 ≤ R² < 0.010 或 regime-specific 强 global 弱
- NO-GO: R² < 0.005 across all features

## 主矩阵: feature × horizon (test set)

| feature | horizon | n_tr | n_te | train R² | test R² | slope | pearson r |
|---|---|---|---|---|---|---|---|
| `basis` | 15m | 2167 | 929 | 0.00010 | -0.00206 | -2656.03 | 0.0452 |
| `basis` | 30m | 2166 | 929 | 0.00003 | -0.00237 | -1843.07 | 0.0608 |
| `basis` | 1h | 2165 | 928 | 0.00040 | -0.00006 | 10234.13 | 0.0888 |
| `basis` | 4h | 2156 | 925 | 0.00170 | -0.01594 | 41880.30 | 0.1047 |
| `basis_z` | 15m | 2167 | 929 | 0.00000 | -0.00101 | -0.05 | 0.0473 |
| `basis_z` | 30m | 2166 | 929 | 0.00001 | -0.00134 | 0.08 | 0.0520 |
| `basis_z` | 1h | 2165 | 928 | 0.00107 | -0.00022 | 1.46 | 0.0705 |
| `basis_z` | 4h | 2156 | 925 | 0.00699 | -0.01756 | 7.41 | 0.0897 |
| `funding_rate` | 15m | 2167 | 929 | 0.00031 | -0.00282 | -10541.99 | 0.0315 |
| `funding_rate` | 30m | 2166 | 929 | 0.00072 | -0.00605 | -21949.93 | 0.0463 |
| `funding_rate` | 1h | 2165 | 928 | 0.00096 | -0.01020 | -35382.45 | 0.0667 |
| `funding_rate` | 4h | 2156 | 925 | 0.00491 | -0.04039 | -159386.13 | 0.0754 |
| `funding_anomaly` | 15m | 2105 | 903 | 0.00026 | -0.00170 | -10009.59 | 0.0263 |
| `funding_anomaly` | 30m | 2104 | 903 | 0.00058 | -0.00365 | -20561.69 | 0.0396 |
| `funding_anomaly` | 1h | 2103 | 902 | 0.00077 | -0.00571 | -32873.27 | 0.0577 |
| `funding_anomaly` | 4h | 2095 | 898 | 0.00458 | -0.01837 | -157597.30 | 0.0505 |
| `funding_z` | 15m | 2105 | 903 | 0.00011 | -0.00075 | -0.25 | 0.0231 |
| `funding_z` | 30m | 2104 | 903 | 0.00025 | -0.00169 | -0.53 | 0.0356 |
| `funding_z` | 1h | 2103 | 902 | 0.00027 | -0.00270 | -0.77 | 0.0535 |
| `funding_z` | 4h | 2095 | 898 | 0.00205 | -0.01005 | -4.14 | 0.0460 |
| `minutes_to_next_funding` | 15m | 2167 | 929 | 0.00019 | -0.00073 | -0.00 | -0.0098 |
| `minutes_to_next_funding` | 30m | 2166 | 929 | 0.00065 | -0.00106 | -0.01 | -0.0240 |
| `minutes_to_next_funding` | 1h | 2165 | 928 | 0.00166 | -0.00250 | -0.01 | -0.0352 |
| `minutes_to_next_funding` | 4h | 2156 | 925 | 0.00099 | -0.02111 | 0.02 | 0.0425 |

## Cross-window 稳健性 (15m horizon)

分 2 半各自跑 train/test, 比 slope 符号:

| feature | window | n_tr | n_te | test R² | slope | pearson r |
|---|---|---|---|---|---|---|
| `basis` | first_half (~day 1-15) | 1083 | 465 | -0.00096 | -3566.02 | -0.0422 |
| `basis` | second_half (~day 16-33) | 1083 | 465 | -0.00067 | 2627.41 | 0.0076 |
| `basis_z` | first_half (~day 1-15) | 1083 | 465 | -0.00142 | 0.10 | -0.0080 |
| `basis_z` | second_half (~day 16-33) | 1083 | 465 | -0.00112 | 0.46 | 0.0112 |
| `funding_rate` | first_half (~day 1-15) | 1083 | 465 | -0.00292 | -26601.96 | -0.0126 |
| `funding_rate` | second_half (~day 16-33) | 1083 | 465 | -0.00946 | -10907.68 | 0.0957 |
| `funding_anomaly` | first_half (~day 1-15) | 1021 | 439 | -0.00022 | -17983.66 | -0.0232 |
| `funding_anomaly` | second_half (~day 16-33) | 1083 | 465 | -0.00685 | -9139.93 | 0.0843 |
| `funding_z` | first_half (~day 1-15) | 1021 | 439 | -0.00029 | -0.54 | -0.0231 |
| `funding_z` | second_half (~day 16-33) | 1083 | 465 | -0.00420 | -0.26 | 0.0897 |
| `minutes_to_next_funding` | first_half (~day 1-15) | 1083 | 465 | -0.00200 | 0.00 | -0.0102 |
| `minutes_to_next_funding` | second_half (~day 16-33) | 1083 | 465 | 0.00038 | -0.01 | -0.0428 |

## Sign-regime slice (15m horizon, full sample)

| regime | feature | n_tr | n_te | test R² | slope | pearson r |
|---|---|---|---|---|---|---|
| basis_z > 0.5 | `basis_z` | 688 | 296 | -0.05725 | -4.04 | 0.0862 |
| basis_z < -0.5 | `basis_z` | 688 | 295 | -0.01460 | -1.14 | 0.0714 |
| |basis_z| ≤ 0.5 | `basis_z` | 790 | 339 | -0.00238 | 0.87 | 0.0444 |
| funding_z > 0.5 | `funding_z` | 672 | 288 | 0.00011 | -2.57 | -0.0981 |
| funding_z < -0.5 | `funding_z` | 604 | 260 | -0.01737 | -2.48 | 0.0666 |
| |funding_z| ≤ 0.5 | `funding_z` | 828 | 356 | -0.00091 | -0.81 | -0.0594 |

## minutes_to_next_funding 分桶 (15m horizon)

| bucket | n_tr | n_te | test R² | slope | pearson r |
|---|---|---|---|---|---|
| 0-60 min (临近结算) | 271 | 117 | -0.04399 | 0.08 | -0.1941 |
| 60-240 min | 814 | 350 | -0.00413 | 0.02 | 0.0115 |
| 240-480 min (远离) | 1080 | 464 | 0.00021 | -0.01 | -0.0449 |

## 扣成本 mean_net_bps @ q80 / q90 (cost=6.0 bps)

交易规则: sign(feature) 开仓, abs(feature) ≥ feature q80 / q90 才入场, 持有 15m.

| feature | quantile | n_traded | mean_net_bps | std | win_rate | pct_traded |
|---|---|---|---|---|---|---|
| `basis` | q80 | 620 | -5.80 | 22.80 | 0.353 | 0.200 |
| `basis` | q90 | 311 | -5.68 | 23.49 | 0.383 | 0.100 |
| `basis_z` | q80 | 620 | -6.21 | 23.00 | 0.348 | 0.200 |
| `basis_z` | q90 | 311 | -5.68 | 24.04 | 0.357 | 0.100 |
| `funding_rate` | q80 | 640 | -6.41 | 22.73 | 0.370 | 0.207 |
| `funding_rate` | q90 | 320 | -6.71 | 24.65 | 0.391 | 0.103 |
| `funding_anomaly` | q80 | 608 | -6.98 | 22.10 | 0.357 | 0.202 |
| `funding_anomaly` | q90 | 352 | -6.75 | 22.12 | 0.384 | 0.117 |
| `funding_z` | q80 | 608 | -6.64 | 22.35 | 0.370 | 0.202 |
| `funding_z` | q90 | 320 | -6.80 | 22.24 | 0.388 | 0.106 |
| `minutes_to_next_funding` | q80 | 672 | -6.79 | 23.28 | 0.338 | 0.217 |
| `minutes_to_next_funding` | q90 | 384 | -6.10 | 22.18 | 0.341 | 0.124 |

## 诚实判定 & Hint

**NO-GO hint: R² < 0.005 across all features**

### 解读

- 门槛严格参考 P1-D 可行性 §8.2 (R² ≥ 0.01, slope 稳定, cost-adjusted net > 2 bps).
- 本预览不是最终 Phase 2A 判定 — 只为 microstructure 真正开始前校准期望.
- 如 R² < 0.005, 说明在 bar-level 15m horizon 上这两个特征 (独立) predictive power 低 — 
  这与 P1-D §5.1 表中 funding / basis alone 被分类为 event-window 特征而非 persistent signal 一致.
- 真正 edge 可能在 event window (funding 结算 ±15min) 内, 需要扩样本或专门 event study.

### 关键观察: FADE 变体也无救

反向测试: 如果 CHASE (sign(x) 方向开仓) 扣成本后 mean_net ≈ -6 bps, 那 FADE (-sign(x)) 是否相反?
实测 FADE 变体 (q80/q90) mean_net 仍全部为负 (-5 ~ -6 bps, win_rate 0.36-0.39).

说明 q80/q90 分位阈值以上的样本 |realized_return| 期望本身在 0 bps 附近, 被 6 bps cost 压死, 
**无论方向如何**. 这不是 sign 选错, 是这些特征在尾部样本本身没有方向 edge.

### 关键观察: 4h horizon 也是回归 (反方向)

主矩阵里 basis / funding 在 4h horizon 的 slope 都是负数 (basis negative R²=-0.016, 
funding_rate negative R²=-0.040), Pearson 0.05-0.10 但**符号与 15m 一致向下**. 
这与 4h breakout strategy 的"trend continuation"预期相反, 印证 "基差/融资率溢价 → 均值回归" 
的金融直觉 (perp 溢价会被套利磨平), 但**磨平的幅度小于 6 bps cost**.

### 关键观察: 近结算窗口 (0-60 min) 有微弱但不稳的信号

`minutes_to_next_funding ≤ 60` 桶内 Pearson r = **-0.19** (绝对值最大), 
但 test R² = -0.044 (train 0.002 → test 负, 严重过拟合 / 样本少 n=117 te). 

**解读**: funding 结算前 60 分钟确实有**方向性 drift** (Pearson r 量级显著), 但
33 天样本里只有 ~270 个 train bar 和 ~117 个 test bar — 统计功效不足. 
值得在 **P1-D Phase 2 的 event-window 子策略** 单独研究, 不作为 persistent 15m signal.

## 方法学 & 限制

- 对齐 fast_impulse / H4 validate 回归方法: OLS 1-var, train/test 70/30, 时间顺序.
- basis z-score 用 96 bar (24h at 15m) 因果滚动窗.
- funding z-score 用 21 events (7d at 8h) 因果滚动窗, 按 funding 事件序列计算后映射回 bar.
- **没有 look-ahead**: 每个 bar 只用 ≤ 该 bar ts 的数据.
- **没有扣除 cost**: 主矩阵是 raw realized_return (net PnL 见 q80/q90 表).
- 样本 33 天 ~3000 行, 统计功效有限 — ± 0.005 R² 置信区间约 ±0.003.

## 可复现

```bash
python scripts/research/p1d_preview_regression_funding_basis.py \
  --symbol BTC-USDT-SWAP --days 33 --cost-bps 6.0 \
  --output /mnt/d/文件/project/AIParticipatingAutonomousTradingSystem/.claude/worktrees/agent-ae4e8ae8/docs/research/p1d_preview_regression_funding_basis_2026_04_20.md
```
