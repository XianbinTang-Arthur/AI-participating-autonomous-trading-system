# Stage 6 Slice 6.2 设计文档：kill_switch 跨进程接 hot_state_store

> 状态：**待审批**
> 前置：Slice 6.1（HotStateStore Redis backend 配线）已完成、4 进程真跑通过
> 后续：Slice 6.3（portfolio_snapshot 缓存）

---

## 1. 问题陈述：当前架构的核心 bug

### 1.1 现状

每个 AATS 进程在 `_build_shared_slice` 阶段独立构造一个 `KillSwitch` 实例：

```python
# aats/bootstrap/config.py:2870
slices.kill_switch = KillSwitch()
```

`KillSwitch` 是一个**纯内存对象**（`aats/services/governance_engine/kill_switch.py`，30 行）：

```python
class KillSwitch:
    def __init__(self) -> None:
        self._state: tuple[bool, str | None] = (False, None)

    def halt(self, reason: str = "manual_halt") -> None:
        self._state = (True, reason)

    def resume(self) -> None:
        self._state = (False, None)

    @property
    def halted(self) -> bool:
        return self._state[0]
```

### 1.2 4 进程拓扑下的失效场景

- **场景 A — operator 手动 halt 不传播**：操作员通过 gateway 的 UI 点 "Halt"，gateway role 的 FastAPI handler 调 `runtime.kill_switch.halt(...)`。**只有 gateway 进程的本地 KillSwitch 状态变为 True**；execution 进程的 `KillSwitch` 仍然是 False，下一笔订单意图仍然会被 `order_manager.py:136` 的 pre-submit 检查放行。资金面临真实风险。
- **场景 B — execution-side 自动 halt 不传播**：trial_guard / derivatives_live_guard / execution_recovery 在 execution 进程内 halt 之后，gateway dashboard 仍然显示 `halted=False`，operator 看不到崩溃。
- **场景 C — 进程重启后状态丢失**：故障演练 #2.2 杀 execution → docker 重启 → 新 execution 进程的 KillSwitch 是默认值 `(False, None)`。即便上一次有 halt，重启后也不知道。整个 4 进程拓扑的安全保证被进程崩溃 + restart 直接抹掉。

### 1.3 既有缓解（部分）

Stage 5 引入 HybridEventBus 的 critical fan-out 之后，理论上可以通过 NATS 广播 halt 事件让其他进程更新本地态。但**目前没有为 kill_switch 状态变化定义任何 NATS topic**，更没有任何订阅者更新本地 KillSwitch。Slice 6.2 之前，跨进程 kill_switch 同步是 0 ——这是一个真实的 production 安全缺陷，必须在实盘前修掉。

---

## 2. Slice 6.2 目标

让 4 进程拓扑下的 kill_switch 满足三条不变量：

1. **写入即可见**：任何进程的 halt/resume 在 ≤1s 内被其他 3 个进程的本地缓存看到（NATS 广播路径）。
2. **重启后仍可见**：进程崩溃 + restart 后，新进程从 Redis 读到上一次 halt 状态，本地缓存正确恢复（hot_state_store bootstrap 路径）。
3. **降级安全**：当 Redis 或 NATS 不可达时，**单进程内的 halt 不会失效**（local cache 始终是 hot path 的真相），只是跨进程同步退到"无同步"状态。

不在本 slice 范围：
- portfolio_snapshot 缓存（Slice 6.3）
- ai_service 状态、recovery_posture 缓存（更后的 slice）
- 取代 NATS critical fan-out（HybridEventBus 仍然是 Stage 5 的事件总线）

---

## 3. 现有调用点盘点

### 3.1 写入路径（5 处生产调用）

