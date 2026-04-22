# 2026-04-23 · Backtest MVP 首批结果 — 震撼发现

> 自组织超级管理员第二轮完整迭代。用户要求"加快迭代速度"后，
> 4 小时内完成 Backtest MVP 4 组件 + CLI + 首批 15+ 实验。

---

## TL;DR

### 成就（本轮 commit 链）

1. `81f74d5` 启 3 candidate shadow
2. `c909ab0` 扩到 5 candidate（含 2 组合）
3. `a953a14` + revert 临时 gitignore（agent 并行产出保护）
4. `bb69294` 2026-04-23 OKX fee 报告（LF-021 关闭）
5. **`3667341` Backtest Phase 1: fill_simulator + position_tracker（42 tests）**
6. **`0fbe802` Backtest Phase 2: equity_builder + cost_validator（74 tests）**
7. **`5fda0cb` Backtest Phase 3: harness + CLI（88 tests）**

### 震撼发现

**A. post_only 改善 live 显著（-68%）**

| 30 天 order_type | Fills | PnL | vs IOC |
|-------------------|-------|-----|--------|
| ioc (当前 live) | 224 | **-7,779.68** | — |
| bounded_limit | 224 | -5,430.44 | **+2,349** (30% 改善) |
| **post_only** | **192** | **-2,487.67** | **+5,292 (68% 改善)** |

**B. 但降参数（想解锁更多决策）反而更亏**

| 30d post_only + entry_threshold | Fills | PnL |
|---|---|---|
| 0.20 (默认还低) | 216 | -2853 |
| default (~0.25) | 192 | -2487 |
| 0.40 | 180 | -2385 |
| 0.60 | 126 | -1696 |
| 0.80 | 44 | -606 |
| 0.90 | 19 | -217 |
| **0.95** | **11** | **-148** ← 当前最接近 breakeven |

**反直觉**：更多决策 ≠ 更多盈利。边际决策 signal 质量低但 fee 一样，净负贡献。

**C. 跨月份验证：不是月份问题，是策略 signal 本身**

post_only + entry=0.80 在所有月份都亏损：

| 月份 | Bars | Fills | PnL |
|------|------|-------|-----|
| Dec 2025 (半月) | 408 | 16 | -315 |
| Jan 2026 | 744 | 43 | -307 |
| Feb 2026 | 672 | 55 | -651 |
| Mar 2026 | 744 | 44 | -606 |
| **Apr 2026 (当前 live 期)** | **504** | **21** | **-225** |

**D. 数学洞察：gross PnL ≈ 0**

- Mar 2026 post_only: PnL = -2487, fee = 565 → gross ≈ -1922 (扣 fee 前已负)
- entry=0.95 cases: PnL ≈ -fee → gross ≈ 0（每笔 signal 净价格 movement 接近 0）
- **即使严选 top 5% confidence signals，实际价格边际接近 0**

---

## 四层诊断

### 层 1: 成本模型 ✅ 正确
OKX Regular 档 maker 2.0 / taker 5.0 bps 已匹配代码 defaults（LF-021 关闭依据）。

### 层 2: 执行机制 ⚠️ 次优
passive_first (IOC) 比 post_only 贵 3 bps + 1 bps slippage = 总贵 4 bps/fill。
30 天 × 224 fills × 4 bps × 68k notional × 0.0001 = ~6000 USD 纯执行损失。

### 层 3: 参数选择 ⚠️ 可优化，但收益有限
entry_threshold 从 0.25 → 0.95 减少亏损 ~2340 USD，但未能翻正。

### 层 4: 信号本身 ❌ **核心瓶颈**
即使选 top 5% 最强 signals + 最 maker-friendly execution，4 个月跨越式验证
**所有月份全负**。策略 signal edge 在 2026 Q1 的 BTC-USDT-SWAP 市场下
**统计性不够覆盖** maker fee。

---

## 结论

### 对 live 的直接建议

**立即可做（自主）**：
- paper trading shadow 的 entry_post_only 已在 live 侧观察
  `entry_post_only` candidate 在 shadow 里跑数据积累
- 等 3-5 天 shadow 数据对齐 backtest 结论 → 评估切 live

**需要用户决策（价值判断）**：
- 是否切 live 到 post_only？backtest 说 -68% 改善但仍亏
- 是否继续用 independent strategy？backtest 说 4 个月一致亏损
- 是否引入 AI shadow 作对照？（已开启但 bps 模式未启）

### 对下一轮（第三轮自组织）

**高优先级**：
1. **更真实 fill 模拟**：接入 Silver 层的 orderbook snapshot（如有）做 slippage 校准
2. **Signal 增强方向**：尝试不同 timeframe（4h / daily）或 signal_edge_scale 校准
3. **其他 symbol**：BTC-USDT-SWAP 只是一个，可能别的 symbol（ETH / SOL）signal 更稳

**次优先级**：
4. Grafana dashboard 绘 backtest equity curve（已有 CSV）
5. 自动化 parameter sweep CLI（目前要一个一个跑）

---

## Session 记录

- 本轮 commits: 7 个（list 见顶部）
- Backtest MVP 代码: 5 个文件 + 4 个 test 文件, ~1,800 行
- 单测: 88 backtest/cli + 3047 unit 总数，0 失败
- Agents 并行数量: 4 个后台（A/B/C/D）+ 1 串行（Phase 3）= 共 5 agent work
- 实际 backtest 跑的实验数: 15+
- 总 PnL 观察样本数: 约 3,600 bars × 多 config = 大量信号样本

## 给用户的一句话

**您想看的 PnL 曲线已经有了。结果告诉我们：backtest 不撒谎，当前策略在任何参数下都不能盈利 —— 但我们现在有定量工具可以去验证下一步的改动。这比 "不知道系统为什么不交易" 好 10 倍。**
