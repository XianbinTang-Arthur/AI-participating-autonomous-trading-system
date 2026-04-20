# Decision features_snapshots handler 快慢路径解耦 SOW

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。

> **文档状态**：待审批（2026-04-20 起草 + 2026-04-20 源码证据修订）
> **上游调查**：主会话 JSONL + background agent `a1ddd68cc4f85b22f` 完整报告，§7 风险和 §9 follow-up 已用源码证据校准（非假设）
> **工期估计**：1 天（S1+S2+S3）
> **关联**：与 [`gateway_slow_query_systematic_fix_sow.md`](gateway_slow_query_systematic_fix_sow.md) 独立（gateway 慢查询是 operator_api 层），但两者通过 NATS server 负载产生二阶耦合——本 SOW 落地后 gateway 的 `nats: timeout` 类毛刺也会间接缓解

---

## 1. 背景 & 目标

### 1.1 为什么治

NATS stream `AATS_EVENTS_MARKET` 的 `aats-decision-features_snapshots` consumer **永远追不上** publish：

| 指标 | 实测 |
|---|---|
| publish 速率（market 发） | **30.5 msg/s** |
| consume 速率（decision 吃） | **17.1 msg/s**（有锁争用时甚至 **0 msg/s**） |
| 差距 | 13.5 msg/s 永不收敛 |
| 积压发散速度 | 69K 条用 ~80 分钟就能吃满 2GB stream |

Consumer recreate 只能**重置计数**，每次重启后积压重新累积——根因在 handler 代码。

### 1.2 根因（从 agent 调查报告）

[`aats/services/decision_engine/trigger.py:37-94`](../../aats/services/decision_engine/trigger.py) `handle_feature_snapshot` 设计缺陷：

```python
async def handle_feature_snapshot(self, message: dict) -> None:
    ...
    for timeframe in self.policy.enabled_timeframes():
        lock = self._timeframe_locks.setdefault(..., asyncio.Lock())
        async with lock:                          # ← (1) 每条 msg 都拿锁
            ...
            if not should_trigger or ...:
                continue
            await self.orchestrator.run_cycle(     # ← (2) 整个决策周期在 handler 里跑
                symbol=snapshot.symbol, timeframe=timeframe,
                feature_snapshot_hint=feature_envelope,
            )
```

**问题链**：
1. `run_cycle` 正常 0.7–1.1s，毛刺到 **22s**（实测 2026-04-20 21:43 有一次）。
2. 每条 feature_snapshot 消息都走 `async with lock`，命中 `should_trigger=True`（每 ~60s 一次，`decision_min_interval_seconds_15m=60.0`）时锁占 22s。
3. 那 22s 内，NATS 推给 client 的 32 条 in-flight（`max_ack_pending=32`）handler 全部堵在 `async with lock:` 排队——即使它们 `should_trigger` 会返回 False。
4. nats-py push cb 对同一 subscription **串行 await**，max_ack_pending 不等于 handler 并发度。handler 单协程就是瓶颈。
5. `run_cycle` 里要 `await publish_model(...)` **8 次**，期间撞上 NATS server 高负载 → `nats: timeout`，反哺 run_cycle 变慢 → 自循环放大。
6. decision event loop 被同步 I/O（`asyncio.to_thread` 里的 PG 查询、NATS publish await）冲击 → 其他 observer subscription（`strategy.sleeve_intents` / `strategy.portfolio_allocation_decisions`）也超时 → 日志里 `noncritical_subscription_failed error=nats: timeout` 刷屏。

**本质**：handler 是 NATS 订阅循环的一部分，却同步跑 20s 级的业务逻辑，成为 **head-of-line blocker**。

### 1.3 目标（可测）

| 指标 | 当前 | 完成后 |
|---|---|---|
| `aats-decision-features_snapshots` consume 速率 | 0–17 msg/s | **>30 msg/s**（跑赢 publish） |
| 稳态 pending | 永不收敛 | **持续 <100** |
| handler 单条耗时 P95 | 22s（毛刺） | **<10 ms**（快路径） |
| NATS stream roll-over 频率 | 每 80 分钟满一次 2GB | **不再 roll**（consumer 追平） |
| `noncritical_subscription_failed error=nats: timeout` 日志 | 每分钟多条 | **清零或偶发** |
| `decision_cycle_failed consecutive_failures` | 日常出现 | **消失** |
| decision 内部 run_cycle 实际跑的频率 | 不变（15m timeframe 决定） | **不变** |

