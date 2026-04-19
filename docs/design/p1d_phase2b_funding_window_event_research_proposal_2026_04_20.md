# Phase 2B 立项 — Funding Settlement Event-Window 研究 (2026-04-20)

**状态**: 📋 立项草案（等 Phase 2A 完成后启动）
**依据**: `docs/research/p1d_preview_regression_funding_basis_2026_04_20.md` §3 次级发现
**所属**: P1-D Phase 2B Exploration track（非 Phase 2A 主线，不阻塞核心 gate）

---

## 1. 触发发现

预览回归 (2026-04-20) 跑 kline+funding 6 特征 × 4 horizon 普遍 R² < 0.005, **但**:

- **全样本 `minutes_to_next_funding`**: Pearson ≈ 0, R² = -0.0007 (无效)
- **Subset `≤ 60 min` bucket**: **Pearson = -0.19**（所有特征/子集中绝对值最大）

这不符合"全样本无 edge"的模式，而是**event-window specific**:  
funding settlement 前 1 小时，perp 价格和 funding 方向**反向**的倾向显著。

### 1.1 初步解释（需验证）

- Funding settlement 是 BTC 交易日的**已知事件**（UTC 00:00 / 08:00 / 16:00）
- Market participant 提前 positioning 对冲 funding fee → 临近 settlement 时买卖压力方向与 funding 相反
- 这是 **predictable event-driven** 现象, 与 "persistent feature" 不同

### 1.2 为什么 Phase 2A 不处理

Phase 2A 的 3 核心特征（OI delta / trade flow / OBI）是 **persistent linear feature**，假设模型: `y = f(x)` over full sample.

Event-window 不同: `y = f(x, context_event)`, 需要时间 conditional slicing。混入 Phase 2A 模型会被其他 3 特征的 "null" 稀释，**必须独立立项**。

---

## 2. 研究设计（Phase 2B scope）

### 2.1 数据要求

| 数据 | 来源 | 覆盖 |
|---|---|---|
| 15m candles (swap) | `silver.market_swap_candles_15m` | 已有 33 天 |
| funding rate history | `silver.market_swap_funding` | 已有 3 月 |
| funding settlement ts | derived | OKX BTC-USDT-SWAP fixed times UTC {00,08,16} |
| realized return | candles-derived | 多 horizon {5m, 15m, 30m, 60m} |

**现有数据够做初轮**（Phase 2A 同步跑，不需要新 backfill）。

### 2.2 回归结构

**Event window 定义**:
```
E(t) = minutes_to_next_settlement(t)
slice into buckets: [0-15min], [15-30min], [30-60min], [60-120min], [>120min]
```

**Per-bucket regression**:
```
y_{t+h} = a_b + β_b · funding_rate(t) + ε
```
for each bucket `b` and horizon `h ∈ {5m, 15m, 30m}`

**关键假设 (要验证)**:
- `β_b` 在 `[0-15min]` bucket 显著负 (perp price 与 funding 反向)
- `β_b` 在 `[>120min]` bucket 不显著 (event-window 外无 edge)

### 2.3 交叉验证

- Cross-window: 前 15 天 vs 后 15 天，确认 slope 符号稳定
- Multi-symbol: BTC + ETH + SOL（扩 symbol 范围）
- 不同 funding regime: |funding_rate| > median vs < median

### 2.4 PnL 模拟

```
if minutes_to_settlement < 30 and funding_rate > threshold:
    short_signal = True  # funding 正 → perp 溢价 → 临近 settle 可能反向
    entry_price = mark_price(t)
    exit_price = mark_price(t + 30min)
    pnl = short_pnl(entry, exit) - 6 bps cost
```

验证 60 天窗口内 **总 PnL 扣成本后是否正**，且 **Sharpe > 0.5**。

---

## 3. 成功/失败 Gate

| 判定 | 条件 |
|---|---|
| **GO** | [0-60min] bucket cross-window Pearson |r| ≥ 0.15 + 扣 cost 后 mean_net > 2 bps + Sharpe (per-trade) ≥ 0.3 |
| **CONDITIONAL-GO** | Pearson 对但 PnL 不显著 ("informational signal"); 可作为 regime gate 输入 |
| **NO-GO** | Cross-window 不一致, 或 PnL ≤ 0 |

---

## 4. 工作量估算

| 阶段 | 工期 | 交付 |
|---|---|---|
| **W0** Script 开发 | 0.5 人天 | `scripts/research/p1d_funding_event_window_regression.py` |
| **W1** 回归 + 报告 | 0.5 人天 | `docs/research/p1d_funding_event_window_regression_2026_XX_XX.md` |
| **合计** | **1 人天** | — |

---

## 5. 风险与局限

- **Funding settlement 是 well-known 事件**: 大机构已在 arb，edge 可能已被 HFT 吃掉
- **n 小**: 30 天 × 3 settle/day = 90 个 event, per-bucket n=90 不多
- **Bonferroni**: 5 buckets × 3 horizon = 15 tests, 需严格矫正

---

## 6. 与 Phase 2A 的协同

- Phase 2A focus: persistent directional alpha (OI delta / trade flow / OBI)
- Phase 2B funding event: **正交信号源**，不冲突
- 若 Phase 2A GO + Phase 2B GO → 两类信号**合成** portfolio, 多元化
- 若 Phase 2A NO-GO: Phase 2B 可作为独立 event sleeve（不依赖 microstructure）
- 若 Phase 2B NO-GO: 只损失 1 人天

**独立立项最大价值**: 如果 Phase 2A NO-GO, Phase 2B 仍是 **non-zero alpha** 可能性 (比 γ funding carry 更 directional)

---

## 7. 启动条件（不要现在启动）

- ✅ **不要求**: Phase 1A 48h 稳定性完成（funding 数据已有）
- ⏳ **不要求**: Phase 2A 回归完成（两条 track 可并行）
- ⏸️ **推荐**: 等到 Phase 2A Week 3 结束看 OI delta 回归结果，再决定 Phase 2B 立项优先级

---

## 8. 签署

- **立项人**: Claude Opus 4.7 · 2026-04-20
- **依据**: P1-D preview regression 次级发现
- **归档**: 本文档；主线工作在 Phase 2A 完成后返回决定是否启动
- **负责**: 待用户在 Phase 2A 结束时 assign
