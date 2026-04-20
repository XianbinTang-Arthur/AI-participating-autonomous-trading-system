# Live Runtime 错误诊断 — 2026-04-20 deploy 后

**调查时间**: 2026-04-20 16:20 – 16:50 UTC
**作用域**: 5 类 Grafana 暴露异常的根因定位
**边界**: **只调查**，不改任何代码/配置/dashboard/容器

---

## 执行摘要

| # | Issue | 根因一句话 | Severity |
|---|---|---|---|
| 1 | `parallel_fetch_slow` wall 14-231s | `guarded_live_run_packet` 内嵌 `parallel_fetch` + `_cached_ttl` singleflight；冷启动后账户快照尚未 warm 时，多条 dashboard 请求同时竞争同 12-worker 共享线程池 + OKX REST 多次调用堆积 | **P1** |
| 2 | `decision_cycle_failed nats: timeout` | 决策引擎 `aats-decision-features_snapshots` JetStream 消费者 ack_pending 打满 256（`max_ack_pending`），pending=142K 且持续增长；publish 在反压下抛 TimeoutError | **P0 — 决策流真被阻断** |
| 3 | `noncritical_subscription_failed nats: timeout` × N | **同根因 Issue 2**。observer tier handler 要在 NATS 上 publish `system.audit_records`，被同一 NATS backpressure 超时；`ack_wait=30s` 叠加引发重投 (`redelivered=8580`) | **P0** |
| 4 | "对账与处理失败" 4-6/5min 持平 | 2026-04-17 留下 **25 条历史 local fill** (执行过真实订单) 没清理；reconciliation 循环每 65 秒重新对比一次，每次都报同一批 `local_exchange_fill_set_diverges_from_exchange_fill_set`；`aats_processing_failures_total` 永远 0 | **P2** (脏数据噪音) |
| 5 | P1-D Phase 1A dashboard 大面积"无数据" | (a) Grafana Postgres datasource 连到 **`aats` 库**（**空库**），但 bronze/silver 表在 **`aats_research`** 库；(b) dashboard 期望 `aats_microstructure_bronze_rows_written_trades_total` 等按表拆分 counter，code 只 emit 未拆分的 `_total`；(c) dashboard 期望 `_ws_reconnect_total`，code 只 emit `_ws_connect_total`；(d) Silver ETL 由 subprocess 执行且 `metrics_registry` 未传入，`_silver_etl_*` counter 永远不 emit | **P1** |

**超出预期严重的发现**：
Issue 2 + 3 不是"偶发 timeout"。`aats-decision-features_snapshots` 消费者 **pending=142,441 条、redelivered=8,580 次、ack_pending 常年打满 256**。决策引擎**根本跟不上 feature snapshot 速率**（每 run_cycle 耗时 ~17s，其中 15+ 个 OKX REST 调用；features 进入速率 >> 决策处理速率）。虽然 baseline_only 模式不下单，但**决策流确实在阻塞**，NATS 反压已到失活边缘。假如某天切到 live-submit，这个堵塞会直接转化为"错过 trade"。**应在 P0 修复**。

---

## Issue 1: `parallel_fetch_slow` wall 14-231s

### Symptom

Loki 过去 1h 在 `{logger="aats.operator_api.parallel"}` 下 39 条 WARNING，集中在 16:12–16:30 UTC 的 20 分钟窗口，其后**归零**。
最严重样本：

```
16:16:35 wall=231.212s queries=12 top5=[blockers=231.194s mode_snapshot=187.353s recovery=82.091s snapshot=17.796s execution=10.978s]
16:28:09 wall=151.284s queries=17 top5=[guarded_live_run_packet=151.227s guarded_live_preflight=131.144s event_store_archive=0.917s latest_decision=0.332s]
16:14:02 wall=86.526s  queries=9  top5=[recovery=85.955s margin_buffer_risk=0.892s ...]
```

过去 15 分钟 (16:35–16:50) 0 条慢事件 → **冷启动现象**。

### Evidence chain

