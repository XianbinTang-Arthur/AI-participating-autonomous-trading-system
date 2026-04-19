# Fast-impulse 候选公式对比选型报告 (P1-A Phase 1-A)

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


生成日期: 2026-04-19  
标的: `BTC-USDT-SWAP` 15m  
采样期间: `2026-03-17 16:30 UTC` ~ `2026-04-19 08:00 UTC` (33 天请求, 实测 33 天数据)  
样本数: `3097` (prewarm 30 根, 每根 bar 预测下一根的 realized_return)
direction 分布: long=`874` short=`886` flat=`1337` (用 ROC(5)/ATR_norm·5 normalize, |ts|>0.15 算定向)
成本假设: `6.0 bps` (taker 5 + slip 1, = 当前 config)

## TL;DR — 推荐

**不推荐上线任何 fast_impulse 候选** — 5 个公式全部未通过 R²≥0.02 门槛.

**核心发现**:

1. **15min horizon 是均值回归, 不是动量**. 5 个公式 + baseline ROC(5) 在 long/short 两个子集上 test R² 都 `< 0`, slope 全为**负数**. 这不是单个公式的缺陷, 是 15min 尺度下 BTC 的市场结构: 大单冲击后价格倾向反转.

2. **测试 R² 为负数的解读**: 负 R² 指 model fit 比 "直接预测 mean" 还差. 6 个候选 × 2 个方向 × 4 个 horizon = 48 个组合, 只有 **4 个** R² 轻微为正 (最高 0.0025, long 方向 1h horizon roc5), 且都在 0.003 以内 — 没有任何达到 0.02 门槛的.

3. **horizon 越长 long 端动量越显现**: 2h horizon, long 方向 roc5/f4/f5 slope 翻正, 但 R² 仍在 `-0.011 ~ -0.012` 之间 — 即使 2h horizon momentum 的 predictive power 也没超过 mean baseline.

4. **short 方向持续反动量**: 所有 horizon, short 子集 slope 都是负的. 这说明做空语境下, fast_impulse 越大 (向下), 越容易反弹. short 信号应 **FADE 而非 CHASE**.

5. **急拉场景 win rate = coin flip**: |ROC(5)|<0.002 + |X_k|≥80%ile 下, 5 个公式 signed mean 全在 ±3 bps 内, win rate 0.378 ~ 0.508 — 统计上无显著 edge.

**建议路径**:

- **短期 (不动代码)**: 不切换 fast_impulse 公式, 当前 ROC(5) 已是最不差的 baseline. 2026-04-19 那次 1.2% 反弹的低 momentum_alpha=0.091 是 anecdote, 不是 pattern — 33 天 3100+ 样本说明这类爆发**无法**由 15min K 线 OHLC 特征可靠识别.

- **中期 (P1-A 重新定位)**: fast_impulse 的价值若存在, 方向应是 **FADE signal** (impulse 发生后开反手单), 而不是 chase. 若要测试 fade 策略, 另做一个专门的回归/回测, 不在本报告范围.

- **长期 (引入新 feature)**: 真正的 15min 预测力需要 **microstructure** 信号:
  - orderbook imbalance / bid-ask pressure
  - trade flow aggression (taker-vs-maker split, large-order detection)
  - OI delta / funding anomaly
  - volume profile 异常 (vs 历史同时段)
  - 跨品种 lead-lag (ETH 先动 → BTC 跟)

---

**参考: 5 个候选里得分最高的是 `f4`** (F4: breakout dist vs max(high[-5:-1]) / min(low[-5:-1])), test R² long=-0.0038 short=-0.0077, 急拉 n=37 mean=-2.11 bps. **仍不达标, 仅供对照.**

## 候选公式完整对比