| # | 调用点 | 文件:行 | 调用上下文 | 后续修改方式 |
|---|---|---|---|---|
| W1 | `TrialGuard._trigger_halt` | `aats/services/governance_engine/trial_guard.py:366` | sync 函数；外层是 `await asyncio.to_thread(trial_guard_service.evaluate_now)`（worker thread） | `service.halt_threadsafe(reason)` |
| W2 | `DerivativesLiveGuard._auto_halt_if_needed` | `aats/services/governance_engine/derivatives_live_guard.py:367` | sync 函数；外层 `await asyncio.to_thread(...)` | `service.halt_threadsafe(reason)` |
| W3 | `ExecutionRecovery._raise_halt` | `aats/services/execution_engine/recovery.py:658` | sync 函数；从 startup_recovery 链路调进来 | `service.halt_threadsafe(reason)` |
| W4 | `StartupRecovery.run` | `aats/services/recovery_control/startup_recovery.py:424,433` | sync 函数；build_runtime 内 sync 调用（启动期一次性） | `service.halt_threadsafe(reason)` |
| W5a | `OperatorReconciliationSystemQueries.recovery_resume_apply` 等 5 处 | `aats/services/operator/reconciliation_system_queries.py:285,432,496,522,539` | **async** 函数（FastAPI handler 链路） | `await service.halt(reason)` / `await service.resume()` |

**测试路径上的 8 处** `kill_switch.halt()` 直调（test_runtime_controls / test_recovery / test_operator_api / test_task63_trial_guard / test_task72_derivatives_live_guard / test_recovery_posture / test_execution_recovery / test_guarded_live）**保持不变**。本地 KillSwitch 实例的 `.halt()` 仍然 sync 改本地 state，测试断言仍然通过。

### 3.2 读取路径（约 30 处）

读路径分两类：
- **快路径 sync 读**：`runtime.kill_switch.halted` / `runtime.kill_switch.status()`，主要在 hot loop 里（订单 pre-submit、health check、blocker 列表渲染）
- **业务 status JSON 拼装**：blocker_control / runtime_queries / reconciliation_system_queries / okx_adapter / order_manager / recovery 等

**Slice 6.2 的关键约束：所有读路径不动**。`kill_switch.halted` 与 `kill_switch.status()` 仍然是 sync 的、读本地缓存的，只是缓存内容由新增的 `KillSwitchSyncService` 在后台保持新鲜。

理由：
- 30 处读路径全都改成 `await` 牵动太大，且会传染到 sync 服务层
- 本地缓存的延迟在 NATS 广播 + Redis bootstrap 的双重保护下足够小（典型 < 50ms）
- 读不动意味着 Slice 6.2 的代码影响面被严格限定在"写入路径 + 新增同步服务 + 启动 bootstrap"

---

## 4. 设计

### 4.1 三层架构

```
┌────────────────────────────────────────────────────────────┐
│  KillSwitch (existing, sync)                                │
│  ─ self._state: tuple[bool, str | None]                     │
│  ─ .halt() / .resume() / .halted / .status()                │
│  ─ 所有 ~30 个 sync 读路径直接打这里（hot path，零网络）    │
└────────────────────────────────────────────────────────────┘
                            ▲
                            │ 同步本地缓存（_apply_local_state）
                            │
┌────────────────────────────────────────────────────────────┐
│  KillSwitchSyncService (new)                                │
│  ─ 拥有 KillSwitch 的引用                                   │
│  ─ 拥有 hot_state_store 引用                                │
│  ─ 拥有 EventBus 引用（用 NATS 广播 critical 路径）         │
│  ─ async halt(reason) / async resume()                      │
│  ─ halt_threadsafe(reason) / resume_threadsafe()            │
│  ─ async bootstrap()  ← 启动期 Redis 读 + 订阅广播         │
│  ─ async stop()       ← 关闭期取消订阅                      │
│  ─ async _apply_remote_event(envelope)  ← NATS 接收 handler│
└────────────────────────────────────────────────────────────┘
                            ▲
                            │ Redis / NATS
                            │
┌────────────────────────────────────────────────────────────┐
│  Source of truth                                            │
│  ─ Redis: aats:system:kill_switch  →  {halted, reason,     │
│       set_at_ts, source_role}  ← 持久化跨重启               │
│  ─ NATS: system.kill_switch_state  ←  跨进程实时广播       │
└────────────────────────────────────────────────────────────┘
```

**核心思想**：`KillSwitch` 是每个进程的"本地真相 / 快路径"；`KillSwitchSyncService` 是把 4 个本地真相收敛到同一个 Redis 状态机的"边车"。Redis 是跨进程持久化真相，NATS 是低延迟广播通道，两者共同支撑本地缓存的最终一致性。

### 4.2 写入路径详解（含 sync/async 阻抗）