1. `aats/services/operator/_parallel.py:55` — `parallel_fetch()` 使用模块级共享 `ThreadPoolExecutor(max_workers=12)`；nested parallel_fetch 降级为串行。
2. `aats/services/operator/query_service.py:1997` — `guarded_live_run_packet` → `_cached_ttl(..., 35, _build_guarded_live_run_packet)`。
3. `_build_guarded_live_run_packet` (line 2001) 串行调用 10 个子方法：`guarded_live_preflight / derivatives_live_guard / trial_guard / margin_buffer_risk / recovery_view / blockers / positions / account_state / forward_validation_report / _scoped_open_order_states`。每个都是带 `_cached_ttl(key, 35, ...)` 的独立子查询。
4. `account_state()` → `AccountQueryFacade.build_account_state` (account_queries.py:76) 内部**又开**一次 `parallel_fetch` (8 个子查询)，这时 nested 检测触发，降级为**串行**。
5. DB 层所有关键 query **都走索引、sub-ms**：
   ```
   EXPLAIN ANALYZE SELECT * FROM event_store WHERE topic='strategy.ai_degradation'
     AND product_type='derivatives' AND margin_mode='cross' AND symbol='BTC-USDT-SWAP'
     ORDER BY sequence_id DESC LIMIT 1;
   → Index Scan Backward using ix_event_store_topic_scope_seq  0.145ms
   ```
   event_store 533K 行 6 GB，但命中的 composite index 都是 sub-ms。`COUNT(*)` scan 287ms，`archive_summary()` 日志里 ~1.2s（跟 EXPLAIN 匹配）。
6. **不是 DB 慢**。slowness 来自 **并发下的 cache / singleflight wait**：
   - `_cached_ttl` singleflight wait timeout = **25s** (query_service.py:194)
   - 冷启动时 dashboard bundle 开 7 个 asyncio task → 每个都调 guarded_live_run_packet / recovery_view / blockers 等相同 key → 只 1 个 leader 真跑 loader，其余 6 个在 Event.wait(25s)
   - Leader 自己是在共享 12-worker 线程池 +  nested parallel_fetch 里串行执行 17-20 个子查询 + OKX REST 调用（账户冷启时一次 refresh 要 15 个 OKX API request，总耗时 ~7s — 从 decision 日志 16:49:31 → 16:49:35 看到）
   - Leader 卡在某个 OKX REST 或某把读锁 → 所有 follower 超时 25s 后各自 fallback 直接跑 loader → 触发二次惊群 → `recovery=85s` 出现
7. 数据库连接池侧：`pool_size=10, max_overflow=20, pool_timeout=30` — 没见 pool_timeout 抛错；4 个 `idle in transaction` 是 `pg_try_advisory_lock` 单实例锁，**by design**（4 个 process_role × 1 锁）。

### Root cause hypothesis (置信度由高到低)

1. **[主因] 冷启动 cache stampede + OKX REST 堆积**: deploy 后 `_ttl_cache` 全空，多个 dashboard panel 同时请求 `guarded_live_run_packet` / `recovery_view`，leader 等 OKX REST 返回，follower 等 25s 后 fallback 自己跑，形成惊群 → nested `parallel_fetch` 的 12-worker 共享池被挤爆。
2. [次因] `_cached_ttl` singleflight 在多 follower 下 fallback 策略放大：25s 超时后 follower 各自 `loader()`，等于 leader 没完 follower 就开始串行跑，加剧线程池饥饿。
3. [可忽略] event_store 6 GB 的 `COUNT(*)` 贡献 287ms × N 次——放大器，非根因。

### Severity 定级

**P1**。后果仅限 dashboard 开启后前 20 分钟响应变慢；baseline_only 模式下不影响交易链路（trading 在 decision 进程，与 gateway dashboard 正交）；cache warm 后自然恢复。但**用户体验差**，且 follower fallback 的惊群会加剧 DB/OKX REST 压力。

### 修复建议

1. (tactical) 把 `_SINGLEFLIGHT_WAIT_SECONDS` 从 25 → 60；延长 leader 的机会窗口避免 follower 堕落成惊群。改文件：`aats/services/operator/query_service.py:194`。
2. (structural) dashboard bundle 应**按需**请求 `guarded_live_run_packet`（只在运营面板），不要让每个 panel 独立触发冷启动路径。
3. (opportunistic) account_service startup 用**后台任务预热** account snapshot + fee schedule，gateway 启动即 warm 状态。
4. 观察：部署时 `scripts/deploy.sh --skip-commit` 的 health check 阶段应等 account_service 首次 refresh 成功再放行，避免冷启动流量打到真用户。

### 不在本诊断范围

- 不调整 pool_size / max_workers（风险是新上限下 GIL 竞争更激烈）
- 不改 `guarded_live_run_packet` 组成（业务逻辑层面）

