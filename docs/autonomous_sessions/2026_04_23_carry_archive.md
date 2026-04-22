# 2026-04-23 · Carry ARCHIVE Record

**战略上下文**: AATS 本季度战略重置为 **R · Re-scope** (research + infra + governance
platform)。Carry 从"主线"降级为"待结案候选"，用 pre-registered scorecard 做最后一轮
OOS 结案验证。

本文件为归档记录，非战略文档。

---

## Verdict: ARCHIVE_CARRY

触发规则（pre-registered, mechanical）:
```
if any dim FAIL:           ARCHIVE_CARRY
elif count(MARGINAL) ≥ 2:  ARCHIVE_CARRY
elif all 3 PASS:           PASS_ALL_PROMOTE
else:                      ARCHIVE_CARRY (1 MARGINAL 也 archive, no benefit of doubt)
```

**实际命中**: any FAIL → ARCHIVE_CARRY (dim-2 触发)

---

## 3 维 Scorecard 结果（pre-registered, 不可 retroactive 调整）

| Config | 值 |
|---|---|
| Sample window | 2024-10-01T00:00:00Z → 2025-03-31T23:59:59Z (182 天) |
| Symbols | BTC-USDT (spot) + BTC-USDT-SWAP (perp) |
| Fee assumption | 20 bps round-trip (taker × 4 legs) |
| Data ingest_run_id | `082ca52a-7b76-439a-abe7-c0bc2156eec8` |
| Silver dataset_version | `oos_2024Q4_2025Q1` |

| Dim | 测量值 | 阈值 PASS | Verdict |
|-----|--------|----------|---------|
| 1. OOS 样本表现（annualized net） | **+810.05 bps** (C1 passive hold) | > 500 bps | **PASS** |
| 2. Maker 可达性（post_only fill rate, 30s timeout, no fallback） | **1.05%** | ≥ 80% | **FAIL** |
| 3. 资本效率 ROIC | **6.48%** | > 6% | **PASS** (擦边 +0.48pp) |

### Dim-1 细节
- C1 passive hold: +810 bps annualized (+403.82 bps / 182 天 × 365)
- C2 selective (funding > 0.5 bps, 30d hold): +712 bps annualized, 5 trades
- score_dim1 = max(C1, C2) = **810.05 bps**

### Dim-2 细节
- 4,368 次 1h bar attempts
- maker_fills = 46, unfilled_skipped = 4,322
- 30s timeout × bar-level 价格穿越概率上界 → fill 1/120 per touched bar
- **核心问题**: 30s timeout + 1h 粒度数据下结构性不可能接近 80%，要真·上阈值需 tick-level
  orderbook + queue position model + 实时 micro-structure 验证

### Dim-3 细节
- annualized_net_pnl = (810.05 / 10000) × $10,000 = $810.05
- total_capital_deployed = $10,000 spot + $1,000 perp margin + $1,500 risk buffer = $12,500
- ROIC = $810.05 / $12,500 = 6.48%
- **敏感性**: fee 20→25 bps 即跌入 MARGINAL (5.88%)；25→30 bps 即 FAIL

---

## Data hygiene 备注（不改变 verdict）

- **Funding rate 数据**: OKX public `/api/v5/public/funding-rate-history` 深度仅 ~33 天，
  OOS 窗口不可达。Agent fallback 用 `(mark_close - index_close) / index_close` 派生
  8h funding approximation (avg +0.78 bps/8h，与业内 BTC ~8% 年化 funding 量级自洽)。
- **派生 funding 不写 silver.market_swap_funding**，避免污染 production dataset
- Dim-1 基于派生 funding; dim-2/dim-3 不依赖 funding
- **此 caveat 不改变 verdict**: dim-2 FAIL 已单独 trigger ARCHIVE

---

## 归档声明

> **Carry not economically viable under current capital + current execution assumptions
> (as of 2026-04-23).**

原因分解:
- ✓ Funding 经济逻辑成立 (dim-1 PASS, +810 bps/年 annualized gross)
- ✓ 资本效率模型勉强过门槛 (dim-3 PASS 擦边)
- ✗ **Maker 执行可达性结构性不达标** (dim-2 FAIL, 1.05% vs 80% 阈值)

---

## Reopen 条件（任一满足才可重开讨论）

1. **Maker 执行实证**: 至少 30 天 live shadow post_only 命中率数据证明真实 fill rate ≥ 50%
   （注：距 80% 阈值仍差 30pp，但可重新评估）
2. **资本规模上升**: 账户资金 ≥ $100k（ROIC 6.48% × $100k = $6,480/年，才有绝对金额意义）
3. **Fee tier 提升**: 账户达 OKX VIP 2+（maker 1 bps vs Regular 2 bps，alpha 直接翻倍级）
4. **市场结构转变**: funding 结构性上移（perp 溢价持续 > 50 bps/8h），即使 maker 不优化
   也能让 dim-1 过更宽阈值

**不满足任一条件前，不 revisit carry**。

---

## 防假进展边界

- ❌ 不做 "M2 carry harness 扩展"（归档结论 flag 的是执行缺陷，不是 harness 不足）
- ❌ 不做 micro-structure research "为 carry 做准备"（microstructure 是独立 research engine，
  不挂 carry 目标）
- ❌ 不 revisit carry，除非上述 Reopen 条件明确满足
- ❌ 不在本季度内重开此讨论

---

## 保留的 platform assets（归档不清零）

| Asset | 状态 | 原因 |
|-------|------|------|
| `aats/data_platform/replay/backtest/*` (fill/position/equity/cost/harness/CLI, 88 tests) | 保留 | 是 generic research platform，可服务其他 research line |
| `silver.market_spot_candles_1h` / `silver.market_swap_candles_1h` (dataset_version=`oos_2024Q4_2025Q1`) | 保留 | 供未来 research reference |
| `/tmp/oos_carry/scorecard.json` | 保留 | 审计 artefact |
| M1 / M2 analysis reports | 保留 (docs/autonomous_sessions/2026_04_23_*.md) | 审计链 |

---

## 关联 commits

- Backtest MVP: 3667341, 0fbe802, 5fda0cb, b8aa1bc
- Live 链路修补: c510383, dd6b2c1, c09490e (已 deploy on HEAD c09490e)
- Session 战略报告: 5d451f4
- 本归档: (this commit)
