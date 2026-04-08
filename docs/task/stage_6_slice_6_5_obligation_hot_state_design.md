# Stage 6 Slice 6.5 设计文档：obligation 热状态跨进程缓存

> 状态：**已批准实现（用户口头授权 2026-04-08 跳过审批环节）**
> 前置：Slice 6.1（HotStateStore Redis backend）/ 6.2（KillSwitchSyncService 原型）/ 6.3（PortfolioSnapshotCache）/ 6.4（KillSwitch 二合一）全部已上线
> 后续：Stage 9 dryrun（operator 真跑验证）、Slice 6.6（open_orders 热状态缓存，待定）
> 安全网 git tag：`pre-stage6-slice6.5-v1`（commit 51ed9b4）

---

## 1. 问题陈述

### 1.1 背景与来源

`docs/task/stage_6_redis_hot_state_design.md` §1.3 的"不在 Stage 6 范围（明确推迟）"表格里原本挂着一条：

> obligation 表 / open_order 表 缓存 → Stage 6.4（独立后续 PR）
> 理由：这两张表的写路径比 portfolio 更复杂（per-order 增量），先把 6.3 的 write-through 框架打稳再扩展

后来 6.4 这个 slice 编号被 KillSwitch 二合一重构占用了（`docs/task/stage_6_slice_6_4_kill_switch_unification_design.md`）。"obligation + open_order 热状态缓存"这件事就一直挂在 TODO 上没有正式 slice 编号。

本文档将其命名为 **Slice 6.5**，并且**只做 obligation**；open_orders 因涉及两条数据源（`execution_repo.list_order_states` + `execution_order_repo.open_orders()`）schema 不统一、改造面大，推迟到 Slice 6.6（如果真的需要再上马）。

### 1.2 当前 obligation 读路径的瓶颈

`active_obligations()` / `all_obligations()` / `get_obligation(coid)` 三个方法全部打 Postgres（`aats/storage/obligation_repo_postgres.py:69-86`），在 4 进程拓扑下的 hot caller：

| 调用点 | 文件 | 进程角色 | 频率 | 当前行为 |
|---|---|---|---|---|
| **W1** risk pre-check | `aats/services/governance_engine/risk.py:1693` | **decision** | 每次 decision 评估 = 高频 | SELECT active obligations → 计算 reserved_amount 汇总 |
| **W2** recovery posture | `aats/services/governance_engine/recovery_posture.py:356` | decision | 每次 posture evaluation | `active_obligations()` |
| **W3** operator dashboard | `aats/services/operator/query_service.py:7476` | **gateway** | dashboard polling 5-10s | `all_obligations()` |
| **W4** execution control monitor | `aats/services/execution_control/monitor.py:91` | gateway | dashboard polling 5-10s | `all_obligations()` |
| **W5** execution reservation | `aats/services/execution_engine/obligations.py:93` | execution | 每次 `reserve_for_intent` | `active_obligations()` 去重 |
| **W6** execution recovery | `aats/services/execution_engine/recovery.py:465,476` | execution | 启动恢复期一次性 + 后续定期 | `active_obligations()` |
| **W7** okx account sync | `aats/services/execution_engine/okx_adapter.py:1384` | execution | 账户快照同步时 | `active_obligations()` |

W1 是最痛的一处：decision 每做一次 risk pre-check 都要打 PG，4 进程拓扑下 decision 和 execution 是不同容器、不同进程，走 PG 链路延迟 3-15ms 不等，跨 symbol 扩张后会线性恶化。

W3/W4 是次痛的：dashboard poll 5-10s 一次，每次几十 ms 的 PG 开销虽然不致命但 waste bandwidth。

W5-W7 是**execution 进程内部**的读路径 —— 由于 obligation 的写方也是 execution，这些调用等价于"从本地 PG 读回刚写的数据"。这类调用不需要跨进程，但也会被本 slice 的 cache 顺带优化（local dict hit，零 PG IO）。

### 1.3 目标

