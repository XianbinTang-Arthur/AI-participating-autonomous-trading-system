# Microstructure silver pipeline gaps Follow-up SOW（备忘）

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。

> **文档状态**：**备忘 / 未启动**（2026-04-20 起草）
> **触发**：用户看到 Grafana `silver last-bar freshness = 30.4 min` 怀疑 pipeline 有问题；主会话起 background agent `a35ed1bb198262027` 深挖链路报告
> **性质**：数据平台（RDP）layer 的观测缺口 + 结构性 bug，**不影响当下交易**
> **工期估计（全 6 项启动后）**：2–3 天

---

## 1. 背景

用户观察到 Grafana `AATS P1-D Phase 1A Microstructure` dashboard 显示：
- `Silver last-bar freshness = 30.4 min`（红）
- `Silver 15m bars produced (24h) = 94`（黄）
- `Silver 'etl_failed' count (24h) = 0`

但查 DB 后发现 silver 表当前状态（主会话 2026-04-20 实测）：

```
silver.market_trade_flow_15m         max(ts)=2026-04-20 14:45 UTC  落后 ~8h
silver.market_orderbook_metrics_15m  max(ts)=2026-04-20 22:45 UTC  落后 ~20m（正常）
silver.market_oi_funding_metrics_15m max(ts)=2026-04-20 22:45 UTC  落后 ~20m（正常）
```

Agent 深挖后确认**当前快照下所有 5 表已对齐到 22:45 UTC**（用户看到的是更早快照），但追溯发现 **16 个连续 15m bars 缺失**（2026-04-19 17:00–20:45 UTC 窗口），并找出 **6 个独立的结构性问题**，其中 2 个 P0 必须治。

**这是数据平台的治理空间**，不影响交易主链路（decision / execution / gateway / market 都正常，silver 是 research 数据链）。

---

## 2. 6 个 Agent 发现（按优先级）

### P0-1 — Scheduler daemon 停机后 gap 永不补

**位置**：[`aats/data_platform/operations/workflow_scheduler.py:675-690`](../../aats/data_platform/operations/workflow_scheduler.py)（`_enqueue_due_workflows_locked`）

**Bug**：scheduler 每次 tick 只计算 `_latest_slot_for_schedule(now)` 一个 slot，`if last_processed_slot == slot_key: skip`。如果 daemon 停机 4h，重启后 `slot_key` 直接跳到最新 15min 窗口，**历史错过的 15 个 slot 永远不入队**。

**证据**（agent 查 DB）：
- `silver.market_trade_flow_15m` 缺失 16 个连续 bars（2026-04-19 17:00–20:45 UTC）
- `governance.rdp_task_queue` 在该窗口内 **0 task**
- `bronze.market_trades` 在同窗口 `count=0`（collector 也断了）
- `aats-rdp-daemon Status=Up 27 minutes`（确实刚重启）

**修法**：`_latest_slot_for_schedule(now)` 换成 `_slots_since(last_processed_slot)`，一次 enqueue 多个 slot（每个 task 不同的 `earliest_start_at` 避免 stampede）。

**独立性**：✅ 纯 scheduler 本地改动，不影响主交易。

---

### P0-2 — Bronze 无 retention 清理脚本

**证据**：
- `grep "DELETE FROM bronze.market_trades"` → 仅出现在设计文档里
- `aats/data_platform/` 和 `scripts/` 里无对应清理脚本
- [`docs/review/p1d_phase1a_stage2_completion_2026_04_20.md:190`](../review/p1d_phase1a_stage2_completion_2026_04_20.md) 明确 "`rdp_microstructure_retention.py` 在 Stage 3/4 scope"（从没实现）

**影响**：`bronze.market_trades` 每天 ~300 MB 无限增长。30 天后磁盘 / 查询性能会逐渐恶化。

**修法**：补 `rdp_microstructure_retention.py` + workflow 配置，对 market_trades / bbo / books5 / staging.market_oi_funding_ticks 各自 retention（参考设计：trades 30d / bbo 14d / books5 14d）。~50 行 SQL + CLI + workflow JSON。