#### 4.2.1 async 写入（W5 reconciliation_system_queries.py 5 处）

```python
async def halt(self, reason: str) -> None:
    """async 写入路径：本地 → Redis → NATS。

    顺序保证：
    1) 本地 cache 立即生效（safety net：即便 Redis/NATS 全挂，本进程仍然 halt）
    2) Redis SET（持久化真相）
    3) NATS PUBLISH（广播触发其他进程的 _apply_remote_event）

    失败语义：
    - 本地 cache 永不失败（步骤 1 是 sync 赋值）
    - Redis 写失败 → 记 warning，继续 NATS 广播（其他进程仍能收到）
    - NATS 写失败 → 记 warning，结束（其他进程要等到自己下次 bootstrap / 安全网刷新才看到）
    """
    set_at_ts = time.time()
    self._kill_switch.halt(reason=reason)         # 步骤 1：本地 sync
    payload = {
        "halted": True,
        "reason": reason,
        "set_at_ts": set_at_ts,
        "source_role": self._process_role,
    }
    await self._best_effort_redis_set(payload)    # 步骤 2
    await self._best_effort_nats_broadcast(payload)  # 步骤 3
```

`resume()` 对称，`halted=False, reason=None`。

#### 4.2.2 sync 写入（W1-W4，4 处 worker thread / 启动期）

worker thread 里调 `service.halt_threadsafe(reason)`：

```python
def halt_threadsafe(self, reason: str, *, timeout: float = 2.0) -> None:
    """从非 asyncio 上下文调 halt：用 run_coroutine_threadsafe 投递到主 loop。

    主 loop 引用在 bootstrap 时缓存（asyncio.get_running_loop()）。
    timeout 是给 Redis/NATS 的总预算。超时 → fall back 到只更新本地 cache。
    """
    if self._loop is None or self._loop.is_closed():
        # 测试 / 启动期早于 bootstrap：退化到 sync local-only
        self._kill_switch.halt(reason=reason)
        return
    future = asyncio.run_coroutine_threadsafe(self.halt(reason=reason), self._loop)
    try:
        future.result(timeout=timeout)
    except (concurrent.futures.TimeoutError, Exception) as exc:
        log_event(
            self._logger,
            "kill_switch_threadsafe_halt_partial",
            level="warning",
            error_type=type(exc).__name__,
            error=str(exc),
        )
        # 即便 fail，本地 cache 已经在 self.halt() 第一步就更新了
```

**为什么 timeout=2s 而不是 0**：trial_guard breach 是关键安全事件，写 Redis + NATS 广播失败时我们宁可阻塞 2s 也要尽量保证跨进程可见。如果 Redis 真挂了 2s 后仍然超时，本地已经 halt，业务仍然安全。

**为什么不抛异常**：保证 worker thread 里的 sync 调用一定不会 propagate failure 到上游 try 块——上游可能误把 halt 失败当成 "halt 没生效，继续下单"。本地 cache always wins。

### 4.3 读取路径

**完全不动**。所有 30+ 处 `kill_switch.halted` / `kill_switch.status()` 调用维持原状。

新鲜度由 `KillSwitchSyncService` 通过两条路径维持：
1. **NATS 广播**：每次任何进程 halt/resume，4 个进程都收到事件，sync handler 把本地 cache 更新。延迟 = NATS round-trip，典型 < 10ms。
2. **启动 bootstrap**：进程构造 `KillSwitchSyncService` 后立即 `await bootstrap()`，从 Redis 读最新状态注入本地 cache。这是故障演练 #2.2 重启后状态恢复的关键路径。

**故意不做**：
- 周期性 Redis poll 安全网。理由：Stage 5 已经验证 NATS critical fan-out 在故障演练里能 100% 重连，poll 是冗余的复杂度。如果实盘期发现 NATS 偶发掉包，再加 poll 兜底。

### 4.4 数据格式

**Redis key**：`aats:system:kill_switch`（用 `make_key("system", "kill_switch")`）

**Redis value**（JSON）：
```json
{
  "halted": true,
  "reason": "trial_guard_threshold_breached",
  "set_at_ts": 1712489600.123,
  "source_role": "execution"
}
```

