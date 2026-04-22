# 2026-04-23 · OKX fee 真相 + LF-021 关闭 + 下一阶段方向

> 自组织超级管理员模式第一轮迭代产出。Dependency Researcher 和 Backend
> Engineer 协同交付 shadow 扩展 + fee 调研两条独立主线，落地为本文档。

---

## TL;DR

1. **OKX 没有公共 fee API** — 调研 agent 用 HTTP + OKX 官方公告 HTML 交叉验证。
2. **Regular 用户 BTC-USDT-SWAP 实际费率 = maker 2.0 bps / taker 5.0 bps** ——
   **匹配 `trade_cost_derivatives_maker_fee_bps = 2.0` / `taker = 5.0` 的代码 default。**
3. **Maker rebate 从 VIP 7 起**（需 ≥$1.5B 月合约量或 ≥$100M 账户）——
   $390 账户**永远拿不到**。
4. **因此 LF-021 "改 cost 模型加 maker rebate" 应当 CLOSE** —— cost model 没 bug，
   它从一开始就是对的。
5. **真正的 cost 改进路径**: 不是 rebate，而是 **taker 5 → maker 2 = -3 bps**
   （把 entry 从 IOC 切到 post_only）。**这条路径恰好是正在跑的
   `entry_post_only` shadow candidate**。
6. **3 bps 正好是 04-22 那 165 次决策跨越 0 bps 门槛的幅度** —— 所以
   `entry_post_only` shadow 的价值比之前预估得高。如果 3-5 天数据表明
   成交率损失不超过 ~60%，切 live 的 ROI 就显著为正。

---

## Section A · OKX fee 数据调研（Dependency Researcher 回传）

### 1) 公共 API 可用性

| 尝试 | 结果 |
|------|------|
| `GET /api/v5/public/fee-rate` | **404 Not Found** |
| `GET /api/v5/account/trade-fee?instType=SWAP&instId=BTC-USDT-SWAP` | **50103** — 需 API key 签名 |
| `GET /api/v5/public/instruments?...` | ✅ 可匿名，但**不返回 fee** |

**结论**：OKX 未暴露公共 fee endpoint；私有 endpoint 需签名。唯一公开、稳定、
可抓取的费率数据源是 OKX 官方公告页（HTML 静态）。

### 2) 真实费率（2026-04-08 生效，BTC-USDT-SWAP ∈ Group 1）

| VIP | Group 1 maker | Group 1 taker |
|-----|---|---|
| **Regular（当前账户）** | **0.0200% = 2.0 bps** | **0.0500% = 5.0 bps** |
| VIP 1 | 0.0160% | 0.0450% |
| VIP 2 | 0.0150% | 0.0360% |
| VIP 3 | 0.0100% | 0.0280% |
| VIP 4 | 0.0080% | 0.0270% |
| VIP 5 | 0.0050% | 0.0260% |
| VIP 6 | 0.0000% | 0.0250% |
| **VIP 7** | **-0.0020% (rebate -0.2 bps)** | 0.0200% |
| VIP 8 | -0.0050% (-0.5 bps) | 0.0200% |
| VIP 9 | -0.0050% (-0.5 bps) | 0.0150% |

**定级门槛**：VIP 1 = ≥$100k 资产或 ≥$5M 月合约量。$390 账户距离 VIP 1
都差 250 倍，短期（数月至年计）谈 rebate 无意义。

### 3) Market Maker Program

- 存在，但邮件申请，具体 rebate **不公开**
- 与 VIP 互斥（MM 成员不享受 VIP 促销）
- **对 $390 账户不在视野范围**

### 4) 数据时效 + 可信度

- 采集日期：2026-04-23
- 费率生效：2026-04-08（已生效约 2 周）
- 官方来源：`https://www.okx.com/en-us/help/advance-notice-adjustment-to-vip-tier-and-future-fees`
- 独立交叉核对：datawallet.com（Regular 2/5 bps 一致）

---

## Section B · 对 AATS 的直接应用

### B1) LF-021 关闭

**原因**：cost model defaults（maker 2.0 / taker 5.0 bps）**就是对的**。
`EffectiveFeeResolver` 不需要改 —— 它已经在正确轨道。

### B2) cost 减轻的真正路径 = entry 模式切换

`aats/services/strategy_engines/independent/execution_policy.py` 里：

