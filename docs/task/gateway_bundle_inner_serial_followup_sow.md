# Gateway 单路查询内部串行治理 Follow-up SOW（备忘）

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。

> **文档状态**：**备忘 / 未启动**（2026-04-20 起草）
> **上游 SOW**：[`gateway_slow_query_systematic_fix_sow.md`](gateway_slow_query_systematic_fix_sow.md) 已签收（S1/S4/S5 合入）
> **前置依赖**：先跑完 7 天观察窗（2026-04-20 起），根据真实 UI 使用频率再判断是否启动
> **工期估计（启动后）**：1–2 天

---

## 1. 背景

上游 SOW 的 S1/S4/S5 全部合入（commits `2482db2`、`7c23049`、`f7ea178`）后，观察到一个**上游 SOW 未覆盖的新瓶颈**。

上游 SOW 目标：`/dashboard/bundle` 冷启动 wall P95 **<10s**。
实际：S1+S4+S5 合入后冷启动仍在 **~34s**（多次复测一致）。

不是上游 SOW 做得不到位——SOW 的 S1（count-only）/ S4（嵌套本地池）/ S5（singleflight wait 25s）全部达成各自设计目标。**真正的慢点移到了一个新层面**：`blockers` 和 `recovery` 这种**单路查询本身**就要 23–33 秒，它们不是 fan-out 场景，而是**单个函数调用内部的串行聚合**。

## 2. 证据

### 2.1 parallel_fetch_slow 日志（S1+S4+S5 落地后采样）

```
wall=33.621s queries=12 depth=1 top5=[blockers=33.571s mode_snapshot=23.042s recovery=23.016s snapshot=17.667s execution=12.077s]
wall=23.699s queries=9  depth=1 top5=[recovery=23.513s persisted_funding_fee_summary=0.025s status=0.001s exchange_funding_fee_summary=0.000s margin_buffer_risk=0.000s]
wall=23.701s queries=5  depth=2 top5=[recovery=23.698s health_blockers=11.046s readiness=6.098s snapshot=0.000s trial_guard=0.000s]
```

读法：
- `queries=12 depth=1` 意思是这轮 parallel_fetch 有 12 个并发任务，外层 panel 下钻一层（S4 引入的 `depth` 字段）
- `top5` 里 `blockers=33.571s` 表明"`blockers` 这一路**自己**就 33 秒"
- 第 2 条和第 3 条里 `recovery` 单路也 23 秒——它不是被别的路拖的，它自己就慢

对照上游 SOW 事故期的 top5 是 `decision_context_events=45s`（event_store 全量拉取），那是 fan-out 里单路慢；现在的 `blockers=33s` 是**非 fan-out 的单路**。

### 2.2 代码路径定位（粗摸）

- `aats/services/operator/query_service.py:4801 blockers()` → `_build_blockers` → [`aats/services/blocker_control/service.py:31 snapshot()`](../../aats/services/blocker_control/service.py)，内部**串行**调：
  1. `recovery = self.owner.recovery_view()` ← 就是下面那个 23s 的路径
  2. `self.owner._latest_scoped_reconciliation()` ← 查 reconciliation_reports 最新一行
  3. `self._build_items(recovery=recovery)` ← 基于 recovery 构建 blocker 列表
  4. `self._primary_task(... latest_reconciliation=...)` ← 可能再次访问 reconciliation
  5. `self._next_step_summary(... latest_reconciliation=...)` ← 同上

  `snapshot()` 总时延 ≈ max(子步骤时延) 但**是串行**，所以是 sum。

- `aats/services/operator/recovery_queries.py:21 build_recovery_view()`：内部虽然已经是 9 路 `parallel_fetch`（S4 走本地小池并行），但**单路里最慢的那一条就是 23s**。候选嫌疑（都是 "latest XXX" 语义）：
  - `_latest_scoped_reconciliation` / `latest_baseline` / `latest_operator_action("rebaseline" / "resume")`
  - `latest_topic_event_for_scope(event_store, AI_DEGRADATION_EVENTS, scope)`（扫 event_store）
  - `latest_topic_event_for_scope(event_store, AI_SHADOW_EVALUATIONS, scope)`（扫 event_store）
  - `latest_state_snapshot_for_scope(scope)`
  - `latest_baseline_generation_for_scope(scope)`
  - `latest_exchange_ack_watermark_for_scope(scope)`

  正常 "latest X for scope" 查询如果走了正确的复合索引 + LIMIT 1，毫秒级返回。23 秒说明某一路**没走索引**（全表扫）或者**内部又做了不必要的聚合**。

## 3. 摸查计划（启动时按这个顺序）

### 3.1 Phase 1 — 按路定位最慢的那一路（0.5 天）