**NATS topic**：`system.kill_switch_state`（**新增**到 `aats/events/topics.py`）
**NATS subject**：`aats.system.kill_switch_state`
**NATS payload**：`EventEnvelope`，event_type=`KillSwitchStateChanged`，payload 与 Redis value 相同的 4 字段

**新增到 `DEFAULT_CRITICAL_TOPICS`**：是。kill_switch 状态变化是 critical 路径，丢一条会让某个进程错过 halt → 资金风险。

### 4.5 时间戳排序与冲突

**问题**：如果 process A 在 t=10 halt、process B 在 t=11 resume，process C 可能先收到 B 的事件再收到 A 的事件。如果按到达顺序应用，C 的最终状态会是 halted=true（错的）。

**缓解**：每个 NATS payload 带 `set_at_ts`。`KillSwitchSyncService._apply_remote_event` 比较 `payload.set_at_ts` 与 `self._last_applied_ts`（in-memory，非持久化），只接受**更新**的事件。同一 set_at_ts 的事件去重（idempotent）。

**Redis SET 用 LWW 简单覆盖**：Slice 6.2 范围内不引入 Redis Lua 脚本做 CAS。两个进程同一秒内 SET 时后写入的覆盖前者，这与本地缓存的 LWW 一致。如果未来发现 race 频繁，再加 CAS。

**时钟偏差**：所有 4 进程都跑在同一台 docker host 上，docker container 时钟 = host 时钟，偏差 < 1ms。即便未来跨机器部署，NTP 同步偏差 < 100ms，set_at_ts 顺序仍然可信。

### 4.6 启动 bootstrap 顺序

`build_runtime` 现有顺序：
1. settings 校验
2. database_runtime 初始化
3. **hot_state_store.connect()**（Slice 6.1）
4. event_store / bus 构造
5. `_build_shared_slice` → `slices.kill_switch = KillSwitch()`
6. ...其他 slice...
7. `ApplicationRuntime(...)` 构造
8. return

Slice 6.2 在第 7 步**之前**插入：
```python
# 已经有 hot_state_store / kill_switch / bus，现在装 sync service
kill_switch_sync_service = KillSwitchSyncService(
    kill_switch=slices.kill_switch,
    hot_state_store=hot_state_store,
    bus=bus,
    process_role=effective_process_role,
    logger=get_logger("aats.governance.kill_switch_sync"),
)
await kill_switch_sync_service.bootstrap()
log_event(
    get_logger("aats.bootstrap"),
    "kill_switch_sync_service_initialized",
    bootstrap_state=kill_switch_sync_service.snapshot(),
)
```

`bootstrap()` 内部：
1. `state = await self._hot_state_store.get(KS_KEY)`
2. 如果有 state 且 `state["halted"]=True`：调 `self._kill_switch.halt(reason=state["reason"])`，`self._last_applied_ts = state["set_at_ts"]`
3. 如果有 state 且 `state["halted"]=False`：维持本地默认（已经是 False）
4. 如果 Redis 没有 key：不动本地 cache，不写 Redis（不要在 build_runtime 里把"未知"误写成"未 halt"，不然两个进程同时 cold start 时谁都不会有 halt 持久化）
5. **订阅 NATS 广播**：`await self._bus.subscribe(topics.KILL_SWITCH_STATE, self._handle_remote_event)`

`stop()` 在 `runtime.stop_background_tasks` 里调（在 `hot_state_store.close` 之前）：
1. 取消订阅（`await self._bus.unsubscribe(...)` if 支持）
2. 不写 Redis（关闭不代表 resume）

### 4.7 ApplicationRuntime 字段

```python
@dataclass
class ApplicationRuntime:
    ...existing fields...
    hot_state_store: HotStateStore  # Slice 6.1
    kill_switch: KillSwitch         # 已有
    kill_switch_sync_service: KillSwitchSyncService  # Slice 6.2 NEW
    ...
```

ApplicationRuntime 的 5 处生产 writer 改成调用 `runtime.kill_switch_sync_service.halt(...)`（async）/ `halt_threadsafe(...)`（sync）。

注意 W1-W4 的 services（trial_guard / derivatives_live_guard / execution_recovery / startup_recovery）目前持有 `self.kill_switch: KillSwitch` 而**不是** sync service。Slice 6.2 给这 4 个 service 加 `self.kill_switch_sync: KillSwitchSyncService` 字段，构造时注入。`_trigger_halt` 改成调 `self.kill_switch_sync.halt_threadsafe(reason)`。