| 模式 | ordType | 费率 |
|------|---------|------|
| `passive_first`（当前 live） | IOC | **5 bps taker**（100%）|
| `post_only_with_timeout_fallback`（shadow） | post_only + timeout fallback | **2 bps maker**（成交时）|

**3 bps 改进不是假设，是 OKX 官方费率表直接给出的硬数字。**

### B3) 04-22 深挖报告数据重解读

先前报告（`2026_04_22_decision_history_deep_dive.md`）说：

> 04-22 有 165 次 net_edge ∈ [0, 1.18] bps，"cost 端改进 1-2 bps → 全部
> 变 2-3 bps net"

**修正**：cost 端改进不是 1-2 bps，而是如果从 IOC 切到 post_only 成交，是
**3 bps 整**（taker 5 → maker 2）。所以不仅这 165 次，还会包括一部分
`net_edge ∈ [-1, 0)`（当前 blocked）的决策变为正。

**但有代价**：post_only 可能 **不立即成交**（等挂单 match），极端情况下根本没成交。
这个概率现在无法从理论推断 → **必须靠 shadow 数据实测**。

---

## Section C · 当前 3-candidate shadow 状态（commit 81f74d5）

| candidate | 干预维度 | override | 预期解锁 |
|-----------|----------|----------|----------|
| `entry_post_only` | entry mode | IOC → post_only | cost 5→2 bps **IF 成交** |
| `edge_buffer_1bps` | 噪音缓冲 | 2.0 → 1.0 bps | signal - cost ∈ [1, 2) 的决策 |
| `min_net_edge_1bps` | 最小净边际 | 2.0 → 1.0 bps | net_edge ∈ [1, 2) 的决策 |

**产出确认**（最近 3 分钟 / deploy 后）:
- entry_post_only: 9 decisions
- edge_buffer_1bps: 3
- min_net_edge_1bps: 3
- 0 个 paper_trading_shadow_*_failed
- 24/24 shadow 单测绿

---

## Section D · 3-5 天后的关键决策点

shadow 累积 3-5 天后（signal 活跃期预计在那之前到来），能精确回答：

1. **entry_post_only vs live (IOC)**：
   - post_only **成交率** = ?（会不会因为挂单没 match 导致大量 close_instead）
   - shadow `action_type` 分布里有多少 `hold_instead` vs `same_as_baseline`？
   - **决策规则**：若成交率 ≥ 70%，切 live 的预期净收益 > 0.5 bps/笔 → **建议切**
2. **edge_buffer_1bps vs live**：
   - 多少 `would_override_baseline = True` 次？
   - 这些 override 的 net_edge 分布是什么？
   - **决策规则**：override 次数 ≥ 20 且平均 net_edge > 0 → **建议降到 1.0**
3. **min_net_edge_1bps vs live**：
   - 同上，但针对 min_net_edge gate

三个维度可能有交叉效应（entry_post_only 降成本 + edge_buffer_1bps 降门槛
= 多次解锁），但 MVP shadow 暂不跑组合 candidate，保持归因清晰。

---

## Section E · 下一阶段方向（Backtest MVP）

**为什么需要 backtest**：

- Shadow 只能看"这个决策在当下是否会触发 override"
- **不能**看"如果当时下单了，实际成交价格、fill 节奏、累计 PnL 如何"
- 科学的参数扫描（edge_buffer × min_net_edge 的 3×3 矩阵等）shadow 需 9 candidates × 3-5 天 — 太慢
- Backtest 能在几分钟扫完整个矩阵

### E1) 重大发现 — AATS 已有 Replay Core（80% 基础就位）

Explore agent `abde0b98e315626e5` 调研结果：`aats/data_platform/replay/` 下已有相当完整的基建：

| 已有组件 | 文件 | 职责 |
|---------|------|------|
| Replay Runner | `core/replay_runner.py` (173 行) | 从 Gold 层读 candle，按 bar 调 adapter 得决策 |
| Replay Context | `core/replay_context.py` (595 行) | 共享数据模型（ReplayBar / ReplayDecision / ReplayCostConfig...）|
| Independent Adapter | `adapters/independent_adapter.py` (543 行) | 评分 + 稳定性 + edge 分解 — **完整** |
| Directional Adapter | `adapters/directional_adapter.py` (377 行) | 最小实现 |
| Parameter Scan | `scan/scan_runner.py` (356 行) | 网格扫描框架 |
| Diagnostics | `diagnostics/replay_diagnostics.py` | opening_count / blocked_count / edge 分布 |
| Registry | `registry/experiment_registry.py` | PostgreSQL 实验元数据 |
| Writers/Reports | `core/replay_result_writer.py` + `reports/markdown_report_builder.py` | CSV/JSON/markdown 输出 |