| Formula | long R²(test) | long slope | long Pearson r | short R²(test) | short slope | short Pearson r | 对称性 |
|---|---|---|---|---|---|---|---|
| `f1` | -0.0164 | -1006.25 | 0.0410 | -0.0210 | -856.66 | -0.0695 | ✓ 同号 |
| `f2` | -0.0164 | -1006.05 | 0.0405 | -0.0213 | -856.93 | -0.0689 | ✓ 同号 |
| `f3` | -0.0224 | -8.73 | 0.0533 | -0.0158 | -7.71 | -0.0937 | ✓ 同号 |
| `f4` | -0.0038 | -260.25 | 0.0093 | -0.0077 | -246.50 | -0.1659 | ✓ 同号 |
| `f5` | -0.0127 | -607.95 | 0.0332 | -0.0228 | -278.47 | -0.0018 | ✓ 同号 |
| `ROC(5)` (baseline) | -0.0041 | -358.79 | 0.0074 | -0.0113 | -344.51 | -0.1445 | ✓ 同号 |

### Train-set R² 参考 (in-sample, 仅做过拟合判断)

| Formula | long train R² | long train n | short train R² | short train n |
|---|---|---|---|---|
| `f1` | 0.0099 | 611 | 0.0071 | 620 |
| `f2` | 0.0099 | 611 | 0.0071 | 620 |
| `f3` | 0.0078 | 611 | 0.0084 | 620 |
| `f4` | 0.0024 | 611 | 0.0026 | 620 |
| `f5` | 0.0083 | 611 | 0.0017 | 620 |
| `ROC(5)` | 0.0029 | 611 | 0.0027 | 620 |

## 公式定义

- **F1: (close-open)/open** — `(close_t - open_t) / open_t`
- **F2: (close-close_{-1})/close_{-1}** — `(close_t - close_{t-1}) / close_{t-1}`
- **F3: EMA3.slope / atr_norm** — `(EMA_3(close)_t - EMA_3(close)_{t-1}) / close_t  ÷  (ATR_14_t / close_t)`
- **F4: breakout dist vs max(high[-5:-1]) / min(low[-5:-1])** — `(close_t - max(high_{t-5..t-2})) / max(...)  或  (close_t - min(low_{t-5..t-2})) / min(...) — 取 |较大|`
- **F5: (close - 2·close_{-1} + close_{-2}) / close_{-2}** — `(close_t - 2·close_{t-1} + close_{t-2}) / close_{t-2}`
- **baseline ROC(5)** — `(close_t - close_{t-5}) / close_{t-5}` (当前线上 momentum_score)

## Multi-horizon R² 扫描 (RAW realized_return, 不扣成本)

目的: 15min 预测 R² 为负可能是纯粹的 noise 主导. 扫描更长 horizon (30m/1h/2h) 看信号是否在某个 horizon 上出现.

### Long direction test R²

| Formula | 15m (h+1) | 30m (h+2) | 1h (h+4) | 2h (h+8) |
|---|---|---|---|---|
| `f1` | -0.0164 | -0.0047 | -0.0117 | -0.0175 |
| `f2` | -0.0164 | -0.0050 | -0.0119 | -0.0176 |
| `f3` | -0.0224 | -0.0149 | -0.0178 | -0.0303 |
| `f4` | -0.0038 | -0.0000 | 0.0003 | -0.0114 |
| `f5` | -0.0127 | 0.0004 | -0.0037 | -0.0111 |
| `roc5` | -0.0041 | -0.0008 | 0.0025 | -0.0118 |

### Short direction test R²

| Formula | 15m (h+1) | 30m (h+2) | 1h (h+4) | 2h (h+8) |
|---|---|---|---|---|
| `f1` | -0.0210 | -0.0511 | -0.0557 | -0.0088 |
| `f2` | -0.0213 | -0.0516 | -0.0560 | -0.0089 |
| `f3` | -0.0158 | -0.0532 | -0.0530 | -0.0171 |
| `f4` | -0.0077 | -0.0373 | -0.0398 | -0.0122 |
| `f5` | -0.0228 | -0.0559 | -0.0574 | -0.0127 |
| `roc5` | -0.0113 | -0.0435 | -0.0390 | -0.0055 |

### Long direction slope sign (+/-/0)

