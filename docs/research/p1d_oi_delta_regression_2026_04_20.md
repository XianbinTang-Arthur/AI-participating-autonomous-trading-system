# P1-D Stage 5 OI Delta × sign(ΔP) 回归 (2026-04-20)

> 项目定位声明: 本文件默认服从 AATS 的统一目标. 详见 [项目定位声明](../../docs/project_positioning.md).

**Scope**: 用本次 Stage 5 OKX REST 回填的 60 天 `bronze.market_oi_history_1h` + `silver.market_swap_candles_1h` (从 15m 聚合或原生) 做 OI delta × sign(ΔP) 对 forward realized return 的 OLS 回归, 为 P1-D Phase 2A 门槛提供 hint.

**标的**: `BTC-USDT-SWAP`, 1h bar (OI 原生粒度) 
**样本**: 1424 行 (warmup 1 bar 后; 实际 OI 深度受限于 OKX ~60 天)
**Cost 假设**: 6.0 bps (taker 5 + slip 1, 与线上一致)
**生成日期**: 2026-04-20

## TL;DR

- **最高 R² (test)**: `abs_oi_delta` @ 1h — R²=-0.00240, slope=1.26
- **Cross-window slope sign stable (1h)**: **NO** (first=-165.02, second=149.09)
- **q80 扣成本 mean_net_bps**: -4.34 bps (n=286)
- **P1-D Phase 2A Hint**: **NO-GO hint: R² < 0.005**

**门槛参考 (P1-D 可行性 §8.2)**:

- GO: R² ≥ 0.010 且 cross-window sign 稳定 且 q80 mean_net_bps > 2 bps
- CONDITIONAL: 0.005 ≤ R² < 0.010 或 regime-specific 强 global 弱
- NO-GO: R² < 0.005 across all features & horizons

## 主矩阵: feature × horizon (test set)

| feature | horizon | n_tr | n_te | train R² | test R² | slope | pearson r |
|---|---|---|---|---|---|---|---|
| `signed_oi_delta` | 1h | 996 | 427 | 0.00037 | -0.00461 | 141.04 | -0.0288 |
| `signed_oi_delta` | 4h | 993 | 427 | 0.00042 | -0.00905 | -312.25 | 0.0059 |
| `signed_oi_delta` | 1d | 979 | 421 | 0.00007 | -0.06016 | -297.47 | 0.1023 |
| `oi_delta` | 1h | 996 | 427 | 0.00009 | -0.00319 | -70.53 | 0.0241 |
| `oi_delta` | 4h | 993 | 427 | 0.00088 | -0.01019 | -451.78 | -0.0019 |
| `oi_delta` | 1d | 979 | 421 | 0.00175 | -0.05039 | -1525.81 | -0.1179 |
| `abs_oi_delta` | 1h | 996 | 427 | 0.00000 | -0.00240 | 1.26 | 0.0445 |
| `abs_oi_delta` | 4h | 993 | 427 | 0.00036 | -0.00803 | 396.79 | 0.0320 |
| `abs_oi_delta` | 1d | 979 | 421 | 0.00122 | -0.05295 | -1757.23 | -0.0517 |

## Cross-window 稳健性 (1h horizon)

分 2 半各自跑 train/test, 比 slope 符号:

| feature | window | n_tr | n_te | test R² | slope | pearson r |
|---|---|---|---|---|---|---|
| `signed_oi_delta` | first_half | 498 | 214 | -0.00069 | 230.19 | 0.0271 |
| `signed_oi_delta` | second_half | 497 | 214 | -0.00138 | -137.54 | -0.0146 |
| `oi_delta` | first_half | 498 | 214 | -0.00313 | 181.44 | -0.0223 |
| `oi_delta` | second_half | 497 | 214 | -0.01914 | -383.96 | 0.1110 |
| `abs_oi_delta` | first_half | 498 | 214 | -0.00311 | -165.02 | 0.0402 |
| `abs_oi_delta` | second_half | 497 | 214 | 0.00011 | 149.09 | 0.0527 |

## 4 象限 OI regime 分析 (全样本, 每象限 mean_realized_bps)