---

## Issue 2: `decision_cycle_failed nats: timeout`

### Symptom

Loki `{logger="aats.decision_trigger"} |= "decision_cycle_failed"` 过去 10 min 4 条；典型：
```
2026-04-20T16:14:05.019Z level=ERROR consecutive_failures=1 backoff_s=2.0
  error=nats: timeout error_type=TimeoutError
  symbol=BTC-USDT-SWAP timeframe=15m
```
每小时稳定 ~24 条。`consecutive_failures=1` 说明 retry 成功。

### Evidence chain

1. `aats/services/decision_engine/trigger.py:65-87` — `handle_feature_snapshot` 入口捕获 `Exception` 记 `decision_cycle_failed`，backoff 后 continue。
2. `orchestrator.py:335` — `_publish_failure_best_effort` 有 `asyncio.wait_for(publish_model(...), timeout=5.0)`。所以 **5s timeout 直接从这里出来**。
3. 真正的 `nats: timeout` 来自 `aats/bus/nats_bus.py:1117` `await self._js.publish(...)` — JetStream publish 等 server ack，超时由 nats-py 内部 timeout 决定。
4. **NATS 端观察到真正堵塞的源头**（http://localhost:8222/jsz?consumers=true）：

| Stream | Consumer | pending | ack_pending | redelivered | ack_wait | max_ack_pending |
|---|---|---:|---:|---:|---:|---:|
| AATS_EVENTS_MARKET | **aats-decision-features_snapshots** | **142,441** | **256** | **8,580** | 30s | 256 |
| AATS_EVENTS_MARKET | aats-execution-features_snapshots | 0 | 178 | 0 | 30s | 256 |
| AATS_EVENTS_MARKET | aats-decision-market_snapshots | 0 | 143 | 0 | 30s | 256 |
| AATS_EVENTS | aats-decision-strategy_position_target | 0 | 1 | 1 | 30s | 256 |

- **pending=142K 且在 30 分钟内从 135K → 142K 持续增长**。
- `ack_pending=256 = max_ack_pending` → NATS **主动停止投递新消息**给该消费者。
- `redelivered=8,580` 次 → `ack_wait=30s` 超时 → 重投 → handler 仍没 ack → 死循环。
5. **为什么决策消费不动 features.snapshots**：
   - `decision_trigger.handle_feature_snapshot` → `orchestrator.run_cycle` → 每次 run_cycle **从 decision log 实测耗时**：
     ```
     16:49:42.804  decision_cycle_started decision_id=decision_8a9d58ff...
     16:50:00.207  decision_cycle_completed
     → 单周期 17.4 秒
     ```
   - 单周期内决策引擎调 15 个 OKX REST endpoint（account/instruments, positions, fills, bills, fee, funding-rate, system/status 等），每次 refresh 花 5-7s。
   - feature.snapshots 进入速率 ~17/min（market 进程每 15s 发一次 × 多 timeframe），决策侧最多 3-4 cycle/min → backlog 天然增长。
6. Publish 侧被 backpressure 阻断：当 features backlog 打满，JetStream 为了保护 storage（配额 8 GB、已 6.4 GB）会 throttle publish；audit.* topic 属 AATS_EVENTS stream（独立 stream，目前 pending=0），所以真正 timeout 的是 **publish 进入 stream 的 ack wait**，不是 consumer ack。

### Root cause hypothesis

1. **[主因]** decision 进程消费 features.snapshots 的处理能力（3-4 cycle/min）**远不足以** 跟上 publish 速率（17/min），导致 ack_pending 打满；叠加 `ack_wait=30s` 的超时重投 → 每条消息被重试多次，进一步消耗处理预算。
2. [次因] `run_cycle` 内 ~15 次 **同步** OKX REST 调用（`_refresh_once` 批量刷账户），单轮 7s+；应做 cache 或去抖。
3. [次因] trigger_policy 似乎没有做消息聚合（每条 feature snapshot 都被 trigger 一次）；baseline_only 模式下连续 feature 变化没必要每条都跑完整 cycle。

### Severity 定级

**P0**。原因：
- 决策流在客观阻塞。虽然 baseline_only 不下单，但 `target_position_qty=0` 的"空决策"也要走完整 cycle 并消耗资源。
- 这种 backlog 不断增长 → 未来某刻触发 NATS stream 满（已 6.4 GB / 8 GB 配额）→ publish 直接失败 → 整条决策流熄火。
- 假如切 live-submit，每条 `decision_cycle_failed` 都是"跟不上信号"。