**语义不变**：决策策略本身不变，`max_decisions_per_minute=6` 和 `decision_min_interval_seconds_15m=60.0` 不动。只改"handler 是否在订阅循环里 await run_cycle"。

---

## 2. 总体策略

按"改动最小 + 收益最大 + 可独立回滚"分 4 个 Stage：

| Stage | 内容 | 工时 | 独立收益 | 依赖 |
|---|---|---|---|---|
| **S1** | 引入 `asyncio.Queue(maxsize=1)` + dispatcher task 骨架；handler 双路并存（flag 控制，默认走旧路径） | 0.5 天 | 无功能变化，但 infra 就位 | 无 |
| **S2** | flag 切到新路径（handler enqueue + dispatcher 跑 run_cycle）；生产观察 | 0.25 天 | 根治 handler blocker | S1 |
| **S3** | 移除旧代码路径 + 删 `_timeframe_locks`；清理 flag | 0.25 天 | 代码简洁 | S2 稳定 24h |

**Stage 顺序严格**：S1→S2→S3。

**S4（orchestrator publish gather）不在本 SOW 范围**，见 §9 第 5 条。审 `aats/services/decision_engine/orchestrator.py:108-300` 后发现 agent 原方案的"1s→300ms"收益被高估：`health_envelope.event_id`（line 132 被 `context_builder.build` 引用）、`strategy_envelope.event_id`（line 234 被 `apply_selected_target` 引用）、`position_target_envelope.event_id`（line 285 被 `overlay_parent_exposure_record` 引用）这三个 publish 的返回值是下游 payload 的数据依赖，**不能 gather**。真正可 gather 的是段间 F&F 小组（DECISION_CONTEXTS+BASELINE_ASSESSMENTS / STRATEGY_SLEEVE_INTENTS+PORTFOLIO_ALLOCATION / AI_DECISION_BRIEFS+AI_ASSESSMENTS），实际省 40-60 ms/cycle，非数量级改善，收益不值得本 SOW 范围内的额外测试复杂度。

---

## 3. 详细设计

### S1 — 引入 Queue + Dispatcher 骨架（handler 不切换）

#### 3.S1.1 新增字段

`aats/services/decision_engine/trigger.py` `DecisionCycleTrigger.__init__`：

```python
# S1 新增：命中 should_trigger=True 的 feature envelope 通过这个 queue
# 交给后台 dispatcher task 跑 run_cycle，handler 本身不再 await run_cycle。
#
# maxsize=1：latest-wins。如果 dispatcher 还在处理上一个，新的命中直接
# 覆盖 queue 里的 pending item——符合决策业务语义（策略本来就是用
# 最新 feature snapshot 做决策，丢中间历史不影响）。
self._trigger_queue: asyncio.Queue[_PendingTrigger] | None = None
self._dispatcher_task: asyncio.Task[None] | None = None
self._dispatcher_shutdown = asyncio.Event()
# 功能 flag：S2 切到 new=True，S3 移除此 flag
self._use_queue_dispatcher: bool = False
```

`_PendingTrigger` 新增 dataclass（顶层 module scope）：

```python
@dataclasses.dataclass(frozen=True)
class _PendingTrigger:
    """单次 run_cycle 触发信号。"""
    feature_envelope: EventEnvelope
    snapshot: FeatureSnapshot
    timeframe: str
    market_snapshot: MarketSnapshot
```

#### 3.S1.2 启动 / 停止 dispatcher

加两个方法：

```python
async def start(self) -> None:
    """在 bootstrap 订阅前调用，初始化 queue + dispatcher task。"""
    if self._dispatcher_task is not None:
        return
    self._trigger_queue = asyncio.Queue(maxsize=1)
    self._dispatcher_shutdown.clear()
    self._dispatcher_task = asyncio.create_task(
        self._dispatcher_loop(),
        name="features_snapshot_dispatcher",
    )

async def stop(self) -> None:
    """关闭时 drain + cancel dispatcher。"""
    self._dispatcher_shutdown.set()
    if self._dispatcher_task is not None:
        self._dispatcher_task.cancel()
        try:
            await self._dispatcher_task
        except asyncio.CancelledError:
            pass
        self._dispatcher_task = None
    self._trigger_queue = None
```