1. 把 `active_obligations()` / `all_obligations()` / `get_obligation(coid)` 三个读路径的 hot caller 加一层 in-memory cache
2. cache 跨 4 进程同步：execution 写入立即广播到 decision/gateway/market，≤1s 内可见
3. 绝对不动写入路径的正确性：Postgres 仍然是 source of truth，cache miss/redis 不可达全部 fallback PG
4. **sync 签名 100% 兼容**：所有 caller 不改一行，由 cache 在 `obligation_repo` 上层透明接入
5. 5 个不变量 I1-I5 保留：fail-soft、cross-process、idempotent、restart-safe、ordering

### 1.4 不在本 slice 范围

| 项 | 推迟到 | 理由 |
|---|---|---|
| `open_orders` 热状态缓存 | Slice 6.6（待定） | 数据源两条、schema 不统一、先看 Slice 6.5 效果再决定 |
| `obligation_repo.save_obligation_in_session` 内联 cache 写 | 不做 | session 提交 vs cache 写的事务边界复杂，改在 service 层统一触发 |
| NATS durable consumer 名字重命名 | 不做 | 沿用 6.3 同款 `_CollectingBus` deferred subscribe 模式 |
| Decision 进程 hydrate 时也同步本地 obligation_repo 副本 | 不做 | decision 进程不需要本地 obligation_repo，只读 cache 即可 |

---

## 2. 设计决策

| # | 决策 | 理由 |
|---|---|---|
| **D1** | cache 类名 `ObligationHotStateCache`，放 `aats/services/execution_engine/obligation_cache.py` | execution_engine 子树天然适合，与 `obligations.py`（ExecutionObligationService）同侧 |
| **D2** | 持有本地 `dict[client_order_id, OrderObligation]` 作为 source of cached truth | 符合 6.3 的"local dict + sync readers"模式；sync caller 无需 await |
| **D3** | `publish(obligation)` 由 `ExecutionObligationService` 在每次 `save_obligation` 之后调用 | **不**改 `obligation_repo_postgres.save_obligation_in_session`；改在 service 层避免污染 session 事务边界 |
| **D4** | 新 NATS topic `OBLIGATION_UPDATES = "execution.obligation_updates"` | 没有现成的 outbox envelope 可 piggyback；单独广播一条 lightweight envelope |
| **D5** | `publish` 内部三步：local dict set → best-effort Redis set → best-effort NATS publish | 与 KillSwitch `_publish` 一致；fail-soft |
| **D6** | Redis key：`aats:hot:obligation:by_coid:<client_order_id>`，外加 index key `aats:hot:obligation:index` 存 `{active_coids: [...], all_coids: [...], version: N, updated_at: ts}` | bootstrap 时通过 index 拉全量；避免 Redis SCAN |
| **D7** | **无 TTL**：obligation 生命周期就是 reserve→consume→release 的业务流，不用 Redis TTL 兜底；terminal 状态（RELEASED/CANCELED/FAILED）保留 6 小时后由 cache 主动 `delete` | 防止 Redis 长期堆积 terminal 行；业务侧不会立即读旧 terminal，TTL 不合理但延迟 GC 合理 |
| **D8** | `bootstrap()` 逻辑：read index → `get_many` 所有 coids → 构造本地 dict → subscribe NATS OBLIGATION_UPDATES | 与 6.3 `bootstrap(scope_fingerprint)` 模式一致 |
| **D9** | `_handle_remote_event()` 用 `last_update_ts <= local` idempotent 判断 | 与 6.3 `snapshot_ts` 规则一致；同一 coid 的不同时戳事件按 ts 单调性 |
| **D10** | cache 不 loopback filter：source_role 字段不加 | 远端事件的 `last_update_ts` 和本地相同时 noop，天然去重 |
| **D11** | **读路径只改 3 处**：risk.py:1693 / recovery_posture.py:356 / query_service.py:7476（以及 execution_control/monitor.py:91）。其它 execution 进程内部 caller（obligations.py:93 / recovery.py:465 / okx_adapter.py:1384）**可选**改或不改 —— 它们本就在 execution 进程本地，cache.get 比 PG SELECT 快但 PG 也可接受 | 把 cross-process 热点优先打掉；execution 本地读的改造留给 follow-up |
| **D12** | 零参 `ObligationHotStateCache()` 构造允许；未 bootstrap 时 `publish` 退到 local-only（不写 Redis、不发 NATS，不抛） | 与 KillSwitch 6.4 `__init__`+`bootstrap()` 分离模式一致；单测场景友好 |
| **D13** | `get_many_sync(coids)` / `active_sync()` / `all_sync()` 都是 sync 方法，返回 None 时 caller fallback PG | sync API 对 caller 透明；与 6.3 `get_sync(scope)` 一致 |
| **D14** | `ApplicationRuntime.obligation_hot_state_cache: ObligationHotStateCache \| None = None` 字段 | 注入点在 `_build_shared_slice`，延后注入 hot_state_store 和 bus 通过 `bootstrap()` |
| **D15** | 4 进程拓扑下所有 role 都装 cache，行为对称，cache 类内部没有 process_role 分支 | D8 / Slice 6.3 D8 一致 |
| **D16** | monolith 模式下 cache 仍然装，backend=memory HotStateStore + in-memory bus，**行为等价**原始 `obligation_repo` 直读 | 零行为差异，monolith 不受影响 |