**独立性**：✅

---

### P1-3 — Bronze 空时 silent 写 NULL row 计成功

**位置**：[`aats/data_platform/merge/microstructure_silver_merger.py:442-448`](../../aats/data_platform/merge/microstructure_silver_merger.py)（trade_flow）+ 同样模式在 `liquidation` 行 1014-1016；orderbook/oi_funding 通过 SQL `AVG(NULL)=NULL` 天然写 NULL 行。

**Bug**：bronze 空 → silver merger 写一行全 NULL（`quality_flags=['trades_no_data']`, `trade_count=0`）但 **return 1**（视为 success）→ workflow exit 0 → task_queue `done/0` → Grafana `etl_failed=0`。

**现象**：collector 断 4h，`etl_failed count=0` 看起来完全正常，**但 silver 实际里面都是 NULL 行**。用户当时看到 `Silver 15m bars produced (24h)=94`（黄）而不是正常的 96（24h/15m），那 2 条缺失的"假成功 NULL 行"是 silent skip 证据。

**修法**（两选一）：
- **选项 A**（数据层面）：`trade_count=0` 时 `return 0` 并 raise marker exception，让 workflow 知道 bronze 缺数据
- **选项 B**（observability 层面）：加独立 metric `microstructure_silver_bars_with_no_data_total` counter，Grafana 加面板 + Loki 告警 `quality_flags @> ARRAY['trades_no_data']` 连续 N 次

**选 B**——更稳，不改 merger 核心语义，只加观测。

**独立性**：✅

---

### P1-4 — Runner 没 watermark，backfill 跳不过中间 gap

**位置**：[`scripts/rdp_build_microstructure_silver.py:289-298`](../../scripts/rdp_build_microstructure_silver.py) + [`microstructure_silver_merger.py:1615-1646`](../../aats/data_platform/merge/microstructure_silver_merger.py) `latest_complete_bar`

**Bug**：每次 scheduler tick 算 `latest_complete_bar(lookback_bars=1)`，不读 silver 自身 `max(ts)` 做水位线。手动跑 `--backfill-bars=N` 只回溯最近 N 个，**跳不过中间 gap**（1..N 连续回溯，不是缺失扫描）。P0-1 缺的 16 个 bars 只能靠一次性脚本 `scripts/maintenance/microstructure_silver_catchup_20260420.py`（手工产物）补。

**修法**：workflow 每次 tick 跑 `SELECT max(ts) FROM silver.market_trade_flow_15m`，从 `max_ts + 15m` 回填到 `latest_complete_bar`，上限 cap 64 bars（防爆）。配合 P0-1 即可完全自动补 daemon 停机产生的 gap。

**独立性**：✅（依赖 P0-1 完成效果最佳，但可以独立合入）

---

### P2-5 — `oi_price_regime` 永远是 None

**位置**：[`microstructure_silver_merger.py:793-823`](../../aats/data_platform/merge/microstructure_silver_merger.py)

**Bug**：`price_change_bps` 因为 Stage 3 v1 注释里明确写 "Phase 2A 加 `mid_price_ref_prev` 列后才真实计算"（行 798-799 是**空 if 块**）。下游 `if oi_delta is not None and price_change_bps is not None` 恒为 False → `oi_price_regime` 永远 NULL。

**影响**：Gold 层或 research 消费 `oi_price_regime` 的逻辑都收不到信号。是 **已知的 Phase 2A scope**，不是 bug——但目前的状态应该在 Gold 层文档里明确说明"此字段永远 NULL，待 Phase 2A 补"。

**修法**（两选一）：
- **选项 A**：Phase 2A 实施时加 `mid_price_ref_prev` 列（大改）
- **选项 B**：本 bar 读上一 bar silver row 的 `mid_price_ref`（最小改）

**独立性**：✅ 单表改动

---

### P2-6 — `etl_failed` counter 不区分 PARTIAL vs EMPTY

**位置**：[`scripts/rdp_build_microstructure_silver.py:238-250`](../../scripts/rdp_build_microstructure_silver.py) + metrics registry

