# 2026-04-23 · AATS 战略报告 — Backtest MVP + 跨 symbol 诊断 + funding carry 调研

> Session 从 "我醒了" 开始，到 "往量化方向走吗" + 用户授权 autonomous 后
> 执行完整的 alpha 源调研。自组织超级管理员第二轮 final deliverable。

---

## TL;DR（30 秒版）

1. **AATS backtest MVP 今日完成**：4 组件 + CLI + 88 tests + 跨 BTC/ETH 5 个月真实回测。
2. **真相 1**：Independent strategy 在 BTC 和 ETH **跨 5 月、各参数下全亏**。
3. **真相 2**：根因是 **gross signal edge < fee**，不是 bug 不是市场。
4. **真相 3**：post_only 切换改善 68% 但仍亏（fee 仍超过 gross edge）。
5. **真相 4（最新）**：Funding carry 独立策略**不可行**（本金不够 + 风险 337× 于收益），
   但作为 **signal filter** 可能有价值（零 capital cost）。
6. **建议 live 暂不动**，聚焦找真 alpha。

---

## Section 1 · 本 Session Commits

```
5fda0cb feat(backtest): MVP Phase 3 harness + CLI
0fbe802 feat(backtest): MVP Phase 2 equity_builder + cost_validator
3667341 feat(backtest): MVP Phase 1 fill_simulator + position_tracker
b8aa1bc docs(backtest): MVP Phase 4 首批结果 + 5 月诊断
bb69294 docs: OKX fee + LF-021 关闭
c909ab0 feat(paper-trading): shadow 扩 5 candidate
81f74d5 feat(paper-trading): shadow 启 3 candidate
a953a14 chore(gitignore): 临时 backtest WIP (reverted)
```

**产出**: 7 commits, ~2000 行新代码, 88 新单测, 30+ backtest 实验。

---

## Section 2 · 核心发现

### 🔴 Finding 1: Independent strategy gross edge ≈ 0 跨 symbol

**ETH 4 月参数扫描 (2025-12-13 → 2026-04-22, 3112 bars):**

| entry_threshold | Fills | Net PnL | Fee | **Gross PnL (pre-fee)** |
|----------------|-------|---------|-----|-------------------------|
| 0.80 | 210 | -120.00 | 99.68 | **-20.32** ← neg |
| 0.90 | 90 | -42.36 | 42.06 | **-0.30** ← 0 |
| 0.95 | 34 | -19.76 | 15.17 | **-4.60** |
| 0.96 | 15 | -7.66 | 6.22 | **-1.44** |
| 0.97 | 6 | -1.67 | 2.65 | **+0.98** ← 小正但 6 fills 统计无意义 |
| 0.98 | 2 | -0.11 | 0.86 | **+0.75** ← 2 fills 不显著 |
| 0.99 | 0 | 0 | 0 | — 无 signal 过门槛 |

### 🟡 Finding 2: BTC signal 比 ETH 更强，但 notional 放大器也放大 fee

BTC 30d post_only default gross PnL = +181 USD (192 fills) = **+0.94 USD/fill**
ETH 30d post_only default gross PnL = -6 USD (214 fills) = **-0.03 USD/fill**

**BTC gross edge per fill 显著大于 ETH**，但：
- BTC fee per fill ~= 13.9 USD (2 bps × $680 notional)
- ETH fee per fill ~= 0.42 USD (2 bps × $350 notional)
- 两者 net = **gross - fee → 都是负**

"ETH 看似好" 只是 notional 小带来的 damage control 假象，**不是 ETH signal 更强**。

### 🟢 Finding 3: Execution mode 改善已完全量化

30 天 BTC 对照:
| Order type | Fills | PnL | vs IOC |
|-----------|-------|-----|--------|
| ioc (当前 live) | 224 | -7,779.68 | — |
| bounded_limit | 224 | -5,430.44 | +30% |
| **post_only** | 192 | **-2,487.67** | **+68%** |

若切 live 到 post_only，**亏损减少 2/3**。但仍是亏损（gross edge 依然 < maker fee）。

### ⚪ Finding 4: Cost model 一直正确，LF-021 已关闭

OKX Regular 费率 2.0/5.0 bps **本来就是 code defaults**。maker rebate 要 VIP 7+（本金差 250×）。
真正的 cost 改进路径不是 "加 rebate"，是 "execution mode 切换"。

---

## Section 3 · Funding Rate Carry 调研（agent `abe4bdf6553acf45f` 交付）

### 数据特征（BTC/ETH, 2025-12-13 → 2026-04-22, 293 个 8h 结算事件）

| 指标 | BTC | ETH |
|---|---|---|
| p50 funding (bps/8h) | 0.25 | 0.15 |
| σ funding (bps/8h) | 0.43 | 0.53 |
| 正占比 | **68.6%** | **59.7%** |
| 年化 gross carry yield | **~2.2%** | **~2.2%** |

### 致命约束：价格风险远大于 carry alpha

