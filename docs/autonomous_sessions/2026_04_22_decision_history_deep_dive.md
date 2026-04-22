# 2026-04-22 · 决策历史深度分析 — 重写早期分析的几个错误结论

> **动机**：用户不在期间我用监控间隙跑 7 天（2026-04-17 到 2026-04-23）的
> DecisionOutcome 数据深挖。**之前我的 Phase 2 baseline 分析里有几个重要的
> 错误结论**，这份报告纠正 + 给出新的、更准确的 actionable insight。

---

## TL;DR（纠错版）

### 之前我说错的

1. **"Score gate (0.25) 一直是主 blocker"** —— 错。
   **事实**：2026-04-22 有 **1995 次 score ≥ 0.25**（入场门槛）！只是那些
   决策仍被后续 gate 挡住。score gate 在信号活跃期间**并不限制**。

2. **"信号强度 ~0.5 bps"** —— 不对。这只是 ONE sample。
   **事实**：信号强度每天都不同。2026-04-17 max **99 bps**；2026-04-22
   max 1.18 bps；平均看有波动。

3. **"Score gate 是第一阻塞，net_edge gate 是第二"** —— 顺序颠倒。
   **事实**：真正的主 blocker 是 `expected_net_edge_below_safe_threshold`
   （1995/4130 条 04-22 决策触发）。score_stability 和 confirm_ticks 是次要
   的内部 filter。

---

## 核心数据（2026-04-17 → 2026-04-23，7 天）

### 每日决策总量 & 方向性
| Date | Decisions | Hold | Non-flat | Avg composite_alpha |
|------|-----------|------|----------|---------------------|
| 04-17 | 2973 | 2945 | **94** ← 唯一交易日 | 0.0972 |
| 04-18 | 3119 | 3119 | 0 | 0.0310 |
| 04-19 | 3252 | 3252 | 0 | -0.0222 |
| 04-20 | 2869 | 2869 | 0 | 0.0695 |
| 04-21 | 3447 | 3447 | 0 | 0.0731 |
| 04-22 | **4130** | 4130 | 0 | **0.1316** |
| 04-23 (partial) | 60 | 60 | 0 | 0.1351 |

### Independent long leg score 分布
| Date | avg | p95 | Max | ≥ 0.15 | ≥ 0.25 (entry thr) |
|------|-----|-----|-----|--------|--------------------|
| 04-17 | 0.20 | 0.50 | 0.65 | 1417 | **740** |
| 04-18 | 0.15 | 0.30 | 0.53 | 1103 | 429 |
| 04-19 | 0.14 | 0.26 | 0.46 | 1162 | 212 |
| 04-20 | 0.22 | 0.54 | 0.76 | 1543 | 1172 |
| 04-21 | 0.23 | 0.58 | 0.72 | 1643 | 1413 |
| 04-22 | **0.26** | **0.65** | **0.77** | 2384 | **1995** |
| 04-23 | 0.24 | 0.45 | 0.56 | 36 | 27 |

### Expected NET edge 分布（signal - cost - buffer）
| Date | avg | max | ≥ -2 bps | **≥ 0 (can trade)** | ≥ 2 bps |
|------|-----|-----|----------|---------------------|---------|
| 04-17 | 12.74 | **99.35** | 1921 | **1778** | **1669** |
| 04-18 | -2.48 | 73.75 | 325 | 291 | 280 |
| 04-19 | -4.88 | -1.11 | 12 | 0 | 0 |
| 04-20 | -5.35 | 1.09 | 184 | 24 | 0 |
| 04-21 | -5.30 | 0.61 | 400 | 14 | 0 |
| 04-22 | -4.92 | **1.18** | 730 | **165** | 0 |
| 04-23 | -5.22 | -1.30 | 1 | 0 | 0 |

### Blocked reasons（2026-04-22 long leg, 4130 次决策）
| Reason | Count | % |
|--------|-------|---|
| `expected_net_edge_below_safe_threshold` | **1995** | 48% |
| `score_stability_below_threshold` | 391 | 9% |
| `score_support_below_min_confirm_ticks` | 115 | 3% |

---

## 🎯 Actionable Insights

### 洞察 #1：2026-04-22 有 165 次 net_edge ∈ [0, 1.18] bps 的机会

这些决策 "**刚好跨过 0 bps 门槛**" —— 打满了成本、buffer 和安全阈值
后仍有正期望收益。但平均 score 在 0.26-0.65，**仍然落在 score_stability
或 min_confirm_ticks 过滤的范围内**。

**意味着**：cost 端改进 1-2 bps（maker rebate）→ 这 165 次都变成更强
信号（1-3 bps net），更容易过后续稳定性 gate。

### 洞察 #2：04-17 是"signal 异常丰富"的一天

**1669 个 net_edge ≥ 2 bps** 的决策，最大 99 bps(!)。但实际只成交 25 次。
换算 "进入交易" 的转化率 = 25/1669 ≈ 1.5%。

