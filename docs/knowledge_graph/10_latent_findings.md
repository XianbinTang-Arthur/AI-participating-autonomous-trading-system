# 10 · Latent Findings（图谱整理过程中发现的可疑模式）

> **生成于 HEAD=0ef6f1c** · 2026-04-21 autonomous session
> **状态**：live 更新，边梳理代码边加
> **原则**：**只记录，不自动修复**。所有 fix 需要用户或下一轮 code review 过关。

## 格式约定

- **ID**: `LF-20260421-NNN`（可引用）
- **Severity**: 🔴 HIGH / 🟡 MED / 🟢 LOW
- **Category**: bug / smell / doc-drift / 性能 / 安全
- **位置**: file:line
- **修复建议**: 如何修（不执行）

---

## 🔴 HIGH — 安全或正确性相关

### LF-20260421-001 · ~~心跳文件 health check 看不到 GIL 卡死~~ (CORRECTED 2026-04-22)

- **~~Category~~**: ~~监控盲点~~ **→ 部分假阳 + 改合并到 LF-020**
- **实际情况**：`aats/bootstrap/process_lifecycle.py:247-309` `_heartbeat_loop`
  是 **async 函数跑在 event loop 上**，不是后台线程。如果 event loop 卡住，
  heartbeat 也会卡住，mtime 就不更新 → docker healthcheck 会捕获。
- **真正的 gap**：不是 heartbeat 不可靠，而是**没有"业务活着"指标**（e.g.
  `decision_cycle_total` rate），区分 "event loop 在跑但策略没决策" vs
  "真没市场数据"。这就是 LF-020 的问题，二者合并处理。
- **教训**：审计时先 trace 实际代码路径再下结论。我的 audit agent 凭名字
  猜"heartbeat 是后台线程"是错的。

### LF-20260421-002 · OrderState 更新存在 WS vs REST 竞争

- **Category**: 正确性 race condition
- **位置**: `aats/services/execution_engine/order_manager.py` + `okx_private_websocket.py`
- **现象**：`OrderManager.sync_exchange_state()` 定期从 OKX REST 刷状态；同时
  私有 WS 推实时 fill。两路都会更新 OrderState，但代码没明显的 version
  或 lock 保护
- **想象的坏场景**：
  1. WS 推 fill qty=0.5 → status=PARTIALLY_FILLED
  2. REST 同时 refetch，看到 qty=0.3（旧快照）→ 覆盖成 SUBMITTED
- **未确认是否真的发生**：需要对齐验证。可能已有 optimistic locking 但我没找到
- **建议**：加显式 version / last_update_ts 比较，证明 "WS is source of truth, REST only backfills missing fills"

### LF-20260421-003 · `run_cycle` 无全局 timeout，NATS 背压时可能卡死整个 decision 进程

- **Category**: 可用性
- **位置**: `aats/services/decision_engine/orchestrator.py::run_cycle`
- **现象**：`run_cycle` 内部有多个 `publish_model` 调用。如果 NATS 满载或
  主题 backpressure（比如 stream_cache 满、JetStream 同步写慢），publish
  会阻塞，没有外层 asyncio.wait_for 保护
- **后果**：单次 run_cycle 可以挂住几十秒甚至更久；trigger.py 的
  `_timeframe_locks` 锁在该 (symbol, timeframe) 上，完全串行
- **建议**：`run_cycle` 整体包 `asyncio.wait_for(timeout=30s)`，超时 raise
  → trigger.py 的 backoff 机制接管

### LF-20260421-004 · Reconciliation → KillSwitch 传播有 10-50ms 缝

- **Category**: 安全竞争窗口
- **位置**: `aats/services/reconciliation_service/` → `RecoveryPostureEvaluator` → `KillSwitch.halt()`
- **现象**：检测到 mismatch 后的链路是：
  1. Reconciliation 产出 Report
  2. 发 NATS RECONCILIATION_REPORTS
  3. Recovery 订阅消费
  4. 调 `kill_switch.halt()`（本地状态立即更新）
  5. 广播 KILL_SWITCH_STATE
- **窗口**：步骤 1-4 之间大约 10-50ms，如果 execution 在这期间接受了新
  的 PositionTarget 并 `submit_order` 下单，它可能在 halt 生效前溜出去
- **建议**：`handle_position_target` 的入口处先**同步检查** `KillSwitch.halted`
  本地状态；Recovery 在 halt 前可以先发 "pending_halt" 信号让 execution 暂停