### 修复建议

1. 把 `aats-decision-features_snapshots` 的 `DeliverPolicy` 从 `all/new` 改成**只消费最新**（可选 `DeliverPolicy.LAST_PER_SUBJECT`），或加 `filter_subject` 只订阅 1 个 timeframe。改点：`aats/bus/nats_bus.py:1146` 附近 `build_consumer_config_spec`。
2. `max_ack_pending` 从 256 → 32（小 buffer 让 NATS 不投递到 decision；client 不会被 flood）。同时把 `ack_wait` 从 30s → 90s（给 run_cycle 留 3x buffer）。
3. decision 端 `run_cycle` 内部 OKX REST 调用引入 `account_state_stale_after_seconds` 门槛（已有），但**把 ttl 从现 20s 提高到 60s**，减少 refresh 频率。
4. trigger_policy 加"上一次触发 feature 哈希未变则 skip"机制，降低无效 cycle。
5. 运营临时手段：`docker exec aats-nats nats consumer rm AATS_EVENTS_MARKET aats-decision-features_snapshots` + `docker restart aats-decision` 一次性把 142K backlog 从源头丢掉（风险：丢掉 142K × ~15 分钟的 feature 历史——在 baseline_only 下可接受）。

### 不在本诊断范围

- 不评估 trigger_policy 业务逻辑
- 不改 orchestrator.run_cycle 内部 OKX 调用链

---

## Issue 3: `noncritical_subscription_failed nats: timeout` × N

### Symptom

Loki 过去 1h 约 6-10 条（显著低于 Issue 2 的频率）：
```
2026-04-20T16:19:03.713Z level=ERROR
  handler=audit.handle_portfolio_allocation_decision
  topic=strategy.portfolio_allocation_decisions
  subscription_class=observer
  error=nats: timeout error_type=TimeoutError
```
涉及的 handler: `audit.handle_decision_outcome / handle_portfolio_allocation_decision / handle_strategy_sleeve_intent / handle_baseline_assessment`。

### Evidence chain

1. `aats/services/decision_engine/audit.py:401` — 每个 `handle_*` 最终进 `_publish_record(...)`，此方法在 line 438 调：
   ```python
   await publish_model(bus=self.bus, topic=topics.AUDIT_RECORDS, ...)
   ```
2. `publish_model` → `NatsEventBus.publish` → `await self._js.publish(subject=..., headers={"Nats-Msg-Id": ...})`。**JetStream publish ack 默认 timeout ~5s**。
3. audit topic 属 `AATS_EVENTS` stream，该 stream 本身 pending=0 但 consumer 侧 `aats-decision-strategy_position_target` ack_pending=1, redelivered=1；`aats-decision-strategy_decision_outcome` ack_pending=1。
4. 关键：`AATS_EVENTS_MARKET` stream 的 features 消费者堵塞**跨 stream 影响 NATS server** —— NATS JetStream 全局存储配额 8 GB，当前已 6.4 GB，写侧 throttle 影响所有 publish，不分 stream。
5. observer tier handler 同步 DB 写 (audit_repo.upsert) + NATS publish，两者有 `asyncio.to_thread` + await 交织，单条处理实际可能 1-3s；在 `ack_wait=30s` 下本应 OK，但 backpressure 让 publish 偶发超时。

### Root cause hypothesis

1. **[主因, 同 Issue 2]** NATS 全局存储配额接近满 (80%) + `aats-decision-features_snapshots` 消费堵塞引起的 internal write throttle，让所有 publish 的 ack 偶发 timeout。
2. [次因] audit 服务 handler 在 `_publish_record` 里对每条事件**同步 publish**，没有 batch；高频 sleeve_intents（86,407 条）下单条 publish 遇到 NATS 慢就卡。

### Severity 定级

**P0（与 Issue 2 共根因）**。非关键订阅 timeout 次数低，但**症状同源**：如果不解决 features 消费堵塞，observer tier 也会持续报。

### 修复建议

参见 Issue 2。额外：
- audit 服务现在已经有 `start_batch_writer()` (audit.py:56) 把 DB upsert 批量化，但 **NATS publish 依然每次一个 `await publish_model`**，建议把 audit → AUDIT_RECORDS 的 publish 也合批（100 条/次或 500ms 窗口）。