### 4.8 monolith / 单进程降级

monolith 模式（hot_state_backend=memory，bus=InMemoryBus）下：
- `KillSwitchSyncService.bootstrap()`：从 InMemoryHotStateStore.get 拿到 None → 不动本地（与多进程冷启动相同语义）
- `service.halt()`：写 InMemoryHotStateStore + publish 到 InMemoryBus → 同进程订阅 handler 收到 → `_handle_remote_event` 跳过（因为 source_role 是自己 + set_at_ts 已应用）→ no-op
- 完全 backward-compatible，所有现有 monolith 测试零修改即可通过

测试里直接调 `kill_switch.halt(reason)`（绕过 sync service）也仍然工作：
- 本地 cache 立即更新
- 但**不会**写 Redis 或广播 NATS。这只在跨进程测试里才是问题；monolith 测试无影响。

---

## 5. 不变量

| # | 不变量 | 如何保证 |
|---|---|---|
| I1 | 任何 halt/resume 都立即生效在本地（sync read 永不落后于本进程的 sync write） | `halt()` 第一步 sync 调 `self._kill_switch.halt`，写 Redis/NATS 是后续步骤 |
| I2 | 4 进程内任何一个的 halt 都会在 ≤1s 内被另外 3 个的本地 cache 看到 | NATS 广播延迟 < 50ms（critical 路径已经在 Stage 5 验证），加 cache 更新 < 1ms |
| I3 | 进程崩溃 + restart 之后能恢复上一次 halt 状态 | `bootstrap()` 在 build_runtime 内 await Redis 读 |
| I4 | Redis 不可达不影响本进程的 halt 生效 | 写路径 best-effort；本地 cache 永远是第一步 |
| I5 | NATS 不可达不影响本进程的 halt 生效，但跨进程同步暂停 | 写路径 best-effort；本地 cache 永远是第一步 |
| I6 | 乱序的 NATS 事件不会让本地 cache 退到旧状态 | `_apply_remote_event` 校验 `payload.set_at_ts > self._last_applied_ts` |
| I7 | 测试调 `kill_switch.halt()` 直接路径不破 | 本地 KillSwitch 类 API 不变 |

---

## 6. 风险与缓解

### 6.1 sync ↔ async 阻抗导致的事件 loop 锁死

**风险**：`halt_threadsafe` 用 `run_coroutine_threadsafe` 投递到主 loop。如果主 loop 正在 await 一个长操作（例如 OKX REST 调用 5s），threadsafe 写要等到主 loop 有空闲才执行，可能超时。

**缓解**：
- timeout 默认 2s，超时 fall back 到本地 only（业务安全不被破坏）
- log warning `kill_switch_threadsafe_halt_partial`，operator 可在 dashboard 看到
- 实盘前验证：在 4 进程拓扑下让主 loop 持续忙的情况下手动 halt，看是否 < 2s 完成

### 6.2 NATS 广播 storm

**风险**：自动 halt 链路可能在某些场景反复触发（trial_guard 抖动）→ 反复 publish → NATS 流量暴增。

**缓解**：
- `halt()` 内部去重：如果当前 `kill_switch.halted == True` 且 `reason` 一致，跳过广播
- `_apply_remote_event` 同样去重：相同 set_at_ts 不重复 apply

### 6.3 测试期 stale Redis state 污染

**风险**：integration test 跑完留在 testcontainers Redis 里的 kill_switch 状态污染下一个测试。

**缓解**：
- 所有 Slice 6.2 集成测试 setUpClass 起 Redis 容器、tearDownClass 停容器
- 同一 testcase 里多个 test method 共享容器：每个 test 之间 `await client.flushdb()`
- 这与 Slice 6.1 集成测试一致，已有现成模板

### 6.4 Bootstrap 与 process_lifecycle 的循环依赖

**风险**：bootstrap() 需要 NATS bus.subscribe，但 bus 的某些后台任务（reconnect loop）可能晚于 bootstrap 启动。