### LF-20260421-005 · Kill Switch 不验证跨进程都收到 halt

- **Category**: 安全一致性
- **位置**: `aats/services/governance_engine/kill_switch.py`
- **现象**：`kill_switch.halt()` 发完 KILL_SWITCH_STATE 就返回，不等 ack
- **场景**：如果某进程（比如 gateway）在 halt 时刚好 NATS 订阅断连，它不
  会收到事件。NATS JetStream 的持久消息在重连后会补发，但如果在"断连 +
  重连"间隙里有人触发了 operator action，后果是 gateway 以为没 halt
- **现有缓解**：Redis bootstrap 路径会重新拉取 KillSwitch 状态（`kill_switch.py:223-245`）
- **建议**：加 version epoch + 重连后 ensure reconcile

---

## 🟡 MED — 值得改但不紧急

### LF-20260421-006 · ~~`DECISION_OUTCOMES` topic declared but never published~~ (CORRECTED 2026-04-22)

- **~~Category~~**: ~~doc-drift~~ **→ false positive (my KG audit agent was wrong)**
- **实际情况**：`DECISION_OUTCOMES` 有真实 publisher —— `_publish_finalized_decision_outcome()`
  在 `aats/bootstrap/config.py:1866-1872` 发布，被 5 处调用（L2974/2986/2997/3005/3014）。
  Audit agent 只查了 `orchestrator.py` 漏了 `bootstrap/config.py`。
- **为什么会误判**：`_publish_finalized_decision_outcome` 是 `_build_position_target_handler`
  内部的 closure，从 `bootstrap/config.py` 的 `build_runtime` 构造而非 orchestrator。
  我在 [02_data_flow.md](02_data_flow.md) 把它标成"未发布"也是错的，已在本次一并修正。
- **教训**：grep 某 topic 时要搜**全仓**，不只核心 service 文件。Audit agent 以后
  需要 prompt 强调 "closure inside bootstrap/config.py 也是 production code"。

### LF-20260421-007 · `_timeframe_locks` / `_consecutive_failures` dict 无清理

- **Category**: 长期内存增长
- **位置**: `aats/services/decision_engine/trigger.py:261, 283-284`
- **现象**：按 (symbol, timeframe) key 累加 entries，从不删除。`_consecutive_failures`
  仅在成功时 pop。如果某 symbol 被下架，entry 永远留
- **规模**：当前 1 symbol，5 entries，零影响
- **建议**：扩 symbol 前用 LRU 或 subscribe 到 allowed_symbols 变化事件清理

### LF-20260421-008 · 后台 loop 缺 jitter（A2 已部分修复 `9e9c0bc`）

- **Category**: 已处理，记录为历史
- **状态**：本次 session 里 A2 commit `9e9c0bc` 给 8 个关键 loop 加了 jitter
- **遗留**：`_housekeeping_loop`（6h）和 `_flush_execution_outbox_loop`（exp
  backoff）没加，原因见 commit message

### LF-20260421-009 · `_pending_evictions` bounded queue（A3 已修复 `222d7ba`）

- **Category**: 已处理，记录为历史
- **状态**：本次 session A3 commit 把 list 换成 `deque(maxlen=500)`

### LF-20260421-010 · Feature snapshot 无 TTL

- **Category**: 陈旧数据风险
- **位置**: `aats/services/feature_engine/` → 发布 FeatureSnapshot，消费方
  通过 MARKET_SNAPSHOTS 触发
- **现象**：如果 market 进程重启，旧的 FeatureSnapshot 还在 decision 进程
  的 trigger 逻辑缓存里。如果市场恰好冷启动期没有新 tick，decision 可能
  用几分钟前的 feature
- **建议**：加显式 cached_at，decision 读取时检测过期

### LF-20260421-011 · `max_gross/pending/total_open_notional = 0` 被当作"禁用检查"

- **Category**: 配置 UX（已锚定 C2 anchor test `d6e6694`）
- **状态**：test 已写死当前语义；实际 fix 留给未来（Pydantic Field(gt=0)）

### LF-20260421-012 · Provider.snapshot() 返回 None 的 permissive fallback

- **Category**: 设计意图（已锚定 C2 anchor test `d477bf4`）
- **状态**：test 已锁死 contract —— provider 可选、真正的 fail-closed 在
  provider 内部 sentinel 层

### LF-20260421-013 · GuardSignalCache bootstrap 失败路径已有测试（`6b0cbaf`）