| quadrant | horizon | n | mean_realized_bps | median_bps | std_bps |
|---|---|---|---|---|---|
| oi↑_price↑ (新多开) | 1h | 355 | 4.94 | -0.40 | 52.08 |
| oi↑_price↓ (新空开) | 1h | 385 | -1.10 | 2.37 | 50.63 |
| oi↓_price↑ (空平/short_squeeze) | 1h | 361 | -0.47 | -3.24 | 52.28 |
| oi↓_price↓ (多平/long_flush) | 1h | 321 | 0.63 | 1.45 | 47.11 |
| oi↑_price↑ (新多开) | 4h | 354 | 9.09 | 8.03 | 109.17 |
| oi↑_price↓ (新空开) | 4h | 384 | 1.36 | 5.18 | 108.20 |
| oi↓_price↑ (空平/short_squeeze) | 4h | 361 | 5.07 | -8.55 | 104.09 |
| oi↓_price↓ (多平/long_flush) | 4h | 320 | -0.01 | -1.09 | 95.85 |
| oi↑_price↑ (新多开) | 1d | 353 | 20.82 | 5.66 | 253.65 |
| oi↑_price↓ (新空开) | 1d | 379 | 12.73 | 13.79 | 252.89 |
| oi↓_price↑ (空平/short_squeeze) | 1d | 355 | 32.37 | 27.33 | 254.09 |
| oi↓_price↓ (多平/long_flush) | 1d | 312 | 36.02 | 37.57 | 262.16 |

## 扣成本 mean_net_bps @ q80 / q90 (cost=6.0 bps, 1h horizon)

交易规则: sign(feature) 开仓, abs(feature) >= q80/q90 才入场, 持 1h.

| feature | quantile | n | mean_net_bps | std | win_rate | pct_traded |
|---|---|---|---|---|---|---|
| `signed_oi_delta` | q80 | 286 | -6.60 | 53.13 | 0.430 | 0.201 |
| `signed_oi_delta` | q90 | 144 | -8.08 | 60.08 | 0.431 | 0.101 |
| `oi_delta` | q80 | 286 | -5.76 | 53.13 | 0.458 | 0.201 |
| `oi_delta` | q90 | 144 | -8.14 | 60.08 | 0.465 | 0.101 |
| `abs_oi_delta` | q80 | 286 | -4.34 | 53.10 | 0.434 | 0.201 |
| `abs_oi_delta` | q90 | 144 | -2.85 | 60.03 | 0.465 | 0.101 |

## 诚实判定 & 与 P1-D 预估对比

**Verdict**: NO-GO hint: R² < 0.005

**P1-D 可行性 §1.3 预估 R²=0.01-0.02** — 本次实测结果见主矩阵.

**解读**:
- signed_oi_delta 是 OI delta × sign(ΔP), 把 4 象限信号折叠到 1D;
  4 象限分析更能暴露真正 edge 来自哪个 regime.
- 4 小时 horizon vs 1 小时 vs 1 天 会有不同性质:
  - 1h: 同步噪声 + microstructure 相关更强
  - 4h: OI 动能持续性 (新开仓推动的延迟 move)
  - 1d: mean reversion / macro noise, 通常 R² 更低

## 方法学 & 限制

- 对齐 `p1d_preview_regression_funding_basis.py`: OLS 1-var, train/test 70/30, 时间顺序.
- 特征构造只用 <= t bar 的数据, **无 look-ahead**.
- y 是 forward close-to-close 无成本 bps; 成本仅在 q80/q90 报表扣.
- 4 象限按 oi_delta 和 price_change 符号切, 观察 non-linear effect.
- **样本量有限**: 60 天 × 24 = 1440 bar, warmup 1 bar 后 ~1439 个有效点.
  统计功效 ± 0.005 R² 置信区间约 ±0.004.
- **OI 深度限制**: OKX REST open-interest-history 实测仅 60 天可回填 (vs 我们要求的 90 天). 这是 API 硬约束, 不能突破.
- Cost-adjusted PnL 假设全量开仓 (no allocator throttle);
  真正 Phase 2A 还需要 sleeve_allocator 通道 cost + slippage 模型.

## 后续建议 (Phase 2A 路径)

根据本次 R²:

- **GO 路径**: 建议 Phase 2A **直接上** OI delta × sign(ΔP) feature 到 baseline strategy:
  1. 在 `aats/services/decision/features/` 加 `oi_delta_reason_codes` module
  2. 配合现有 15m bar 用 forward-fill 把 1h OI delta 映射回 15m
  3. sleeve_allocator confidence 阈值按 q80 (见上表) 定调参
  4. 走 calibration → paper → dry-run → shadow → 灰度实盘

## 可复现

```bash
# 前提: scripts/rdp_backfill_okx_rest_history.py --apply 已跑
python scripts/research/p1d_oi_delta_regression.py \
  --symbol BTC-USDT-SWAP --days 60 --cost-bps 6.0 \
  --output docs/research/p1d_oi_delta_regression_2026_04_20.md
```