在 `recovery_view` 的 9 路每一路前后埋 timestamp（或临时加 `perf_logger.info` 指令），跑一次冷启动抓时延分布：

```python
# 临时 instrument，摸查完删掉
import time
queries = {
    name: _wrap_timed(fn, name) for name, fn in queries.items()
}
```

或者直接改 `_parallel.py` 临时让阈值从 2s 降到 100ms，`parallel_fetch_slow` 会把**所有** top5 都打出来——这样就知道是哪一路在 23s。

### 3.2 Phase 2 — 针对最慢那一路看 SQL 执行计划（0.25 天）

拿到嫌疑方法后（假设是 `latest_topic_event_for_scope`），在 Postgres 里：

```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT ...
FROM event_store
WHERE topic = 'strategy.ai_degradation'
  AND product_type = 'derivatives'
  AND margin_mode = 'cross'
ORDER BY sequence_id DESC
LIMIT 1;
```

看是 Index Scan 还是 Seq Scan。大概率是**复合索引前缀错位**（比如只有 `(topic, sequence_id)` 但 scope 过滤走 post-filter）。

### 3.3 Phase 3 — 治方案候选（0.5–1 天）

按"最小改动 / 收益最大"排序，启动时再细化：

1. **加复合索引**：如果是 EXPLAIN 显示缺 `(topic, product_type, margin_mode, sequence_id DESC)` 之类，一条 DDL 搞定
2. **改 `latest_topic_event_for_scope` 的查询**：如果该函数目前是"先 query all topics then filter in Python"这种反模式，改 DB 层精确筛选
3. **`blocker_control_service.snapshot()` 内部并行化**：把 1-5 步骤里能并行的（`recovery_view` 和 `_latest_scoped_reconciliation` 可以并行，`_build_items` 后串行）包装成 `parallel_fetch`，利用 S4 的本地池
4. **分层缓存**：`recovery_view` 已有 35s TTL 缓存。看是否扩展给里面的重型子查询加独立短 TTL（比如 `latest_topic_event_for_scope(AI_DEGRADATION)` 给 60s TTL，因为这类事件本来就低频）

### 3.4 Phase 4 — 验证（0.25 天）

同上游 SOW 的验证套路：
- 单元/perf 测试（新增 `tests/perf/test_latest_topic_event_perf.py` 之类）
- 冷启动 wall time 前后对比，目标从 34s → <10s（达成上游 SOW §1.2 原目标）
- `parallel_fetch_slow` 日志阈值下（没 > 2s 的 top1）

## 4. 启动条件

**不现在做**，现在进 7 天观察窗（2026-04-20 起）。启动条件（至少命中一条）：

- **观察窗内用户报告"signal is aborted without reason"≥3 次**（前端 30s 超时被冷启动打爆）
- **自动采集数据显示冷启动 P95 持续 >30s**（有规律，不是偶发）
- **启用新 dashboard 或 panel 时发现性能进一步退化**（新代码路径加入）

如果 7 天窗口内冷启动频率低 + 用户未抱怨，**永久搁置**（系统热缓存命中率高就是能用）。

## 5. 不在本 SOW 范围

- event_store 分区 / 归档策略（上游 SOW §10.1 已标 follow-up）
- `historic_orphan_fill` finding_type 重命名（[`orphan_fill_misclassification_root_cause_2026_04_20.md`](../review/orphan_fill_misclassification_root_cause_2026_04_20.md) L1 提及）
- 前端 panel 的 lazy-load / 分屏渲染（UX 角度的改造）
- Grafana 10.4→12.4 升级后的面板兼容性全面 regression（另起 audit）

## 6. 相关资产指针（启动时先读）

- 上游 SOW：[`gateway_slow_query_systematic_fix_sow.md`](gateway_slow_query_systematic_fix_sow.md)
- 事故复盘：[`orphan_fill_misclassification_root_cause_2026_04_20.md`](../review/orphan_fill_misclassification_root_cause_2026_04_20.md)
- `_parallel.py` 改动（S4）：[`aats/services/operator/_parallel.py`](../../aats/services/operator/_parallel.py)
- `blocker_control/service.py`：[`aats/services/blocker_control/service.py`](../../aats/services/blocker_control/service.py)
- `recovery_queries.py`：[`aats/services/operator/recovery_queries.py`](../../aats/services/operator/recovery_queries.py)
- S1 新增的"count-only" API（可作为"不拉 payload 只要 aggregate"的范本）：[`aats/storage/event_store_postgres.py`](../../aats/storage/event_store_postgres.py) 里的 `count_by_topic_scoped`

## 7. 审批记录

| 角色 | 姓名 | 日期 | 意见 |
|---|---|---|---|
| 起草 | Claude | 2026-04-20 | 备忘状态，未启动 |
| 触发启动 | — | — | 等 7 天观察窗结论 |