**缓解**：
- bootstrap() 调用点在 `build_runtime` 内、bus 构造之后，bus 只需要 connect 通即可（subscribe 不要求所有后台任务都已启动）
- 已经在 Slice 6.1 验证：build_runtime 顺序里 bus.connect 早于 hot_state_store.connect 早于 _build_shared_slice
- 实盘验证：4 进程启动期间 docker logs 看 `kill_switch_sync_service_initialized` 早于 `process_lifecycle_ready`

### 6.5 单元测试用 fake bus / fake hot_state_store 的 mock 复杂度

**风险**：现有单元测试构造 KillSwitch 时直接 `KillSwitch()` 没有依赖。Slice 6.2 给某些 service 加 `kill_switch_sync` 字段后，测试要构造 mock。

**缓解**：
- `KillSwitchSyncService` 的依赖 `kill_switch + hot_state_store + bus + process_role + logger` 都可以用 InMemory 实现填
- 提供 `make_kill_switch_sync_service_for_tests()` helper（在 `tests/_helpers/...`），一句话拿到一个能用的 service
- 单元测试里如果不关心跨进程同步，**直接传 None 给 service 字段，service writer fall back 到 self.kill_switch.halt()**（向后兼容路径）

---

## 7. 实施步骤

### 7.1 改动文件清单

| 文件 | 变更 | 行数估计 |
|---|---|---|
| `aats/services/governance_engine/kill_switch_sync.py` | **新增**：KillSwitchSyncService 类 | ~250 |
| `aats/services/governance_engine/kill_switch.py` | 不动（保持 sync API） | 0 |
| `aats/events/topics.py` | 新增 `KILL_SWITCH_STATE = "system.kill_switch_state"` | +1 |
| `aats/bus/nats_bus.py` | 把 `KILL_SWITCH_STATE` 加进 `DEFAULT_CRITICAL_TOPICS` | +1 |
| `aats/bootstrap/config.py` | 构造 + bootstrap KillSwitchSyncService；ApplicationRuntime 加字段；stop_background_tasks 加 stop 调用 | +50 |
| `aats/services/governance_engine/trial_guard.py` | 加 `kill_switch_sync` 字段；W1 改成 `halt_threadsafe` | ~10 |
| `aats/services/governance_engine/derivatives_live_guard.py` | 加 `kill_switch_sync` 字段；W2 改成 `halt_threadsafe` | ~10 |
| `aats/services/execution_engine/recovery.py` | 加 `kill_switch_sync` 字段；W3 改成 `halt_threadsafe` | ~10 |
| `aats/services/recovery_control/startup_recovery.py` | 加 `kill_switch_sync` 字段；W4 改成 `halt_threadsafe` | ~10 |
| `aats/services/operator/reconciliation_system_queries.py` | W5 5 处改成 `await runtime.kill_switch_sync_service.halt/resume(...)` | ~20 |
| `tests/unit/test_kill_switch_sync.py` | **新增**：单元测试，覆盖 halt/resume/bootstrap/_apply_remote_event/threadsafe | ~250 |
| `tests/integration/test_kill_switch_cross_process.py` | **新增**：testcontainers 集成测试，2 个 KillSwitchSyncService 共享一台 Redis + InMemoryBus（模拟跨进程） | ~150 |

总计：约 750 行新增 + 100 行 modification。

### 7.2 实施顺序（每步可独立验证）

1. 新增 topic 常量 + 加入 critical 集合（编译通过即可）
2. 新增 `KillSwitchSyncService` + 单元测试（独立、不动 build_runtime）
3. 在 build_runtime 构造 + bootstrap KillSwitchSyncService，wire 进 ApplicationRuntime；ApplicationRuntime.stop_background_tasks 加 stop（**此时无 caller 用，是纯添加**——等于 Slice 6.1 的零行为变化策略）
4. 改 5 处生产写 caller（W1-W5），每改一处跑相关 unit test 验证
5. 跑全套 unit test（目标：1259 → 1260+ 全绿）
6. 写 + 跑 integration test
7. **真跑验证**：4 进程 docker compose；从 gateway 触发 halt；验证 4 进程的 `/system/health` 都看到 halted=true；杀 execution；新 execution 启动后看到 halted=true（bootstrap 路径）
8. runbook §10 加 Slice 6.2 章节
9. commit 与 Slice 6.1 同等粒度切分：
   - `feat(stage6-slice6.2): KillSwitchSyncService + cross-process kill_switch sync`
   - `docs(stage6-slice6.2): runbook §10 记录 Slice 6.2 真跑验证`