- **状态**：本次 session 已补

### LF-20260421-014 · Market WS 无 circuit breaker / REST fallback

- **Category**: 可用性
- **位置**: `aats/services/market_gateway/gateway.py`
- **现象**：如果 OKX 公开 WS 断连并且连不上，没有 fallback 到 REST polling。
  market 进程只会一直重试 WS。decision 收不到新 snapshot
- **建议**：加 "WS 断连 >60s → 临时切 REST polling（低频）→ WS 恢复后切回" 机制

### LF-20260421-015 · Operator command proxy 假定 execution 一直在 (DEFERRED 2026-04-22)

- **Category**: 可用性
- **位置**: `aats/services/operator/command_bridge.py` `OperatorCommandClient.invoke`
- **现状**：超时 30s 会 raise OperatorCommandTimeoutError，gateway 返回 500。
  用户等 30s 才看到失败 —— 慢但不致命。
- **为什么不修**：真正的 fix 需要 UI 显示 execution 实时状态 + 预检，属于
  Phase 3 UI 层工作。当前规模（1 用户 / 不频繁触发 operator command）优先级低。
  "execution 可达性预检" 本身又依赖 NATS（同一条有问题的通道），逻辑打结。
- **对当前 ops 的影响**：gateway UI 等 30s 后报错，可接受；真关心的话加
  Grafana alert 监控 `OperatorCommandTimeoutError` 日志出现率（docs/operations/
  grafana_alerts.md 可扩）。

---

## 🟢 LOW — 细节或 future-proofing

### LF-20260421-016 · trigger.py docstring drift（line 26）

- **Category**: doc-drift
- **注释**：说 "初始化 queue + 起 dispatcher task"，但实际 `dispatcher_task` 是 `DecisionCycleTrigger.initialize()` 管理，`stop()` 时取消
- **修**：更新 docstring

### LF-20260421-017 · AATS_PROCESS_ROLE typo 静默降级

- **Category**: smell
- **位置**: `apps/api_gateway/main.py:46-52`
- **现象**：未知的 `AATS_PROCESS_ROLE` 值静默当作 "gateway"
- **风险**：运维打错字（如 "gateways"）不会被发现
- **建议**：加 `allowed_roles = {"gateway", "market", "decision", "execution", "monolith"}`，不在内就 `raise ValueError`

### LF-20260421-018 · ~~RDP daemon 连接池与业务进程共享~~ (CORRECTED 2026-04-22)

- **~~Category~~**: ~~潜在性能影响~~ **→ 假阳，RDP 已有隔离设计**
- **实际情况**:
  - `RDP_DATABASE_URL` → `aats_research` 独立数据库（物理隔离）
  - `RDP_LIVE_DATABASE_URL` → `aats_live_derivatives`（只读），engine 在
    `aats/data_platform/live_facts/db.py:59-66` 配置为 pool_size=3 + max_overflow=5
    （非常小）
- **现有架构已正确**：RDP 读实盘数据的连接池上限是 **8**，4 业务进程各 60，
  PG max_connections=200 有足够 headroom
- **可能的唯一小坑**（保留记录）：`RDP_DATABASE_URL` 和 `RDP_LIVE_DATABASE_URL`
  引用不同 DB 但共享密码 —— 如果 POSTGRES_PASSWORD 变，两个 URL 都要同时
  更新。config 层不是 single source of truth。但这是**运维一次性小麻烦**，
  不是运行时 bug。

### LF-20260421-019 · 丢失触发指标

- **Category**: 可观测性
- **位置**: `aats/services/decision_engine/trigger.py::_enqueue_trigger`
- **现象**：`maxsize=1` 的 queue latest-wins dedup 会丢早来的 trigger。丢
  弃次数没 metric 记录
- **建议**：加 `decision_cycle_dropped_triggers_total` counter，长期趋势观察

### LF-20260421-020 · ~~Decision trigger idleness 无告警~~ (CORRECTED 2026-04-22)

- **~~Category~~**: ~~可观测性~~ **→ metric 已存在，只缺 alert 规则**
- **实际情况**：`orchestrator.py:342 self.metrics.increment("decision_cycles")`
  已经在发 counter。Prometheus 能抓到。缺的只是 Grafana alert rule
  (e.g. `rate(decision_cycles_total[5m]) == 0 for 10m`)。