### 不在本诊断范围

- audit 业务逻辑 / DecisionAuditRecord 字段

---

## Issue 4: "对账与处理失败" 持续 4-6/5min

### Symptom

Grafana Operations dashboard panel "Reconciliation & Processing Failures"，过去 1h y 轴稳定 4-6 / 5min。

### Evidence chain

1. Panel query (`deploy/wsl2-dev/grafana/provisioning/dashboards/files/AATS/aats_operations.json:298,305`)：
   ```promql
   sum(rate(aats_reconciliation_mismatches_total[5m])) * 300    # 蓝线
   sum(rate(aats_processing_failures_total[5m])) * 300           # 红线
   ```
2. Prometheus 现状：
   - `aats_reconciliation_mismatches_total{instance="aats-execution:9464"}` = 99（过去 1h 从 44 → 99 = **+55/h ≈ 4.6 / 5min**），**与 panel 完全对应**。
   - `aats_processing_failures_total` **vector 为空** — 该指标从未被 export。原因：code 只 `metrics.increment("processing_failures")`（见 `aats/services/reconciliation_service/repair.py:692,723`），但 Prometheus 只能看到 **已注册的 OTel counter**；重启后 counter 首次出现需要至少一次 increment。过去 1h 没有真实 processing_failure 发生（baseline_only 不发单）。
3. 找到 reconciliation_findings 表：
   ```sql
   SELECT finding_type, count(*) FROM reconciliation_findings
   WHERE created_at > now() - interval '1 hour';
   → local_fill_missing_on_exchange | 1375
   SELECT severity, count(*) FROM reconciliation_reports
   WHERE created_at > now() - interval '1 hour';
   → SOFT_MISMATCH | 55
   SELECT reason_code FROM reconciliation_findings ORDER BY created_at DESC LIMIT 1;
   → local_exchange_fill_set_diverges_from_exchange_fill_set (severity_class=soft)
   ```
4. 回溯源头：
   ```sql
   SELECT MIN(exchange_timestamp), MAX(exchange_timestamp), COUNT(*) FROM fill_events;
   →  2026-04-17 04:49:08+08  |  2026-04-17 17:51:39+08  |  25
   ```
   **fill_events 表有 25 条 2026-04-17 的历史本地 fill**，交易所侧（OKX `/api/v5/trade/fills` 只返回最近 3 天且实盘已关户处理）返回 0 条。reconciliation 每轮（~65s）对比两侧 → 1375 findings / 55 reports = 平均每报 **25 findings**，刚好对应 25 条孤儿 fill。

### Root cause hypothesis

1. **[确认]** 2026-04-17 实际下过单、产生本地 fill 持久化，之后切到 baseline_only 后 **这 25 条 fill_events 没被清理**，也没对应 OKX 真实成交记录（或 OKX 端已超 3 天不返回）。每轮 reconciliation 必 mismatch，`aats_reconciliation_mismatches_total` 稳定 +1/cycle。
2. 这是**脏数据噪音**，不是真实 bug；严重级别 soft，**不会阻断恢复**（`review_required=false, halt_required=false`）。
3. `processing_failures_total` 在当前 baseline_only 负载下永远 0，panel 只显示 mismatches 一条线，符合现象。

### Severity 定级

**P2**。不影响安全、不影响交易；只是 dashboard 上一条持续红线和 `reconciliation_findings` 表浪费空间（已 15MB、15,907 行）。

### 修复建议

1. 清理这 25 条孤儿 fill：一次性运维脚本 — `DELETE FROM fill_events WHERE exchange_timestamp < '2026-04-18';` + 同时清 `fill_outcomes`, `execution_fills` (若有残留)、`aats:hot:order_state:*` Redis key（按 CLAUDE.md OrderState 三重持久化原则）。**前提必须**人工确认这批 fill 已在 OKX 账户里清算（2026-04-17 关户、PnL 已结算）。
2. 修改 reconciliation classifier：`local fill > 72h 且 exchange 侧无对应记录` → 标记 `historic_orphan_fill`（不计 mismatch，或 severity_class=`info`）；避免同类情况再出现。
3. panel 加一句文字注释：`当前线是 2026-04-17 历史订单的持续噪音，待清理脚本执行后归零`。
4. 解决后 `aats_reconciliation_mismatches_total` 增量即可归零，panel 变平。