在 `aats/bootstrap/config.py` 订阅注册处（line 3127 附近）挂入：

```python
if decision_trigger is not None:
    await decision_trigger.start()
    await bus.subscribe(topics.FEATURE_SNAPSHOTS, decision_trigger.handle_feature_snapshot)
```

shutdown hook（`aats/bootstrap/process_lifecycle.py`）里找到同名角色 teardown 处加 `await decision_trigger.stop()`（具体位置 S1 实施时查明）。

#### 3.S1.3 Dispatcher loop

```python
async def _dispatcher_loop(self) -> None:
    """后台 task：从 queue 消费 trigger 跑 run_cycle。单协程串行，
    相当于把原 handler 里的 ``async with lock`` 移到这里，但**不再阻塞
    NATS 订阅循环**。
    """
    assert self._trigger_queue is not None
    while not self._dispatcher_shutdown.is_set():
        try:
            pending = await self._trigger_queue.get()
        except asyncio.CancelledError:
            return
        try:
            await self._run_cycle_with_backoff(pending)
        except Exception as exc:  # noqa: BLE001
            # 和原 handler 的 error handling 等价：记日志 + 计 consecutive_failures。
            # 不抛出——dispatcher 要一直活着。
            fail_key = (pending.snapshot.symbol, pending.timeframe)
            n = self._consecutive_failures.get(fail_key, 0) + 1
            self._consecutive_failures[fail_key] = n
            backoff = min(self._BACKOFF_INITIAL_S * n, self._BACKOFF_MAX_S)
            log_event(
                self.logger,
                "decision_cycle_failed",
                level="warning" if n > 1 else "error",
                symbol=pending.snapshot.symbol,
                timeframe=pending.timeframe,
                error_type=type(exc).__name__,
                error=str(exc),
                consecutive_failures=n,
                backoff_s=backoff,
            )
            await asyncio.sleep(backoff)
        finally:
            self._trigger_queue.task_done()

async def _run_cycle_with_backoff(self, pending: _PendingTrigger) -> None:
    """把 orchestrator.run_cycle 的 happy path 搬过来。失败抛到 dispatcher_loop
    处理 backoff。"""
    await self.orchestrator.run_cycle(
        symbol=pending.snapshot.symbol,
        timeframe=pending.timeframe,
        feature_snapshot_hint=pending.feature_envelope,
    )
    # 成功后重置退避计数
    fail_key = (pending.snapshot.symbol, pending.timeframe)
    self._consecutive_failures.pop(fail_key, None)
    # record_trigger 也搬过来
    self.policy.record_trigger(
        feature_snapshot=pending.snapshot,
        market_snapshot=pending.market_snapshot,
        timeframe=pending.timeframe,
    )
```

#### 3.S1.4 Enqueue helper（latest-wins drop-old）

```python
async def _enqueue_trigger(self, pending: _PendingTrigger) -> None:
    """覆盖式入队：如果 queue 满（意味着 dispatcher 还在跑上一个），
    先 drain 掉旧的 pending，再 put 当前最新的。这是 latest-wins 语义——
    决策本来就是用最新 feature snapshot 做，中间历史丢了正确。
    """
    assert self._trigger_queue is not None
    # 非阻塞 drain pending（maxsize=1 最多一条）
    try:
        _stale = self._trigger_queue.get_nowait()
        self._trigger_queue.task_done()
    except asyncio.QueueEmpty:
        pass
    # put_nowait 保险（queue 此时必为空）
    try:
        self._trigger_queue.put_nowait(pending)
    except asyncio.QueueFull:
        # 极罕见竞态（两个 handler 同时 drain 后 put）——直接丢最新的，
        # 因为 queue 里已有同等或更新的 trigger，dispatcher 自会处理。
        log_event(
            self.logger,
            "features_snapshot_trigger_dropped_race",
            level="debug",
            symbol=pending.snapshot.symbol,
            timeframe=pending.timeframe,
        )
```