问什么：
- 每个 decision 都是 symbol × timeframe 的"当下信号"，但连续多个 snapshot
  指向同一个 entry 意图 → 只需要成交 1 次就持仓
- `score_stability_below_threshold` 要求信号在 N 个 confirm ticks 内稳定
- `min_hold_remaining_seconds` 有冷却

所以 1669 信号 → 25 交易**本身合理**（不是 bug）。重要的不是"信号数"而是
"最终是否 profitable"。

### 洞察 #3：04-18 到 04-22 是"signal 枯竭期"，非 bug

连续 5 天 0 交易的根因：**expected_signal_edge_bps 几乎天天低于 cost 6 bps**。
策略"不交易"是对的保守决定。

但 04-22 信号已在回暖（avg composite_alpha 0.13 比前几天高，max score 0.77
恢复到 04-17 水平），如果这是周期性现象，下一个"signal 丰富期"可能在未来
几天出现。

---

## 对 LF-021 (maker rebate) 优先级的重估

**之前（Phase 2 报告）**：LF-021 被我判为"改成本模型 = value-laden → 等用户决定"。

**现在**：
- 04-22 有 165 次 net_edge ∈ [0, 1.18]，cost 端少 1-2 bps 就能让它们**全部**
  变 2-3 bps net（容易过 stability gate）
- 04-21 有 14 次 net_edge ∈ [0, 0.61]，同样
- 04-20 有 24 次 net_edge ∈ [0, 1.09]

**大约每天 10-160 次 "被 cost 误判掩盖" 的潜在交易机会。**

**maker rebate 在 OKX 的典型值**（VIP 0-5 档）：
- 无 VIP（Tier 0 Regular）: maker fee 0.02%（不是 rebate，只是更便宜）
- VIP 1 以上：maker **rebate** -0.015% 到 -0.005%
- 如果账户有 maker rebate → 有效 cost 降 0.015% = 1.5 bps

**结论**：**LF-021 的改进幅度 ~1-2 bps，正好是解锁 04-22 那 165 次机会的幅度。**
从"有用的技术改进"升级为"直接解锁交易机会"。

---

## 我不自主做的（为什么）

**LF-021 代码改动**本身我**仍然不自主做**，原因：

1. 需要**读 `.env.derivatives.live` 文件**才知道 OKX 账户实际的 VIP 等级和
   fee 表 —— 但这文件在 CLAUDE.md 铁律里是 "绝不读凭证文件" 的范畴
2. 不同账户、不同品种、不同时间 rebate 不一样
3. 填错费率会让 cost 模型说谎 → 决策层意外放开不该放的单

**我需要您提供的信息**（等您回来一句话回答）：
- 您 OKX 账户 BTC-USDT-SWAP 交易对，maker 和 taker 的**实际**费率分别是多少？
  （可以去 OKX 费率表页面查，或告诉我您的 VIP 等级）
- 是否有 maker rebate？

提供后我就可以直接改 `EffectiveFeeResolver`，把真实费率接进 cost 模型，
然后写 anchor test 锁定。预计 2-3 小时完成 + deploy + 观察。

---

## 额外观察（非紧急但值得记录）

### `score_stability_below_threshold` 是一个真实的独立 gate

我之前的分析漏了它。在 score 过 0.25 阈值之后，还需要"score 稳定 N 个
confirm ticks"。这是 `independent/adaptive.py` 里的逻辑。

**查验**：如果用户想开 paper trading candidate，`strategy_hedge_independent_
score_stability_*` 相关配置也应该是候选实验对象之一，不只是 entry_threshold。

### Direction bias 一直 "flat" 的原因

7 天数据里 `baseline.direction_bias` 在绝大多数决策里都是 "flat"。这驱动了
前面的 scoring 分析（confidence 只在 leg == direction_bias 时计分 →
flat 时 confidence 永远 ~0.06 最大）。

如果用户感兴趣，可以 paper trading 测 `baseline_direction_threshold_*` 更宽
（让 bias 更容易被判为 long/short）的候选。

---

## 下一步推荐

1. **立即**：当您回来时告诉我 OKX 实际 maker/taker fee → 我做 LF-021
2. **短期**（我可以自主做）：开始 paper trading 的 candidate（利用 Round 3
   基建），测 `strategy_edge_noise_buffer_bps` 从 4.0 降到 1.0 会不会解锁
   04-22 的 165 次机会
3. **中期**：规划 Phase 3（Grafana dashboard for paper trading 数据）

**不推荐立即做**：降 score_stability / min_confirm_ticks 阈值 —— 这是策略
参数，改之前必须 backtest（Gap #2）。

---

## Commit summary

本 session 除了这份报告，还有（按时序）：
- R3P2 window evaluator (63659c7)
- AI shadow exception backport (aaac5f8)
- 6+ 小时的 30min 周期监控（全稳）

本报告产出：零代码改动，纯数据分析。