---

## 3. `ObligationHotStateCache` API 表

| 方法 | 签名 | 调用方 | 行为 |
|---|---|---|---|
| `__init__` | `ObligationHotStateCache(*, logger)` | `build_runtime` | 本地 `_latest: dict[str, OrderObligation] = {}`；不接 Redis/NATS |
| `bootstrap` | `(*, hot_state_store, bus, process_role, subscribe=True) → coroutine` | `build_runtime` / tests | 从 Redis hydrate 全部 coids + 订阅 NATS |
| `register_remote_subscription` | `(bus) → coroutine` | `_wire_event_subscriptions`（4 进程 `_CollectingBus`） | 订阅 OBLIGATION_UPDATES |
| `publish` | `(obligation) → coroutine` | `ExecutionObligationService` 5 处 save_obligation 调用后 | 本地 set + Redis set + NATS publish |
| `get_sync` | `(client_order_id) → OrderObligation \| None` | W1 read path wrapper | 本地 dict get |
| `active_sync` | `() → list[OrderObligation]` | W1/W2 read path wrapper | 本地 dict filter status |
| `all_sync` | `() → list[OrderObligation]` | W3/W4 read path wrapper | 本地 dict values |
| `remove_sync` | `(client_order_id) → None` | 内部 GC 用；terminal + 6h 后 | 本地 dict pop + 异步 Redis delete |
| `stop` | `() → coroutine` | `stop_background_tasks` | log-only 清理（Redis/NATS 不动，下次启动仍可 hydrate） |
| `snapshot` | `() → dict` | dashboard / startup log | introspection dict |

---

## 4. Redis Schema

### 4.1 Index key

```
key:   aats:hot:obligation:index
value: {
  "active_coids":   ["coid1", "coid2", ...],     // ACTIVE + PARTIALLY_CONSUMED
  "all_coids":      ["coid1", "coid2", ..., "coidN"],  // 所有未 GC 的
  "version":        <monotonic int>,
  "updated_at":     <unix epoch ts>,
  "writer_role":    <"execution" | "monolith" | ...>
}
```

index 在每次 `publish()` 写完 per-key 之后被原子重写。并发安全：execution 是唯一写方，没有多 writer 竞争。

### 4.2 Per-coid key

```
key:   aats:hot:obligation:by_coid:<client_order_id>
value: <OrderObligation.model_dump(mode='json')>
```

terminal 行（status in RELEASED/CANCELED/FAILED）在 6h 后被 cache 内部 GC 循环 delete。

### 4.3 bootstrap 流程