- BTC 价格单 8h log-return σ = **145 bps**（funding σ 的 **337 倍**）
- ETH 同 σ = **192 bps**（funding σ 的 **363 倍**）
- 结论: **无 hedge 不可行** → 必须 spot long + perp short 做 delta-neutral

### $390 本金的 reality check

- BTC delta-neutral 最小 unit: spot $850 + margin $85 = **$935** → **本金不够**
- ETH delta-neutral 最小 unit: spot $240 + margin $24 = **$264** → **能跑 1 个 pair**
- 但 ETH 月 funding 收入 ≈ $0.43，单次 open/close taker fee ≈ $10+ → **28 天 hold 才 breakeven**

### 💡 Agent 的聪明建议

> "把 funding rate 当作现有 strategy 的 **signal filter**，而非独立策略 — 零额外 capital, 低风险验证"

具体：
- 当 funding > 0.5 bps 时，禁止做多（多头付 funding，signal edge 被蚕食）
- 当 funding < -0.5 bps 时，禁止做空（反之）
- 在 backtest harness 加 `funding_filter_enabled` 参数验证

### Go / No-Go

| 方案 | 判断 | 理由 |
|------|------|------|
| Funding carry 独立策略 | ❌ **No-Go** | 本金 + 风险比 + fee 三重不对 |
| Funding 作 signal filter | ✅ **Worth POC** | 零 capital, 可能改善 existing gross PnL |

---

## Section 4 · "往量化方向走吗" 的结构化回答

AATS **本身就是量化系统**。问题不是"要不要量化"，是"哪种量化 style"。

### 真实可走的路径（按 ROI 排序）

**🥇 P1 · Funding-aware Signal Filter**（agent 新发现，最高 ROI）
- 工程量: 1-2 天（harness 加 filter + backtest 验证）
- 风险: 低
- 预期: 未知但 positive skew（不 work 也不亏）

**🥈 P2 · Regime-Aware Wrapper**
- 工程量: 1 周（regime classifier + backtest 集成）
- 风险: 中（需 classifier 训练）
- 预期: 中（假设有 regime 差异）

**🥉 P3 · 换 Strategy Family（factor / momentum / carry）**
- 工程量: 2-4 周
- 风险: 高（overfit 风险）
- 预期: 有潜力但不确定

### 不推荐的路径

- ❌ **换 live 到 ETH**: backtest 证明只是 damage control，无盈利路径
- ❌ **Funding carry 独立策略**: 本金不够 + 风险比例失衡
- ❌ **Market making / HFT**: VIP 门槛 + 本金 + 延迟要求不现实
- ❌ **立即切 live 到 post_only**: 值得做，但不急；等 shadow 数据完整

---

## Section 5 · 对 live 的建议

### ❌ 暂不建议动
- 切 live 到 ETH（只减少损失量，无盈利路径）
- 降 edge_buffer / min_net_edge（backtest 证明更亏）
- 调 entry 到 0.95+（fills 减少 = 亏损减少，但未盈利）

### ✅ 可以考虑（需用户决策）
- **切 post_only**: 30d backtest 说 -68%。等 shadow 再 2-3 天，若成交率 ≥70% 则切
- **暂停 live**: 聚焦找 real alpha，不亏 = 第一步不赔

---

## Section 6 · 角色复盘

| 角色 | 贡献 |
|------|------|
| Dependency Researcher × 2 | OKX fee 调研 + Funding rate 可行性 |
| Explore Agent | Replay 基建摸底 → 80% 已就位 |
| Backend Engineer × 4 | 并行 fill/position/equity/cost/harness/CLI |
| Test Engineer | 88 单测，3047 unit 全绿，零回归 |
| Documentation Writer | 3 份 session docs (OKX fee + Phase 4 + 本文件) |
| System Architect | P1/P2/P3 战略对比 |
| **Main Coordinator** | PM + yaml changes + deploy + 30+ experiments + cross-symbol 分析 |

**Review Rule**: 并行度达 5，无瓶颈，交付质量高。

---

## Section 7 · 给用户回归后的两个核心问题

**Q1**: 知道 independent strategy 在 BTC+ETH 都亏、post_only 仅是 damage control，**您要继续 live 吗**？
- (A) 继续跑 — 监控 shadow/backtest 趋势
- (B) 暂停 live — 专注 alpha 研究
- (C) 切 live 到 post_only — damage control

**Q2**: 下一阶段 alpha 方向？
- (A) **Funding-aware Signal Filter**（我推荐，ROI 最高）
- (B) Regime-Aware Wrapper
- (C) 新 Strategy Family（factor / momentum）
- (D) 先多看数据

### 我的默认行动（若用户不指示）
- **静默等待**：live 不动，shadow 继续跑
- **下一 autonomous 窗口**：若用户授权继续，我会实现 P1 (funding filter POC)

---

## 给用户的一句话

**AATS 不是缺"量化方向"—— 它已经是。缺的是 signal edge > fee 的 alpha source。
本 session 证明了现有 independent strategy 没有这种 alpha。下一步应该找新 alpha
（funding filter 是我推荐的第一试），而不是继续在 execution 层面优化已经优化过的东西。**