### 不在本诊断范围

- reconciliation 整体架构
- 如何备份再清理（应在主任务中走"备份+设计+批准"流程）

---

## Issue 5: P1-D Phase 1A Microstructure dashboard 大面积"无数据"

### Symptom

`deploy/wsl2-dev/grafana/provisioning/dashboards/files/AATS/p1d_microstructure.json` 的 8 个 panel 显示"无数据"：
- Bronze Rows Written per 15m (by table) — Prometheus query
- Bronze trades row count (24h) — Postgres query
- Bronze trades last-write lag — Postgres query
- Silver ETL Success vs Error — Prometheus query
- Silver 'etl_failed' count — Postgres query
- 8 tables pg_total_relation_size — Postgres query
- Silver 15m bars produced (24h) — Postgres query
- Silver last-bar freshness — Postgres query

但 daily check 确认 Silver ETL 正常产出。

### Evidence chain

1. **Grafana Postgres datasource 连接错库**：
   - `docker exec aats-grafana env | grep POSTGRES` → `POSTGRES_DB=aats`
   - `psql -d aats` → schemaname 只有 pg_catalog / information_schema，**无 bronze/silver/staging**。
   - `psql -d aats_research` → `bronze: 16 表, silver: 14 表, staging: 11 表`；`SELECT count(*) FROM bronze.market_trades WHERE ts >= NOW()-interval '24 hours'` → **1,772,126** 条 ✅ ETL 正常。
   - `SELECT count(*) FROM silver.market_orderbook_metrics_15m WHERE symbol='BTC-USDT-SWAP' AND ts >= NOW()-interval '24 hours'` → **79 bars** (比 dashboard 期望的 96 bars 少，但 ETL 有在跑)。
   - **原因**：`deploy/wsl2-dev/grafana/provisioning/datasources/datasources.yml:64` `database: $POSTGRES_DB` 环境变量 = `aats`，但 bronze/silver 实际在 `aats_research`。
2. **Prometheus 名字失步（code ≠ dashboard）**：
   当前实际 emit 的 `aats_microstructure_*` counter（`http://localhost:9090/api/v1/label/__name__/values`）：
   ```
   aats_microstructure_bronze_flush_total
   aats_microstructure_bronze_rows_written_total         ← 单个，未拆
   aats_microstructure_ws_connect_total                  ← 拼写为 _connect_
   aats_microstructure_ws_messages_total
   ```
   Dashboard 期望：
   ```
   aats_microstructure_bronze_rows_written_trades_total  ← 不存在（未按表拆）
   aats_microstructure_bronze_rows_written_bbo_total     ← 不存在
   aats_microstructure_bronze_rows_written_books5_total  ← 不存在
   aats_microstructure_bronze_rows_written_oif_total     ← 不存在
   aats_microstructure_ws_reconnect_total                ← 不存在（code 只 emit _connect_）
   aats_microstructure_silver_etl_runs_success_total     ← 不存在
   aats_microstructure_silver_etl_errors_total           ← 不存在
   ```
3. **Silver ETL metrics 从未入 Prometheus**：
   - `aats/data_platform/merge/microstructure_silver_merger.py:1383` `build_silver_microstructure_15m` 有 `metrics_registry: _MetricsLike | None = None` 参数。
   - `scripts/rdp_build_microstructure_silver.py:173` 调用时**不传**该参数。
   - 且该脚本由 `rdp_task_daemon` 每 15 分钟以**子进程**方式 fork 执行（见 rdp-daemon log：`Executing: /opt/aats-venv/bin/python /app/scripts/rdp_run_scheduled_workflow.py --workflow microstructure_silver_15m`），子进程**没有 Prometheus exporter 端口**，即使埋点也无法被 scrape。
4. **WS collector metric 按表拆分未实现**：
   `aats/data_platform/collectors/microstructure_ws_collector.py:1074` 只打一次 `microstructure_bronze_rows_written_total`（单个），不按表区分。Dashboard 的 `_trades_total` / `_bbo_total` 等都是**规划中的 counter、code 从没实现**。

### Root cause hypothesis