### 7.3 不在本 slice 内的跟进项

- portfolio_snapshot / ai_service / recovery_posture 接 hot_state_store（Slice 6.3+）
- KillSwitch + KillSwitchSyncService 二合一重构（API 简化，等 Slice 6.3 之后再做）
- 如果实盘期发现 NATS 偶发掉包导致 cache 漂移，加周期 Redis poll（默认 5s）

---

## 8. 验收标准

### 8.1 单元测试

- [ ] `test_kill_switch_sync.py` 包含：
  - bootstrap 从 Redis 读到 halt=True 并应用到本地 cache
  - bootstrap 读到 None 时本地 cache 维持默认
  - halt 写本地 + Redis + NATS 三层全部生效
  - halt Redis 失败时本地仍然 halt + warning log
  - halt NATS 失败时本地仍然 halt + Redis 仍然写入 + warning log
  - halt 重复调（reason 一致）跳过广播
  - resume 对称
  - `_apply_remote_event` 接收较新 set_at_ts 时更新本地，旧 set_at_ts 时跳过
  - `halt_threadsafe` 从 worker thread 调能更新本地 + Redis（用 IsolatedAsyncioTestCase + run_in_executor）
  - `halt_threadsafe` timeout 时本地仍然 halt + warning
- [ ] 全量 1259 unit test 不退化

### 8.2 集成测试

- [ ] `test_kill_switch_cross_process.py` 用 testcontainers 起 Redis + 内存 bus，模拟两个 KillSwitchSyncService 共享 Redis：
  - service A halt → service B 的 KillSwitch 在 1s 内变 halted=true
  - service A halt → 重启 service B（用 stop+rebuild）→ 新 service B bootstrap 后看到 halted=true
  - service A halt 然后 service B resume → 两个 KillSwitch 最终一致到 halted=false
  - 乱序事件（旧 set_at_ts 后到）不让本地 cache 回退

### 8.3 真跑验证（WSL2 4 进程 docker compose）

- [ ] 4 容器全部 healthy + 4 条 `kill_switch_sync_service_initialized` 日志
- [ ] gateway role POST `/system/blocker-control` 触发 manual halt → ≤1s 内 4 个进程的 `/healthz` / `/system/health` 都报 halted=true
- [ ] kill execution → docker restart → 新 execution log 显示 `kill_switch_sync_service_initialized bootstrap_state.halted=true`，符合 I3
- [ ] resume → 4 个进程 1s 内全部 halted=false
- [ ] redis CLIENT LIST 仍然看到 4 个稳定的 redis-py 连接
- [ ] NATS jsz 应该出现 `aats-{role}-system_kill_switch_state` 4 个 durable consumer，全部 push_bound

### 8.4 完成判定文档

- [ ] runbook §10 新增 Slice 6.2 章节，含真跑验证矩阵 + 关键命令 + 故障演练（杀 redis / 杀 nats）行为描述

---

## 9. 回滚预案

- 如果 Slice 6.2 真跑出现严重问题（kill_switch 误报 / 漏报）：
  - 立刻 `git revert` Slice 6.2 commit
  - 5 处 writer 调用回退到 `kill_switch.halt(reason)`（直接 sync 调用本地）
  - KillSwitchSyncService 与 topic 常量保留为 dead code，下一次再修
- 如果只是 cross-process 同步偶发延迟（< 5s）但不影响安全：
  - 加 metric `kill_switch_sync_lag_seconds`，观察一周
  - 不回滚，进入 Slice 6.3
- 如果 Redis bootstrap 拖慢 build_runtime > 5s：
  - 把 bootstrap 改成 background task（在 start_background_tasks 内异步执行），允许 build_runtime 立刻返回；但代价是启动期 1-2s 内本地 cache 不准确

---

## 10. 备份策略

按改动纪律，实施前：
- 创建备份分支：`git branch backup/pre-slice6.2 main`
- 实施期间在 feature branch 上做：`git checkout -b feat/stage6-slice6.2`
- 真跑验证通过后再合回 main
- 不在 main 上直接改

---

**状态机**：本设计文档**待审批**。批准后按 §7.2 顺序实施。
