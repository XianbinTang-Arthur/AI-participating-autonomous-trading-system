# Path C Fix 3 后续任务：Fee drift / Cost margin / BLOCKED 订单告警

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


**日期**: 2026-04-19
**状态**: 待启动（已从本次 session 范围剥离）
**来源**: `docs/review/cost_audit_live_reconciliation_2026_04_19.md` §7.3 + §10

---

## 为什么从本次 observability fix session 剥离

Fix 1（execution_style 顶层落库）和 Fix 2（raw_exchange 白名单透传）都是**数据层 schema 扩展**，改 3 个文件、加 11 个单元测试即可落地。

Fix 3（告警监控）不同 —— 它涉及：
- Prometheus metrics 框架扩展（新指标定义 + 暴露点）
- Grafana 告警规则 / alerting 模块集成
- RDP daemon 或其他常驻计算服务的新责任
- 对现有 P90/P95/异常检测基础设施的理解和扩展

实施需要先做**监控框架现状盘点**，再决定增量点。贸然在本次 session 内实施会扩大范围、且没有精确入口点（Path C 报告没有给出具体的 `aats/metrics/...` 文件名）。

---

## 三个告警的具体需求

### 告警 1：Fee drift 监控

**依据**: `docs/review/cost_audit_live_reconciliation_2026_04_19.md` §7.2 建议点 3

**意图**: 滚动 7 天扫 `execution_fills.raw_payload.fill_event`，计算 `derived_fee_bps = abs(fee_amount) / (fill_qty * fill_price) * 10000` 的 mean/p95，触发阈值告警。

**建议阈值**:
- **warning**: 7 天 mean > **5.5 bps** 或 stdev > **0.5 bps**
- **critical**: 7 天 mean > **6.0 bps**（说明系统真实成本显著偏离 OKX taker 5 bps 基准）

**数据源前置依赖**：
- **已就绪**：fee_amount 在 `FillEvent` 里
- **新赋能**：`FillEvent.raw_exchange.feeRate` 提供 OKX 端原始报价，可与系统侧计算 cross-check

### 告警 2：Cost margin 监控

**依据**: `docs/review/cost_audit_live_reconciliation_2026_04_19.md` §5.3 + §6.1

**意图**: 单笔订单的 `total_entry_cost = fee + slippage` 接近 `strategy_hedge_independent_max_acceptable_cost_bps` 时告警。Path C 实测 max=7.271 vs 阈值 7.5，**裕度仅 0.23 bps**。

**建议阈值**:
- **warning**: 单笔 total_entry_cost > `max_acceptable_cost_bps × 0.9`（7.5 × 0.9 = 6.75 bps）
- **critical**: 单笔 total_entry_cost > `max_acceptable_cost_bps × 0.95`（≈ 7.125 bps）

**数据源**: `execution_orders.raw_payload.fill_event` + `submission_payload.referencePrice` + `average_fill_price`

### 告警 3：BLOCKED 订单 `okx_close_only_without_reducible_position`

**依据**: `docs/review/cost_audit_live_reconciliation_2026_04_19.md` §2.4 + §10 疑问 4

**意图**: Path C 发现 3 条此类 BLOCKED 订单，疑似 close_only 判定和实时仓位读取之间的 race condition。告警触发后人工排查（单次是偶发、连续发生才是 bug）。

**建议阈值**:
- **warning**: 24h 内出现 ≥ **2 笔** `cancel_reason=okx_close_only_without_reducible_position`
- **critical**: 24h 内出现 ≥ **5 笔**

**数据源**: `execution_orders` 表 `cancel_reason` 列

---

## 实施前置工作清单

在动任何代码之前，必须先完成：

1. **盘点现有监控框架**：
   - Prometheus metrics 在哪定义？哪个容器暴露 `/metrics` 端点？
   - Grafana 告警规则在哪？`configs/grafana/` 下的 provisioning 文件位置？
   - 是否有现成的 "fill 扫描" periodic worker 可复用？

2. **决定 metric 暴露点**：
   - 写入 `execution_fills` 时直接 increment counter / update gauge（inline）
   - 或独立 worker 定期扫表算（offline）
   - 哪种对生产延迟 / CPU / 磁盘影响最小？

3. **确认告警传递通道**：
   - Grafana → Telegram / Email / Slack 哪个？
   - 是否已有 credentials 配置？

---

## 建议执行方式

**独立 spawn 调研任务**（不直接实施），产出:

`docs/design/path_c_fix3_monitoring_implementation_design_2026_04_20.md`

包含:
- 现有监控框架盘点结果
- 3 个告警的具体 metrics / alerts 实施路径
- 代码改动文件清单 + 工期估算
- 与现有告警的冲突 / 重叠分析
- 告警疲劳（false positive）风险评估

**盘点和设计完成后**再决定是否进入实施阶段。

---

## 与 P1-D 的协同

P1-D Microstructure 研究需要扩 RDP ingest 管道 + 新 Silver 表，自然会**加很多新的监控点**（WS 断线、采集延迟、backfill 完整度）。可以考虑：

- 把 Fix 3 的监控设计与 P1-D 的监控需求**合并立项**，避免两次建设
- 或者 Fix 3 先做（~1 人周），P1-D 复用其框架

**优先级**:
- 当前生产零订单 → Fix 3 告警**无数据可观察**，暂无紧迫性
- 等 P1-D 实施 Phase 2A 开始首次订单流 → 此时 Fix 3 告警真正有用

**结论**: **Fix 3 可以和 P1-D 一起做**，不急在本 session 内独立完成。

---

## 签署

- 剥离决策：2026-04-19 session（H4 + Path B + Path C + P1-D spawn 这一波）
- 相关上游报告: `docs/review/cost_audit_live_reconciliation_2026_04_19.md`
- 下一步: 等用户决策是否立即启动 Fix 3 设计 spawn 或与 P1-D 合并