| Formula | 15m (h+1) | 30m (h+2) | 1h (h+4) | 2h (h+8) |
|---|---|---|---|---|
| `f1` | − (回归) | − (回归) | − (回归) | − (回归) |
| `f2` | − (回归) | − (回归) | − (回归) | − (回归) |
| `f3` | − (回归) | − (回归) | − (回归) | − (回归) |
| `f4` | − (回归) | − (回归) | − (回归) | + (动量) |
| `f5` | − (回归) | − (回归) | − (回归) | + (动量) |
| `roc5` | − (回归) | − (回归) | − (回归) | + (动量) |

## 急拉场景分析

场景定义: `|ROC(5)| < 0.002` (前 5 根 15m 平均化, 当前 momentum 会漏掉) 且 `|X_k| ≥ 自身 80 分位`.
对每个公式的信号, 用 `signed_return = realized_bps × sign(X_k)` 评估方向预测正确性.

| Formula | n | |X_k| 80分位阈值 | signed mean (bps) | signed std (bps) | win_rate | max | min |
|---|---|---|---|---|---|---|---|
| `f1` | 121 | 0.00231 | 1.36 | 28.69 | 0.504 | 109.3 | -109.9 |
| `f2` | 120 | 0.00231 | 1.54 | 28.74 | 0.508 | 109.3 | -109.9 |
| `f3` | 93 | 0.40220 | 0.49 | 22.37 | 0.430 | 109.3 | -62.1 |
| `f4` | 37 | 0.00832 | -2.11 | 28.72 | 0.378 | 109.9 | -62.1 |
| `f5` | 148 | 0.00348 | 2.44 | 25.26 | 0.507 | 109.3 | -61.7 |

## 急拉 case study

挑 top-3 急拉 sample: `|ROC(5)|<0.002` 但 X_best 方向预测与 realized_return 一致且幅度大.

排序键: `realized_bps × sign(f4)` 降序.

| ts | close | ROC(5) | X_best | realized_{t+1} bps | direction |
|---|---|---|---|---|---|
| 2026-03-21 23:30 | 70127.30 | -0.00176 | -0.00302 | -176.54 | short |
| 2026-03-30 00:00 | 65816.50 | -0.00002 | 0.01364 | 109.94 | flat |
| 2026-04-16 13:30 | 74565.00 | -0.00147 | -0.00314 | -109.30 | flat |

## 方法学说明

1. **数据源**: RDP `silver.market_swap_candles_15m` 表. 33 天 OKX 15m K 线.
2. **realized_return 定义**: `(close_{t+1} - close_t) / close_t × 10000`. 15 分钟前瞻.
3. **成本**: 扣 6.0 bps (taker 5 + slip 1) 得到 `y_net_bps` = 实际可套利幅度.
4. **direction 标签**: 沿用线上 FeatureEngine.trend_strength: `ROC(5)/(ATR_norm·5)` clamp 到 [-1,1], sign-threshold ±0.15.
   - 这等于把 bar 分成 long/short/flat 三类, 回归只用 long & short 子集. Flat bars (通常占 30-50%) 不参与 — 因为 flat 的 realized_return 噪音大, 且不是我们的触发场景.
5. **回归**: OLS `y_net = a + b·X_k`. Train 前 70%, Test 后 30% (时间顺序, 无 look-ahead).
6. **R² 判定**: 目标 test R² ≥ 0.02 (比当前 baseline leg_score 0.005 高 4×).
7. **对称性**: long / short 子集的 slope 符号必须一致 (universal predictive power).

## 风险与局限

- 33 天数据, long+short 子集分别约 800-1500 样本. 统计显著性可接受但不是极强.
- direction 用 ROC(5) normalize, 与我们试图取代的指标有重合 — 不构成循环因果, 因为 direction 分类后, fast_impulse 是在当前方向内找 **额外** 的信号.
- 急拉 case study 在当前窗口数量较少 — 若没有则说明此窗口少见爆发行情 (主要是震荡).
- 成本假设 6.0 bps 是线上当前值. 如果成本假设变化, slope/intercept 不变, 但 y_net 绝对值会变.

## 可复现

脚本: `scripts/calibration/fast_impulse_selection_regression.py` (参数: `--days 33 --symbol BTC-USDT-SWAP`)