1. **[主因 a]** Grafana Postgres datasource database 环境变量错配 — `POSTGRES_DB=aats` 应为 `aats_research`。所有 SQL 面板直接返回 0 行。
2. **[主因 b]** Dashboard 的 Prometheus expr **领先于** code：按表拆分的 bronze counter、silver ETL counter、ws_reconnect counter 都是设计层面规划，code 还没落地。
3. **[主因 c]** Silver ETL 以子进程方式运行，天生无 Prometheus 端口暴露 — 即使把 `metrics_registry` 传进去也打不到 scraper。

### Severity 定级

**P1**（诊断侧）。不影响交易链路；但 P1-D 的验收门禁（§11 Gate 1-5）全部挂在这个 dashboard 上，**现在相当于门禁失明**。

### 修复建议

1. **立刻修 (a)**：Grafana datasource.yml 或 env 里把 `POSTGRES_DB` 从 `aats` 改成 `aats_research` —— 或者在 dashboard SQL 里 explicit `FROM aats_research.bronze.market_trades` 带 dbname（Postgres 不支持跨 DB，所以必须改 datasource）。改点：`deploy/wsl2-dev/grafana/provisioning/datasources/datasources.yml:64` 或 compose 环境变量。
2. **中期修 (b)**：让 collector `_metric_inc` 对每张表分别 emit：改 `microstructure_bronze_rows_written_total` → `_written_trades_total / _bbo_total / _books5_total / _oif_total`。改点：`aats/data_platform/collectors/microstructure_ws_collector.py:1074` 附近。
3. **中期修 (b)**：collector 代码里把 `_metric_inc("microstructure_ws_connect_total")` 区分 initial connect vs reconnect（reconnect 独立 counter）。改点：`aats/data_platform/collectors/microstructure_ws_collector.py:1201`。
4. **结构修 (c)**：Silver ETL 子进程结束前把 `metrics_registry.snapshot()` 通过 HTTP POST 或 file 传给 rdp-daemon 长驻进程，由后者合并到自己的 Prometheus 端口。或者把 Silver ETL **改成 rdp-daemon 进程内直接调**（asyncio task），不再 fork 子进程。
5. **备选**：先把 dashboard 里那些 **永远空** 的 panel 隐藏或加文字说明（"pending metric instrumentation"），避免用户误判系统坏掉。

### 不在本诊断范围

- Silver ETL 的 bar 覆盖率偏低（79 / 96） — 另案
- 是否切换到 postgres-exporter 替代 dashboard 里的 pg_total_relation_size SQL — 另案

---

## 附录：Postgres & NATS 全局指标

```
# Postgres aats_live_derivatives 库
event_store:                533,832 rows / 6,079 MB   (热表)
decision_audit_records:     185,283 rows / 312 MB
strategy_sleeve_intents:     86,437 rows / 272 MB
fill_events:                     25 rows / 408 kB     (2026-04-17 孤儿)
fill_outcomes / order_states: 0 rows  (baseline_only 正常)

# Postgres event_store 索引
ix_event_store_topic_scope_seq:   60 MB   (主要索引)
ix_event_store_topic_symbol_seq:  58 MB
ix_event_store_event_id:          39 MB

# NATS JetStream
AATS_EVENTS:         msgs=66,674  (66 MB, 13h 历史)
AATS_EVENTS_MARKET:  msgs=839,796 (2,048 MB, 7h 历史)   ← 堵在这
全局存储配额:        6.44 GB / 8 GB (80%)

# NATS 消费者 backlog (仅 >0 的)
aats-decision-features_snapshots:    pending=142,441, ack_pending=256, redelivered=8,580
aats-decision-market_snapshots:      ack_pending=143
aats-execution-features_snapshots:   ack_pending=178
aats-execution-market_snapshots:     ack_pending=178
```

---

## 优先级建议（给主任务）

| Prio | 任务 | 预估工作量 |
|---|---|---|
| **P0** | Issue 2+3 NATS features_snapshots 消费堵塞 — 改 max_ack_pending / ack_wait / DeliverPolicy + 运维一次清 backlog | 0.5 day |
| P1 | Issue 5a Grafana Postgres datasource db 改到 aats_research | 5 分钟 |
| P1 | Issue 5b+c 补 collector 按表拆分 counter + silver ETL metrics 直连 | 1 day |
| P1 | Issue 1 `_SINGLEFLIGHT_WAIT_SECONDS` 调整 + account_service 启动预热 | 2 hour |
| P2 | Issue 4 清 2026-04-17 孤儿 fill + classifier 加 historic_orphan 规则 | 2 hour（+ 备份流程） |