**已能**：历史 candle → replay → 决策序列 + 诊断。
**未能**：实际 fill 模拟、PnL 累加、equity curve。

### E2) MVP 补齐清单（5 个新组件）

新目录建议 `aats/data_platform/replay/backtest/`：

| # | 组件 | 职责 |
|---|------|------|
| 1 | `fill_simulator.py` | IOC 按 bar close ± slippage 成交；post_only 按概率模型 |
| 2 | `position_tracker.py` | 维护 entry_price, current_qty, accumulated_fees |
| 3 | `equity_builder.py` | 逐 bar 累计 equity；cumulative_pnl / max_drawdown / Sharpe |
| 4 | `cost_validator.py` | 实际 vs assumed cost diff（发现 cost model 偏差）|
| 5 | `backtest_harness.py` + `aats/cli.py` 加 `backtest` 子命令 | 协调器 + CLI 入口 |

### E3) 可直接复用的既有代码

- `replay_context.py:49-127` — `ReplayCostConfig` 费率混合计算（可直接移植 FillSimulator）
- `independent_adapter.py:196-270` — 评分 + 稳定性（无改动）
- `execution_realism/slippage_estimator.py` — 滑点估算（CostValidator 参考）
- `execution_realism/fill_feasibility.py:43-99` — 成交可行性分类（需改造为概率）
- `tests/unit/test_independent_replay.py` — 单测模板

### E4) 三个待决策的设计问题（下一轮 Architect 处理）

**Q1. IOC 订单滑点假设**：`fill_price = bar_close ± slippage_bps` 够不够？BTC-USDT-SWAP 流动性强，< 1 bps；1 bps defensive 默认合理。

**Q2. post_only 成交概率模型**（最关键）：
- A. volume_ratio 分段：<1% → 90%；1-5% → 60%；5-10% → 20%
- B. 波动率代理
- C. shadow + live 实测校准（之后可用）
- **MVP 建议 A，3-5 天后用 shadow 数据校准**

**Q3. 跨 bar 时间语义**：决策在 bar close，成交在本 bar close（乐观）vs 下一 bar open（真实）？**MVP 建议**：本 bar close + 1 bps slippage buffer（简单 + 略悲观）。

### E5) MVP 验收

```
python -m aats.cli backtest \
  --symbol BTC-USDT-SWAP --timeframe 1h \
  --start-date 2026-03-01 --end-date 2026-03-31 \
  --family independent \
  --overrides entry_execution_mode=post_only_with_timeout_fallback \
  --output backtest_result.json

Output:
  summary: {pnl, sharpe, max_drawdown, fill_count, fee_total}
  equity_curve: timeseries of (ts, equity, drawdown)
  trades: per-trade breakdown
```

### E6) 启动时机

**当前不立即启动**。原因：
- Shadow 收集才第 1 天，数据太少
- Q2 的 post_only 成交率模型校准依赖 shadow + live 交叉验证
- 本轮已出 MVP sketch，下一轮就可进入实施

**触发下一轮实施的条件**（任一即可）：
- 3-candidate shadow 稳定跑 3-5 天 → 可用真实数据校准 Q2
- 用户明确要求"立刻开始 backtest MVP"

---

## Session 记录

- 本轮 commits:
  - `b8347b8` feat(paper-trading): 启用 entry_post_only candidate
  - `81f74d5` feat(paper-trading): 扩展 shadow 到 3 candidate
- 新文档: 本文件 `2026_04_23_okx_fee_research_and_next.md`
- LF-021: 建议 CLOSE，理由见 Section B1
- 已消化: Dependency Researcher (fee API 调研) + Explore agent (replay 基建调研)

## 下一轮角色分化建议

根据 Review Rule：

| 角色 | 状态 |
|------|------|
| Dependency Researcher | ✅ 完成使命，淘汰 |
| Backend Engineer | 暂停（等 MVP 触发条件）|
| Test Engineer | 暂停 |
| **System Architect** | **主力（下一轮）** — 基于 E2-E4 出 MVP 详细设计 |
| **Documentation Writer** | 本轮交付本文档，淘汰 |
| Performance Engineer / Debug Investigator / Refactoring Specialist / Security Auditor / Frontend / Task Planner | 均不适用 |