```python
async def bootstrap(self, *, hot_state_store, bus, process_role, subscribe=True):
    self._hot_state_store = hot_state_store
    self._bus = bus
    self._process_role = process_role
    # Step 1: Redis hydrate
    try:
        index = await hot_state_store.get(INDEX_KEY)
    except Exception as exc:
        log_event("obligation_cache_bootstrap_index_failed", ...)
        index = None
    if isinstance(index, dict):
        coids = index.get("all_coids", [])
        keys = [make_key(NS_OBLIGATION, "by_coid", coid) for coid in coids]
        try:
            raws = await hot_state_store.get_many(keys)
        except Exception as exc:
            log_event("obligation_cache_bootstrap_get_many_failed", ...)
            raws = {}
        for key, raw in raws.items():
            try:
                obligation = OrderObligation.model_validate(raw)
                self._latest[obligation.client_order_id] = obligation
            except Exception:
                log_event("obligation_cache_bootstrap_parse_failed", ...)
                continue
        log_event("obligation_cache_bootstrap_hydrated", count=len(self._latest))
    else:
        log_event("obligation_cache_bootstrap_empty")
    self._bootstrapped = True
    # Step 2: Subscribe (deferred 路径由 _wire_event_subscriptions 调)
    if subscribe:
        await self.register_remote_subscription(bus)
```

---

## 5. 写路径接入点

### 5.1 `ExecutionObligationService` 5 个 save 点

`aats/services/execution_engine/obligations.py` 里 `obligation_repo.save_obligation(...)` 出现 5 次：

| 方法 | 行号 | 描述 |
|---|---|---|
| `reserve_for_intent` | 60 | 预留新 obligation |
| `persist_previewed_obligation` | 65 | preview 之后持久化 |
| `consume_for_fill` | 135 | fill 扣减 |
| `finalize_for_order_state` | 177 | 订单终态收尾 |
| (execution_engine/outbox.py:126,242,386) | - | outbox publish 期间带 obligation |

**改造方案**：
1. `ExecutionObligationService.__init__` 接受 `obligation_hot_state_cache: ObligationHotStateCache | None = None` 参数
2. 每次 `save_obligation` 返回之后，如果 cache 不为 None，`await cache.publish(obligation)`
3. `outbox.py` 的三个 `save_obligation_in_session` 点**暂不动** —— 它们是 session 内部操作，cache 写要在 session commit 之后；改造空间留到 Slice 6.5.1 补丁
4. `recovery.py:470` 的 `self.obligation_repo.save_obligation(updated)` 也加 cache.publish hook

注意：因为 publish 是 async 而 `save_obligation` 调用方基本都是 sync/async 混着用的，需要 wrapper 函数处理：

```python
def _publish_obligation_cache(obligation: OrderObligation) -> None:
    """sync-safe cache publish helper。
    
    在当前 event loop 上 fire-and-forget；无 loop（sync context）时 log 跳过。
    """
    if self.obligation_hot_state_cache is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # no running loop → skip
    loop.create_task(self.obligation_hot_state_cache.publish(obligation))
```

### 5.2 recovery.py 批量 save 场景

`execution_engine/recovery.py:470` 在循环里批量 `save_obligation`，每次循环都 fire-and-forget publish 是 OK 的，cache publish 的 idempotent 规则（by last_update_ts）保证即使顺序错乱也不会写退化数据。

---

## 6. 读路径接入点

### 6.1 W1 risk.py:1693

```python
# before:
for obligation in self.obligation_repo.active_obligations():
    ...

# after:
cache = getattr(self.runtime, "obligation_hot_state_cache", None)
cache_result = cache.active_sync() if cache is not None else None
if cache_result is not None:
    obligations_iter = cache_result
else:
    obligations_iter = self.obligation_repo.active_obligations()
for obligation in obligations_iter:
    ...
```

**降级规则**：cache 未 bootstrap 或 runtime 未装时，`cache.active_sync()` 返回空列表 `[]`（注意不是 None），此时**不能**直接用这个空列表 —— 空列表是"明确没有 active obligation"的信号，而未初始化应该视作"不知道，去打 PG"。

为了区分这两种情况，cache 新增 `_bootstrapped` flag，`active_sync()` 在未 bootstrap 时返回 `None`，bootstrap 后返回实际列表（可能为空）：

```python
def active_sync(self) -> list[OrderObligation] | None:
    if not self._bootstrapped:
        return None
    return [o for o in self._latest.values() if o.status in {"ACTIVE", "PARTIALLY_CONSUMED"}]
```

