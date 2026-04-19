# P1-D Microstructure Kickoff 决策确认 (2026-04-19)

**状态**: 用户批准，Phase 1A 启动 runway 就绪
**前置依据**: `docs/design/p1d_microstructure_feasibility_2026_04_19.md`
**批准人**: 用户（"按照你的推荐"，2026-04-19）

---

## 1. 背景 Recap

P1-A CHASE 方案和 P1-C FADE 方案均被证伪（OHLC 15m 无 alpha）。
P1-D Microstructure 被批准为下一阶段主线，探索 orderbook / trade flow /
OI delta 等非 OHLC 派生特征的预测力。

可行性调研结论：**GO Phase 1**，预计 6-8 周到 W8 最终 GO/NO-GO。

---

## 2. 8 个疑问的最终决策

### 需用户确认的 2 个

| # | 疑问 | 决策 | 决策人 |
|---|---|---|---|
| Q6 | Phase 2A gate fail → 提前 NO-GO 吗？ | **A. 允许提前 NO-GO** — 不沉没 6 周机会成本，立即转 γ 路径 | 用户 2026-04-19 |
| Q8 | Decision trigger 可否灵活调 5m？ | **A. 允许 Phase 2 发现需要时切 5m** — 让数据说话 | 用户 2026-04-19 |

### 6 个由 Claude 自主决策

| # | 疑问 | 决策 | 理由 |
|---|---|---|---|
| Q1 | OKX VIP 级别？ | **VIP0**（生产 taker fee 5 bps = 常规档） | Phase 1 所需频道 `books` / `books5` / `bbo-tbt` / `trades-all` 全部 VIP0 可订；VIP 升级在 Phase 2B 发现 OFI 有显著 edge 后再评估 |
| Q2 | aats-market 容器能否承载新 collector？ | **W0 pre-work 实测 baseline 后决定** | 调研报告 §9 明确要求"实际部署一次 stub collector 做 baseline" |
| Q3 | NATS 隔离策略？ | **新 collector 不发 NATS，只写 DB Bronze 表** | 避免污染现有 `market.snapshot.*` topic；Silver ETL 从 DB 读，解耦 |
| Q4 | BTC-only 还是 BTC+ETH？ | **Phase 1 仅 BTC-USDT-SWAP** | 调研报告 §9 评估 BTC+ETH 存储/工期 ×1.5；ETH lead-lag 预期 R² <0.005；Phase 2B 末尾再评估扩 ETH |
| Q5 | Path B 是否批准启动？ | **已完成** | 本 session commit `d920644` merged，14 天 retention 启用（当前 NOOP，系统累积满 14 天后自动生效） |
| Q7 | 第三方历史数据（Kaiko/Tardis.dev）加速？ | **不买** | 月费 $500-3000 / 单次 $300-800；项目偏好本地 WSL2 无云费用（user_role_aats memory）；W3 Phase 1B 的 1 周等待期可用于 Phase 2A 前置脚本开发 |

---

## 3. Phase 时间线（与 Path B 协同）

```
W0 [DONE]       P1-A 归档 + P1-C CONDITIONAL-GO + P1-D kickoff
W0+ [本次后续]   Pre-work 调研: 
                 - aats-market 容器 baseline (CPU/network/DB)
                 - 现有 market_gateway/WS collector 框架盘点
                 - Bronze/Silver migration 现有编号
                 - 产出 Phase 1A 详细实施设计文档
W1-W2           Phase 1A 实施: collector + 5 Bronze/Silver 表 + ETL
W3              Phase 1B 等数据积累 + Phase 2A 前置脚本开发
W3-W4           Phase 2A: 3 核心特征回归 (OI delta / taker ratio / top-5 OBI)
                → W4 end gate: 至少 1 特征 R² ≥ 0.01 → 继续；全 < 0.005 → NO-GO
W5-W6           Phase 2B: 剩余 2 特征 + regime slice + 交叉验证
                → W5 起 P1-C FADE 可自动复跑（event_store retention 达 30+ 天）
W7-W8           Phase 3: 综合决策 + GO/CONDITIONAL-GO/NO-GO
```

**协同 gate**:
- W5 end: P1-D Phase 2B 中途 + P1-C FADE 复跑并行
- W8 end: "四象限" 决策矩阵（P1-D GO/NO-GO × P1-C GO/NO-GO）

**NO-GO 兜底**: γ 路径（funding carry / basis arb）非定向策略

---

## 4. 核心特征清单（Phase 1 优先级）

按 ROI 排序（调研报告 §1 + TL;DR）：

1. **OI delta × sign(ΔP)** — 最便宜，已订 `open-interest`，只差 15m 聚合表 + 回归。预期 R² 0.01-0.02
2. **Trade flow aggression** (taker buy/sell ratio) — 订 `trades-all`。预期 R² 0.01-0.025
3. **Top-5 orderbook imbalance** — 订 `books5`。预期 R² 0.015-0.03（15m 聚合稀释后）

**延后到 Phase 2B**:
- Volume profile z-score
- Liquidation cascade（偏 volatility gate，不是 directional）

**可能 drop**:
- 跨品种 lead-lag（ETH→BTC）— 调研判断 R² < 0.005

---

## 5. 项目约束（红线）

本项目所有工作必须遵守：

- ✓ **不改 taker_fee 假设**（生产仍按 5 bps 计算）直到 VIP 升级确认
- ✓ **不碰 decision_engine 的 15m tick 频率**，除非 Phase 2A 明确证据要求切 5m
- ✓ **新 collector 独立 DB connection pool**，不共用 aats-market 的连接
- ✓ **不发 NATS**（Bronze 只写 DB，Silver ETL 从 DB 读）
- ✓ **Phase 1 单 symbol**（BTC-USDT-SWAP）
- ✓ **每 phase 有 git commit checkpoint**，便于回滚

---

## 6. 观测性协同

Path C Fix 3（fee drift / cost margin / BLOCKED 告警）与 P1-D Phase 1A 的监控需求合并立项：

- P1-D 天然需要 `microstructure_ws_connected` / `microstructure_ws_stale_seconds` / `bronze_row_count_last_15min` 等指标
- Fix 3 的 `derived_fee_bps` / `cost_margin` / `BLOCKED_count` 等可复用同一监控框架

**建议**: Phase 1A W1 第一天先做监控框架盘点（Prometheus + Grafana 现状），然后在同一框架下统一设计 P1-D 的新指标 + Fix 3 的告警规则。

---

## 7. 下一步

**本 session 结束前**:

- Spawn W0 pre-work 调研 agent：
  - 实测 `aats-market` 容器 baseline 资源占用
  - 盘点 `aats/services/market_gateway/` 现有 OKX WS client 框架
  - 盘点 `aats/data_platform/` Bronze/Silver migration 编号和模式
  - 盘点 Prometheus/Grafana 监控现状
  - 产出 `docs/design/p1d_phase1a_implementation_design_2026_04_20.md`

**下一个 session**（用户发起）:

- Review W0 pre-work 产出
- 决定是否 kickoff Phase 1A 实施（W1）

---

## 8. 签署

| 条目 | 内容 |
|---|---|
| 项目主题 | P1-D Microstructure 立项 |
| 批准人 | 用户（"按照你的推荐"） |
| 批准日期 | 2026-04-19 |
| 前置文档 | `docs/design/p1d_microstructure_feasibility_2026_04_19.md` (1061 行) |
| 启动状态 | W0 pre-work 已 queue，W1 待下次 session |
| 终止条件 | W4 gate / W8 final / 或用户主动终止 |
| 兜底路径 | γ 路径（funding carry / basis arb） |