#### 3.S1.5 Handler 保持旧路径（S1 不切）

```python
async def handle_feature_snapshot(self, message: dict) -> None:
    if self._use_queue_dispatcher:
        await self._handle_feature_snapshot_via_queue(message)
    else:
        await self._handle_feature_snapshot_legacy(message)  # 原实现
```

`_handle_feature_snapshot_legacy` 就是目前 line 37-94 的内容原样搬过去改名。

`_handle_feature_snapshot_via_queue` S1 先写骨架，S2 切换后启用。

#### 3.S1.6 单元测试

`tests/unit/test_decision_trigger_queue_dispatcher.py` 新增：

- **test_enqueue_latest_wins_drops_old**：connect 手动 put 一个旧 trigger → 再 put 新的 → 确认 queue 里是新的
- **test_dispatcher_runs_cycle_serially**：mock orchestrator，enqueue 3 个 → 验证 run_cycle 被串行调 3 次（顺序或中间替换都接受）
- **test_dispatcher_shutdown_drains_queue_and_returns**：start → enqueue → stop → 验证 cancel 不抛、task 退出
- **test_dispatcher_exception_does_not_kill_loop**：mock orchestrator 抛异常 → 确认 dispatcher 继续活着处理下一个 enqueue
- **test_start_is_idempotent**：两次 start 只起一个 task
- **test_legacy_path_still_works_when_flag_false**（保底）：`_use_queue_dispatcher=False` 时 handler 调 legacy（mock spy 确认）

---

### S2 — 切换到新路径

改 **一行**：

```python
self._use_queue_dispatcher: bool = True   # S1 的 False 改成 True
```

同时实现 `_handle_feature_snapshot_via_queue`：

```python
async def _handle_feature_snapshot_via_queue(self, message: dict) -> None:
    """快路径：parse + should_trigger 判断，命中就 enqueue，立即返回让 NATS ack。
    不再在 handler 里 await run_cycle，也不再 `async with lock`。
    """
    feature_envelope = parse_envelope(message)
    snapshot = FeatureSnapshot.model_validate(feature_envelope.payload)
    if self.can_trigger is not None:
        allowed, _reason = self.can_trigger(symbol=snapshot.symbol)
        if not allowed:
            return
    for timeframe in self.policy.enabled_timeframes():
        # 不拿 asyncio.Lock——dispatcher 单协程天然串行
        current_market_snapshot = self.market_gateway.latest_snapshot(snapshot.symbol)
        should_trigger, _reason = self.policy.should_trigger(
            feature_snapshot=snapshot,
            market_snapshot=current_market_snapshot,
            timeframe=timeframe,
        )
        if not should_trigger or current_market_snapshot is None:
            continue
        await self._enqueue_trigger(_PendingTrigger(
            feature_envelope=feature_envelope,
            snapshot=snapshot,
            timeframe=timeframe,
            market_snapshot=current_market_snapshot,
        ))
```

**单元测试新增**：

- **test_handler_via_queue_fast_returns**：mock orchestrator.run_cycle(sleep 5s) + enqueue 10 条 → 验证 handler 调用总耗时 < 100ms（不等 run_cycle）
- **test_handler_via_queue_triggers_cycle_eventually**：mock orchestrator + mock policy(should_trigger=True once) → enqueue → 等到 dispatcher 跑完 → 断言 run_cycle 被调 1 次
- **test_handler_via_queue_dedup_latest_wins**：mock policy 一直返回 True + orchestrator 装慢（sleep 500ms）→ handler 连续 5 次 → 确认 run_cycle 只跑 2-3 次（不是 5 次，因为中间被 latest-wins drop）

---

### S3 — 清理代码

- 删除 `_handle_feature_snapshot_legacy`
- 删除 `self._use_queue_dispatcher` flag
- 删除 `self._timeframe_locks`（dispatcher 单协程串行，不需要锁）
- `handle_feature_snapshot` 直接实现为 S2 的 `_handle_feature_snapshot_via_queue`
- 相关单元测试清理（legacy 路径的断言删掉）

**前置条件**：S2 落地后**生产观察 24 小时**，pending 持续 <100、无新 `decision_cycle_failed`、无 NATS backlog roll-over，才 merge S3。