- **性质**：Grafana 配置而非代码变更，不算 code latent bug。应放
  `docs/operations/` 或 Grafana provisioning config，不属于本 latent 清单。
- **不修**：把 grafana alert rule 写进 `docs/grafana_alerts.md` 供 ops 配置。

### LF-20260421-021 · 成本模型没扣 maker rebate

- **Category**: 经济学估算不准
- **位置**: `aats/services/strategy_engines/independent/independent_family.py:1741-1745`
- **现象**：`bounded_limit` / `passive_first` 执行模式里，fee 只考虑 "70% maker
  + 30% taker"，但**不扣 OKX 的 maker rebate**（VIP 账户可达 -0.015%）
- **影响**：生产 `expected_cost_bps` 比真实多估 ~1-2 bps，导致更多决策被判
  "期望净亏" 而 hold
- **建议**：`fee_resolver` 加 `maker_rebate_bps_decimal()` 方法，在 cost 里减掉；
  验证 OKX 账户等级对应的实际 rebate

### LF-20260421-022 · direction_bias = flat 时 confidence 对称性问题

- **Category**: 信号偏置
- **位置**: `aats/services/strategy_engines/independent/scoring.py:142-154`（H4 fix 2026-04-19）
- **现象**：`confidence` 只在 `baseline.direction_bias == leg` 时计分；否则贡献 0
- **副作用**：如果 baseline 长期 "flat"，long/short 两边 confidence 都 0（不对称）；
  如果长期 "long"，short leg 被系统性压制
- **实测**：最新 DecisionOutcome direction_bias = "flat" → 两边 confidence 都 0
- **建议**：做 24h 采样统计 `direction_bias` 分布；如果 "flat" 占 80%+，应调整
  confidence 的 gating 逻辑或引入 flat-bias 下的默认 confidence 值

### LF-20260421-023 · score gate 与 net_edge gate 阈值不联动

- **Category**: 策略几何冲突
- **位置**: `aats/services/strategy_engines/independent/engine.py:284`
- **现象**：entry 需要两道 gate 都过：
  1. `score >= entry_threshold`（硬编码 0.25）
  2. `net_edge >= safe_threshold`（0.0）
- **问题**：`signal_edge = score × 20 bps`，所以 score=0.15 对应 signal_edge=3 bps，
  扣 cost+buffer 后仍然净亏。**score < 0.30 的时候 net_edge 永远过不了**，
  所以调低 entry_threshold 到 0.15 没用（被 net_edge gate 接着挡）
- **实际的 "能 trade" 最小 score**: ~0.50（即 signal_edge > 10 bps 覆盖 cost 6 + buffer 4）
- **建议**：要么 entry_threshold 和 safe_threshold 联动设置，要么降 cost / buffer 让 net_edge gate 更宽松

---

## 追踪 / Sync 状态

| 发现编号 | 记录时间 | 是否已 fix | 修复 commit |
|---------|---------|-----------|-------------|
| 001 | 2026-04-21 KG·2 | 否 | - |
| 002 | 2026-04-21 KG·3 | 否 | - |
| 003 | 2026-04-21 KG·3 | 否 | - |
| 004 | 2026-04-21 KG·3 | 否 | - |
| 005 | 2026-04-21 KG·2 | 否 | - |
| 006 | 2026-04-21 KG·3 | 否 | - |
| 007 | 2026-04-21 KG·2 | 否 | - |
| 008 | 2026-04-21 | **是** | `9e9c0bc` |
| 009 | 2026-04-21 | **是** | `222d7ba` |
| 010 | 2026-04-21 KG·2 | 否 | - |
| 011 | 2026-04-21 | **anchor 已写** | `d6e6694` |
| 012 | 2026-04-21 | **anchor 已写** | `d477bf4` |
| 013 | 2026-04-21 | **anchor 已写** | `6b0cbaf` |
| 014 | 2026-04-21 KG·2 | 否 | - |
| 015 | 2026-04-21 KG·2 | 否 | - |
| 016 | 2026-04-21 KG·2 | 否 | - |
| 017 | 2026-04-21 KG·2 | 否 | - |
| 018 | 2026-04-21 KG·2 | 否 | - |
| 019 | 2026-04-21 KG·3 | 否 | - |
| 020 | 2026-04-21 KG·3 | 否 | - |
| 021 | 2026-04-21 Phase 2 | 否 | - |
| 022 | 2026-04-21 Phase 2 | 否 | - |
| 023 | 2026-04-21 Phase 2 | 否 | - |