**Bug**：`_build_trade_flow` 空数据 return 1 → merger 无 tables_failed → runner exit=0 → task_queue `done/0` → Grafana `etl_failed=0`（看起来一切正常）——但 **数据上 `trade_count=0`**。observability 太粗，无法区分"ETL 本身失败"和"ETL 成功但输入为空"。

**修法**：metrics registry 加 `microstructure_silver_bars_with_no_data_total_{table}` counter，Grafana 加面板分 PARTIAL/EMPTY；Loki 日志在 `COMMITTED` 时根据 `all_zero + no_data flags` 再分一级 `COMMITTED_BUT_EMPTY`。

**独立性**：✅（P1-3 选项 B 的扩展）

---

## 3. Agent 扫过但排除的点（非 bug）

Agent 沿链路完整扫过，以下均**健康**：
- **SAVEPOINT 串链失败**：P0-a 已用 `begin_nested()` 修
- **EMA 递归竞态**：同 bar 两次 UPSERT 因 `session.begin_nested()` 幂等安全
- **Auto-retry 循环**：`process_one_task` 行 399-448 有 `auto_retry_of_<task_id>` 前缀防循环 + `db_create_task_if_idle` 的 active-task idempotency
- **Per-table quality_flags 污染**：`_quality_flags_for_table` 已做 per-table 过滤（行 1327-1375）
- **数值 overflow**：P0-a 已修 `vol_weighted_tfi` NUMERIC(14,8) → (28,10)

---

## 4. 启动条件

**不立即做**。触发启动的条件（至少命中一条）：

- 生产实际出现 daemon 停机 > 1h 后发现 silver 链路有 gap
- Research / Gold 层消费 silver 数据时发现 gap 或 NULL 导致的异常
- Bronze 层磁盘使用率 > 70%（P0-2 紧急）
- 7 天观察窗内 silver `etl_failed count` 看起来正常但实际 silent skip 被发现

**永久搁置条件**：
- P1/P2 都无实际生产问题显现
- P0-1 可以在 daemon restart 后人工跑 `scripts/maintenance/microstructure_silver_catchup_*.py` 补齐（现有兜底手段）
- P0-2 在磁盘接近满之前用 `VACUUM FULL` 或手工 DELETE 兜底

---

## 5. 启动时的推荐顺序

```
P0-1 (scheduler gap 补) → 首先做，消除停机后数据缺口的结构性原因
P0-2 (bronze retention) → 跟进，消除长期磁盘隐患
P1-4 (runner watermark) → 和 P0-1 配合，实现自动 gap 补齐
P1-3 (silent skip 观测) → 加监控，让未来断 collector 不再蒙在鼓里
P2-5 (oi_price_regime) → 如果 Gold 层要用，否则永久搁置
P2-6 (metrics 细分) → P1-3 的自然延伸
```

---

## 6. 相关资产

- Agent 完整调查报告：主会话 JSONL（`a35ed1bb198262027.output`）
- 启动时先读：
  - [`aats/data_platform/operations/workflow_scheduler.py`](../../aats/data_platform/operations/workflow_scheduler.py)
  - [`aats/data_platform/merge/microstructure_silver_merger.py`](../../aats/data_platform/merge/microstructure_silver_merger.py)
  - [`scripts/rdp_build_microstructure_silver.py`](../../scripts/rdp_build_microstructure_silver.py)
  - [`scripts/rdp_task_daemon.py`](../../scripts/rdp_task_daemon.py)
  - 一次性 catchup 工具（作为手工补救模板）：`scripts/maintenance/microstructure_silver_catchup_20260420.py`
- 相关 Stage 完成报告：[`docs/review/p1d_phase1a_stage2_completion_2026_04_20.md`](../review/p1d_phase1a_stage2_completion_2026_04_20.md)

---

## 7. 审批记录

| 角色 | 姓名 | 日期 | 意见 |
|---|---|---|---|
| 起草 | Claude | 2026-04-20 | 备忘状态，未启动 |
| 触发启动 | — | — | 等上述启动条件命中 |