### 6.2 W2 recovery_posture.py:356

同 W1 pattern。

### 6.3 W3 query_service.py:7476

```python
# before:
else max(len(self.runtime.obligation_repo.all_obligations()) - ..., 0)

# after:
obligation_count = self._obligation_all_count()

def _obligation_all_count(self) -> int:
    cache = getattr(self.runtime, "obligation_hot_state_cache", None)
    cache_result = cache.all_sync() if cache is not None else None
    if cache_result is not None:
        return len(cache_result)
    return len(self.runtime.obligation_repo.all_obligations())
```

### 6.4 W4 execution_control/monitor.py:91

同 W3 pattern。

### 6.5 W5-W7 execution 本地 caller

**不在本 slice 改造**：这些调用本就在 execution 进程本地 PG，cache 命中不会显著加速（local PG 本就 <1ms）；另外 cache 和 PG 的一致性在 execution 进程内同样有保证（同一个 publish 路径），但改这几个点会引入测试面扩展，收益比不高。

---

## 7. NATS 事件 schema

### 7.1 topic

```python
# aats/events/topics.py
OBLIGATION_UPDATES = "execution.obligation_updates"
```

### 7.2 envelope

```python
EventEnvelope(
    event_type="OrderObligationUpdated",
    source_component="aats.execution_engine.obligation_cache",
    topic=topics.OBLIGATION_UPDATES,
    key=obligation.client_order_id,
    payload=obligation.model_dump(mode="json"),
)
```

payload 就是 `OrderObligation.model_dump(mode='json')`。接收侧 `_handle_remote_event` 用 `parse_envelope(message).payload` → `OrderObligation.model_validate()`。

### 7.3 `_wire_event_subscriptions` 接入

与 6.3 `PortfolioSnapshotCache` 一样，在 `bootstrap/config.py::_wire_event_subscriptions` 里把 cache 的 `register_remote_subscription` 调用挂到 `_CollectingBus` 上，避开 NATS JetStream durable consumer 重复绑定问题。

---

## 8. 不变量 I1-I5

| # | 不变量 | 保证机制 |
|---|---|---|
| I1 | fail-soft：cache 任何失败都不阻塞 obligation_repo 的主写入路径 | `publish` 的 Redis/NATS 步骤全是 best-effort try/except；`_publish_obligation_cache` wrapper 的 loop 检查 |
| I2 | cross-process ≤1s：execution 写 → decision 可见 | NATS 实时广播；local dict on publish 立即可见 |
| I3 | restart-safe：进程崩溃 → 重启后 cache 恢复最近状态 | bootstrap 从 Redis 读 index + get_many |
| I4 | idempotent：同 coid 的乱序事件不会让本地退化 | `_apply_locally` 的 `last_update_ts <= existing.last_update_ts` 规则 |
| I5 | miss 不破坏读：cache 未 bootstrap / Redis 挂 / NATS 挂 | `get_sync` / `active_sync` / `all_sync` 返回 `None` → caller fallback obligation_repo |

---

## 9. 测试矩阵