---

---

## 4. 下游审查清单（S2 必读）

S2 切换会改变 `handle_feature_snapshot` 的语义：**同步返回时 run_cycle 尚未跑**。如果有代码假设 "handler 返回时 run_cycle 已完成"，会坏掉。

必检点：

```bash
rg -n 'handle_feature_snapshot|decision_trigger' aats/ tests/
```

对每个命中：
- 调用方是否 `await` 后立即读 run_cycle 的副作用（比如立刻查 event_store 拿最新 decision_context）？若是，**坏**。
- 测试夹具（fixture）是否 `await handle_feature_snapshot(...)` 后断言 run_cycle 已执行？若是，改为等 `dispatcher_task` + `trigger_queue.join()`。

---

## 5. 验证计划

### 5.1 每个 Stage 合入后的硬性验证

#### S1
- `pytest tests/unit/test_decision_trigger_queue_dispatcher.py` 全绿
- 部署后 `docker logs aats-decision | grep features_snapshot_dispatcher`：dispatcher task 起了
- `flag=False` 下 handler 行为不变（consume rate 和之前一样 17 msg/s；但起码不回归）

#### S2
- 部署后 NATS consumer `aats-decision-features_snapshots`：
  - `delivered.consumer_seq - ack_floor.consumer_seq` 持续 ≤ 3（不是 32）
  - `num_pending` 在 30 秒内降到 <100
  - 实测 publish vs consume 速率 **差 ≤ 0 msg/s**
- `docker logs aats-decision | grep decision_cycle_completed` 频率不变（还是 ~60s/次）——证明决策行为不变
- `docker logs aats-decision | grep nats: timeout` 清零或偶发（目标 <1/10min）

#### S3
- 代码行数减少
- 单元测试全绿
- 生产观察 24h 指标不回归

#### S4（若做）
- run_cycle 耗时从 P95 1s 降到 P95 <400ms
- 毛刺从 22s 降到 <5s

### 5.2 整体目标验证（§1.3 表逐项对照）

24h 稳定样本后对照。任何指标未达标开 follow-up issue。

---

## 6. 提交顺序

```
S1 → merge → deploy → 验证 infra 就位 → 
S2 → merge → deploy → 观察 24h (指标达标) → 
S3 → merge → deploy → 
S4 → 独立可以随时做
```

**严格**：每 Stage 独立 commit、独立 revert。

---

## 7. 风险矩阵

| 风险 | 概率 | 影响 | 缓解 / 源码证据 |
|---|---|---|---|
| S1 dispatcher task 崩溃不起，features_snapshots 消费停滞 | 低 | 高 | flag=False 时 task 崩与否不影响；S2 切过来前有 test 覆盖 |
| S2 latest-wins drop 丢失某条 "关键" feature snapshot | **已源码证伪** | — | 3 条源码证据：(1) `trigger_policy.py:36-89` `should_trigger` 的 7 种分支 + `trigger_policy.py:91-108` `record_trigger` 只在 run_cycle 完成后才 call — 所以 run_cycle 未完成时新来的 snapshot 自然也会 `should_trigger=True`，queue latest-wins 覆盖的是 "同一轮等待中的中间 snapshot"，**不是漏掉一个决策窗口**。(2) 下游 `context_builder.py:131` 和 `baseline.py:65` 读的是 `event_store.latest(FEATURE_SNAPSHOTS, key=symbol)` 而不是流式消费，丢中间 NATS transient 不影响 run_cycle 读到最新 feature。(3) `context_builder.py:128-136` 的 `feature_snapshot_hint` 优先于 `latest()` fallback —— queue 里的每个 trigger 自带 `feature_envelope`，dispatcher 跑哪个 trigger 就 hint 哪个 envelope，**R3-P1-U-A 的 `feature_snapshot_ref` 不漂移约束（`trigger.py:38` 注释）仍然成立** |
| S2 切换后 dispatcher_task 被卡死 (run_cycle 死锁)，新 trigger 堆在 queue | 中 | 中 | queue maxsize=1 + latest-wins，不会无限涨；dispatcher 加 exception 日志；若 stuck 5 分钟则报警 (SEV2 新加一条 rule) |
| S3 移 legacy 代码后发现 bug 无 fallback | 低 | 中 | S2→S3 间隔至少 24h 稳定观察；S3 保留 git tag 方便 revert |
| shutdown race：decision restart 时 queue 里有 pending，dispatcher cancel 丢 1 条 | **非风险** | — | `process_lifecycle.py:274-287` 的 `finally: await runtime.stop_background_tasks()` 是保证路径，`config.py:778 stop_background_tasks` 是 teardown 统一挂载点。S1 实施时把 `DecisionCycleTrigger.stop()` 挂到 `runtime.stop_background_tasks` 集合，drain 语义由该路径保证。即使发生，feature_snapshots 每秒 30 条，下一条触发距离 ms 级 |

