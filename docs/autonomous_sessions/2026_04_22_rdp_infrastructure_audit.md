# 2026-04-22 · RDP / 回测 / Paper Trading 基础设施审计

> **生成**：Stream C Explore agent 调研结果
> **目标读者**：用户（决策"收益杠杆 #2 怎么走"时用）
> **TL;DR**（30 秒）：**回测基础设施 80% 已就位**，主要缺 2 块：
> ① 非 AI 策略的 paper trading 开关（shadow mode 只给 AI 用）
> ② 回测结果的"realistic PnL"（currently 只出 decisions，不出 fills + 模拟盈亏）

---

## 🟢 已经有什么（比我之前估计的好得多）

### 1. RDP 数据平台（`aats/data_platform/`）

**生产级的数据 pipeline**：
- 独立数据库 `aats_research`（和实盘 DB 物理隔离 — 很好的设计）
- **7 层 schema** 架构：meta / staging / bronze / silver / gold / research / governance
- **采集器**齐全：
  - OKX REST 增量 candles（1m/5m/15m/1h）
  - 资金费率 + open interest + mark price
  - 历史回填（ZIP/CSV 批量）
  - 微观结构 WebSocket（trades / orderbook BBO / depth, 2 Hz 采样）
  - 清算数据 WebSocket
- **Gold 层**：去重 + 质量校验 + 资金费率对齐的 replay-ready bars

**对我们意味着什么**：想做"测试新策略在过去 2 年 BTC 上的表现" —— 数据基础**已经够了**。

### 2. 回测 / Replay 框架（`aats/data_platform/replay/`）

**已有完整的 bar-by-bar 回测引擎**：
- `replay_runner.py`：按时间顺序喂 bars 给 strategy adapter
- **independent_adapter.py 完整实现**：OHLCV 特征 + 打分 + 稳定性检查 + 决策状态机
- **参数扫描** (`scan/parameter_grid.py`)：默认 243 个参数组合（min_confirm_ticks × score_stability_threshold × min_safe_net_edge_bps × entry_threshold × close_threshold）
- **实验注册表**：每次扫描留痕，artifact 落盘 `artifacts/research/experiments/`

**对我们意味着什么**：想调 `entry_threshold` 或 `min_safe_net_edge_bps` —— 跑一次 scan，216 个组合对历史数据的表现**几小时**能出来。

### 3. Attribution（归因）框架（`aats/data_platform/attribution/`）

- 聚合 / 分层分类 / 报告构建已存在
- 作用：告诉你 "哪个 sleeve / family / 因子贡献了多少 PnL"
- **局限**：当前只针对**实盘** PnL，还没接上回测结果

### 4. Regime 检测（`aats/services/feature_engine/regime.py`）

- 4 种 regime：`breakout` / `trend` / `range` / `uncertain`
- 置信度 0-1
- **局限**：检测在跑，但 **策略选择不是 regime-aware** —— 当前 `strategy_family_active` 是 config 写死的 "independent"

---

## 🟡 还缺什么（按优先级）

### 🔴 Gap #1：非 AI 策略的 Paper Trading 开关

**现状**：`ai_shadow_mode_enabled=True` 只影响 AI 路径。Independent / protective / opportunistic 三个策略 family 的 `*_shadow_mode_enabled` flag **已经存在但没接线** —— 设成 true 也没用。

**影响**：想测试"改进后的 independent 策略在实盘流量下表现"，**必须直接接管实盘**（有风险）或**做回测**（不能用真实流量）。没有第三条路。

**修法**：
- 在 `decision_engine` 和 `strategy_engines` 里加 shadow-mode 分支
- 新 schema `StrategyFamilyShadowDecision`（或扩 `AIShadowDecision`）
- 写 shadow 决策但**不调 execution**

**预估**：3-4 天工作量。

### 🔴 Gap #2：回测结果没有 realistic PnL

**现状**：`replay_runner` 出 `ReplayDecision`（entry/exit 决策），但**不出 fills + 模拟盈亏**。

**原因**：Phase 2 刻意只做决策层，Phase 4 的 execution_realism (slippage / fee / fill feasibility) 已经**写了代码但没接入 replay_runner**。

**影响**：想验证"新策略 Sharpe 多少" —— 做不了，因为没有 PnL 输出。

**修法**：把 Phase 4 接入 replay_runner，输出 `ReplayResult`（含 executed_price / slippage / realized_pnl / cumulative_pnl）。

**预估**：8-12 天，**卡在 orderbook 历史数据是否完整**（需要先核查 Gold 层有没有 orderbook depth）。

### 🟡 Gap #3：自动化 backtest → governance 闭环

**现状**：扫描出的好参数只是 CSV + JSON 落盘，**没有自动推荐到 `governance.active_parameter_sets`**。

**修法**：推荐构建器（按 Sharpe / trade count / max DD 过滤）+ 审批流（人工 gate）→ active_parameter_sets。

**预估**：5-7 天，**卡在 Gap #2**（没 PnL 就没 Sharpe）。

### 🟢 Gap #4：多策略 Paper Trading 并行对比

**预估**：4-5 天，**卡在 Gap #1 + #2**。

### 🟢 Gap #5：数据完整性自动校验

（缺 / 异常 / 停盘未标记检测）—— 运维级别，2-3 天。

---

## 我对"收益杠杆 #2"的具体建议

用户您要做的决策是 **"先做 Gap #1 还是 Gap #2"**？

### 方案 A · 先 Gap #1（非 AI paper trading）
- **优势**：快（3-4 天），可立即并行 shadow 当前生产，不干扰实盘
- **产出**：能跑 "改良版 independent" vs "生产 independent" 实时对比，**用真实市场数据**
- **限制**：只能横向比"哪个更好"，不能回答"这个策略历史上赚不赚钱"

### 方案 B · 先 Gap #2（回测 realistic PnL）
- **优势**：回测后直接出 Sharpe / max DD 数字
- **限制**：8-12 天 + 可能要回填 orderbook microstructure 历史
- **风险**：回测永远有 look-ahead bias / overfit 风险，历史表现好 ≠ 未来赚钱

### 我的推荐：**A 和 B 并行但先 A**

A 更快落地、立刻能给我们"这个想法值不值得 backtest"的信号。B 作为 A 确认后的 due diligence 工具。

**具体时间线（如果您同意）**：
- Week 1：Gap #1 上线（我做）
- Week 2-3：并行跑 "改良 independent" shadow 看 7 天真实市场表现
- Week 4-6：Gap #2 上线（我做）
- Week 7+：两套一起验证，选最好的参数推进 governance

这个时间线里您只需要做 3 次决策：
1. 同意开始 Gap #1 吗？（我可以下轮 session 开工）
2. 看完 7 天 shadow 数据，是否推进该策略到生产？
3. 看完 backtest 结果，哪组参数入 governance？

其余时间我自动跑、您不用陪。

---

## 附：我今天**没做**的事

- ❌ 没动 `aats_research` DB（那是 RDP 世界，我专心 `aats_live_derivatives`）
- ❌ 没改 replay 代码（只读审计）
- ❌ 没启动任何 backtest（Stream C 就是 read-only 调研）

等您醒来读完这份，给我一个"A 开始" / "B 开始" / "都等等" / "你觉得呢"就行。