| # | 测试 | 文件 | 验证 |
|---|---|---|---|
| T1 | 零参构造 | unit | `ObligationHotStateCache()` 后 `active_sync() is None` |
| T2 | bootstrap 无数据 | unit | InMemoryHotStateStore 空 → bootstrap → `_bootstrapped=True`, `active_sync() == []` |
| T3 | bootstrap hydrate | unit | 预写 index + 2 个 coid 到 store → bootstrap → `_latest` 含 2 个 obligation |
| T4 | publish 本地 + Redis | unit | `await cache.publish(obligation)` → `_latest[coid]` set + Redis `get(by_coid:coid)` 含 payload |
| T5 | publish NATS 广播 | unit | 用 FakeBus，assert `publish()` 调用次数 + topic + payload |
| T6 | publish idempotent | unit | 同 coid 的 last_update_ts 回退版本 → 本地不变，Redis 不再写 |
| T7 | publish idempotent (equal ts) | unit | 同 ts 同 state → noop（避免广播 storm） |
| T8 | Redis set 异常 | unit | mock store.set raises → publish 不抛，log warning，local 仍然 set |
| T9 | NATS publish 异常 | unit | mock bus.publish raises → publish 不抛，log warning，local 仍然 set |
| T10 | remote event apply | unit | 喂 OBLIGATION_UPDATES envelope → `_latest` 被 apply |
| T11 | remote event stale | unit | last_update_ts 比本地旧 → noop |
| T12 | remote event parse fail | unit | envelope.payload 缺字段 → `_handle_remote_event` 不抛 |
| T13 | active filter | unit | `_latest` 含 3 ACTIVE + 2 RELEASED → `active_sync()` 返回 3 |
| T14 | get_sync | unit | 插入 1 coid → `get_sync(coid)` 返回该 obligation；未知 coid → None |
| T15 | GC terminal | unit | `remove_sync` 清本地 + async Redis delete |
| T16 | bootstrap with subscribe=False | unit | subscribe=False → 不调 bus.subscribe；`register_remote_subscription(bus)` 显式调则生效 |
| T17 | `snapshot()` introspection | unit | 返回 dict 含 `_bootstrapped` / `_subscribed` / cached_count |
| T18 | risk.py wrapper degrade | unit | cache is None → fallback PG |
| T19 | risk.py wrapper active_sync 返回 None | unit | `_bootstrapped=False` → 用 PG |
| T20 | risk.py wrapper active_sync 返回空 list | unit | `_bootstrapped=True` 但无 obligation → 用 cache（空）不打 PG |

共 20 个单测。integration test 推迟到 Stage 6.5.1 补丁（testcontainers + 真 Redis + cross-process）。

---

## 10. bootstrap/config.py 改动清单

1. `_RuntimeSlices`：加字段 `obligation_hot_state_cache: ObligationHotStateCache | None = None`
2. `ApplicationRuntime`：同加字段
3. `_build_shared_slice`：
   ```python
   obligation_hot_state_cache = ObligationHotStateCache(
       logger=get_logger("aats.execution.obligation_cache"),
   )
   slices.obligation_hot_state_cache = obligation_hot_state_cache
   ```
4. `build_runtime`（在 hot_state_store/bus/event_store 构造完之后）：
   ```python
   if slices.obligation_hot_state_cache is not None:
       await slices.obligation_hot_state_cache.bootstrap(
           hot_state_store=slices.hot_state_store,
           bus=slices.bus,
           process_role=effective_process_role or "monolith",
           subscribe=False,  # deferred to _wire_event_subscriptions
       )
   ```
5. `_wire_event_subscriptions`：
   ```python
   if runtime.obligation_hot_state_cache is not None:
       await runtime.obligation_hot_state_cache.register_remote_subscription(collecting_bus)
   ```
6. `_apply_post_init_guards`：把 cache 注入 `ExecutionObligationService`
7. `stop_background_tasks`：在 kill_switch.stop 之前加
   ```python
   try:
       cache = getattr(self, "obligation_hot_state_cache", None)
       if cache is not None:
           await cache.stop()
   except Exception as exc:
       log_event("obligation_cache_shutdown_failed", ...)
   ```

---

## 11. 回滚策略

- **git tag**：`pre-stage6-slice6.5-v1`（commit 51ed9b4）已打
- **回滚**：`git reset --hard pre-stage6-slice6.5-v1` 回到 Stage 9 完成状态
- **非破坏性**：cache 是纯加法；读路径有 None-check fallback PG；写路径 publish 是 best-effort。任何中间状态回滚都不会损坏业务

---

## 12. 工时与依赖

- **工时**：单 conversation 可完成
- **依赖**：Slice 6.1（HotStateStore）+ Slice 6.2 / 6.3 / 6.4（cache 模式） 全部已上线
- **Stage 9 / 阶梯 dryrun 不依赖本 slice**：Stage 9 AbortHookService + DriftScore 的 inputs_provider 不 touch obligation，本 slice 是 orthogonal 加法

---

## 13. Changelog

- 2026-04-08 首版。范围从原计划的 "obligation + open_order" 收窄到 "obligation only"；open_order 推迟到 Slice 6.6。