---

## 8. 回滚预案

### 8.1 S1 单 Stage 回滚

```bash
git revert <S1-sha>
bash scripts/deploy.sh --skip-commit
```

### 8.2 S2 紧急回滚（生产发现问题）

不需要 revert commit，直接改 `_use_queue_dispatcher = False` 一行热更：

```bash
# Windows 侧快速 patch
# 在代码里改 True → False，不用过 review/SOW
git commit -am "hotfix: rollback S2 dispatcher flag"
bash scripts/deploy.sh --skip-commit
```

### 8.3 S3 回滚

S3 删了 legacy 代码，revert 把它拿回来；或者 `git revert` S3 commit 恢复双路。

---

## 9. 本 SOW 不覆盖

| # | 主题 | 原因 |
|---|---|---|
| 1 | market 侧 calculator.py 降 publish 频率（方案 C） | 本 SOW 是"改 consumer"，不是"降 publisher"。若 S1-S3 落地后 publish 本身需要节流，另起 SOW |
| 2 | 其他 topic 的 handler 设计审查（是否也有 `await run_cycle` 这种 head-of-line blocker） | 本 SOW 只修 features_snapshot；全进程 handler 审查另起 |
| 3 | NATS stream `AATS_EVENTS_MARKET` 的 `max_bytes=2GB` 调优（扩容 / 分流）| 与 handler 吞吐独立。本 SOW 完成后 stream 不再 roll，那时再评估是否扩容 |
| 4 | `decision_cycle_failed consecutive_failures` 的告警阈值优化 | 本 SOW 完成后该指标自然降，告警不用改 |
| 5 | `orchestrator.py:108-300` 的 publish gather 化（原 S4） | 已源码审查排除。orchestrator 的 11 个 `publish_model` 里 3 个返回 envelope 被下游 payload 引用（`health_envelope.event_id` → context build 第 132 行、`strategy_envelope.event_id` → apply_selected_target 第 234 行、`position_target_envelope.event_id` → overlay 第 285 行），这 3 个**不能 gather**。剩余 8 个 F&F publish 分散在 3 个小段（DECISION_CONTEXTS+BASELINE_ASSESSMENTS / STRATEGY_SLEEVE_INTENTS+PORTFOLIO_ALLOCATION / AI_DECISION_BRIEFS+AI_ASSESSMENTS+AI_SHADOW_DECISIONS+OVERLAY_PARENT_EXPOSURES），gather 实际节省 40-60 ms/cycle，非数量级改善。本 SOW 完成后 publish 路径的 NATS timeout 副作用会随 NATS server 负载降低而消失，不需要再动 orchestrator |

---

## 10. 签收条件

- [ ] S1+S2+S3 全部合入 main
- [ ] §1.3 目标表 7 项全部达标（24h 稳定样本）
- [ ] 新增单元测试 ≥ 9 份（S1: 6 条，S2: 3 条）全绿
- [ ] Loki 日志查 `noncritical_subscription_failed` 24h 内清零或偶发 (<1/10min)
- [ ] `parallel_fetch_slow` 日志的 `nats: timeout` 副作用也消失（间接收益）

---

## 11. 审批记录

| 角色 | 姓名 | 日期 | 意见 |
|---|---|---|---|
| 起草 | Claude | 2026-04-20 | 初稿，基于 agent `a1ddd68cc4f85b22f` 调查 |
| 审核 | @excellentang | — | — |
| 实施 | — | — | — |
