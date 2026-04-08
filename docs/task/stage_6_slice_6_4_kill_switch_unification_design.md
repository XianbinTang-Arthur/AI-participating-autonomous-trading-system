# Stage 6 Slice 6.4 设计文档：KillSwitch 二合一重构（消灭 dual-class fallback）

> 状态：**已批准实现（用户口头授权 2026-04-08，跳过审批环节）**
> 前置：Slice 6.2（KillSwitchSyncService 边车）、Slice 6.3（PortfolioSnapshotCache 边车）已上线
> 后续：Stage 8（OTel 端到端 trace）、Stage 6 剩余 hot state、Stage 9 dryrun
> 安全网 git tag：`pre-stage6-slice6.4-v1`

---

## 1. 问题陈述：dual-class anti-pattern + W1-W5 fallback bug

### 1.1 当前架构（Slice 6.2 之后）

```
KillSwitch                    KillSwitchSyncService
（30 行 data holder）         （507 行 sidecar）
─────────────────             ─────────────────────
+ halt(reason) ← sync         + bootstrap()         ← async
+ resume()     ← sync         + stop()              ← async
+ status()     ← sync         + halt(reason)        ← async
+ halted (prop)               + resume()            ← async
                              + halt_threadsafe()   ← sync wrapper
                              + resume_threadsafe() ← sync wrapper
                              + _handle_remote_event
                              + _publish_*
                              + snapshot()
```

调用方分工：

- **50+ 个 sync 读路径**（`api/auth_routes.py`, `governance_engine/health.py`, `execution_engine/order_manager.py:136`, ...）只读 `kill_switch.halted` / `kill_switch.status()` —— 直接打 KillSwitch
- **5 个 sync 写路径**（W1-W5）通过 `kill_switch_sync` 写 —— 需要 if/else fallback
- **4 个 async 写路径**（reconciliation_system_queries 内部）通过 `kill_switch_sync_service` 直接 await

### 1.2 W1-W5 fallback 的资金安全 bug

5 个 sync 写入点（trial_guard / derivatives_live_guard / execution_recovery / startup_recovery / reconciliation_system_queries 部分）共有同一段 if/else：

```python
if self.kill_switch_sync is not None:
    self.kill_switch_sync.halt_threadsafe(reason)
else:
    self.kill_switch.halt(reason=reason)  # ← 静默退化为 local-only
```

这段代码的初衷是"sync service 没注入时不要崩"，但在 4 进程拓扑下：

- 如果 `kill_switch_sync` 因为某个 build_runtime 路径漏装配 = None
- trial_guard / derivatives_live_guard 触发 halt 时走 else 分支
- **本地 halt 生效，跨进程广播被静默吞掉**
- 其他 3 个进程的 KillSwitch 状态仍然是 False
- execution 进程的 `order_manager.py:136` 继续放行订单
- **资金风险**

这个 bug 在 Slice 6.2 提交时就有，只是被"边车 always 注入"的 build_runtime 路径暂时遮住。但任何未来重构（增加 process role / 更细粒度 slice / 测试 mock）都可能重新触发。

### 1.3 dual-class 还有哪些问题

- **API 不一致**：sync 写要走 `halt_threadsafe`，async 写要走 `halt`，read 走 `kill_switch.halted` —— 三套 API 三个对象
- **测试痛**：单元测试要么用 bare KillSwitch（简单但跑不到边车路径），要么搭 InMemoryHotStateStore + InMemoryEventBus + KillSwitchSyncService（重）
- **构造顺序复杂**：KillSwitch 在 _build_shared_slice 里构造，KillSwitchSyncService 在 build_runtime 后期构造，5 个 slice builder 都要把 sync_service 注入下游 —— 注入链长且容易漏
- **stop_background_tasks 多一个 try/except**：边车关闭单独写一段 close_failed log
- **诊断割裂**：snapshot 在边车里，halted/status 在 KillSwitch 上，dashboard 要拼

---

## 2. Slice 6.4 目标

把 `KillSwitch` + `KillSwitchSyncService` 合并成**单一 KillSwitch 类**，满足：

1. **Slice 6.2 的 7 个不变量 I1-I7 全部保留**（本地 cache / Redis 持久化 / NATS 广播 / 重启恢复 / 乱序拒绝）
2. **W1-W5 写入点删掉 if/else fallback**：直接 `self.kill_switch.halt(reason)`，不再判断 sync_service 是否存在
3. **50+ 个 sync 读路径不动一行**：`.halted` / `.status()` 接口完全等价
4. **零参 `KillSwitch()` 仍然可用**：测试不需要 bus/store，sync halt/resume 立即生效，纯本地模式
5. **delete `kill_switch_sync.py`**：源代码 -507 行
6. **构造序列简化**：bare construct → bootstrap(deps) → done

不在本 slice 范围：
- `kill_switch_sync` 字段在 W1-W5 服务里的整段拆除（已合在 W1-W5 实现里）
- 测试文件改名（保留 `test_kill_switch_sync.py` 文件名但内容改为测试新 API）
- 删除 W1-W5 服务的 `kill_switch` + `kill_switch_sync` 双字段：合并为单 `kill_switch` 字段

---

## 3. 设计决策

| # | 决策 | 理由 |
|---|---|---|
| **D1** | 删除 `KillSwitch` data holder 类，把 `(halted, reason)` state 内化到合并类 | 一个 state，一个 owner |
| **D2** | 合并类沿用 `KillSwitch` 名字（不叫 `KillSwitchSyncService`） | 50+ 个读路径已经按 KillSwitch 命名，改名等于改 50 个文件 |
| **D3** | sidecar deps（`hot_state_store` / `bus` / `process_role` / `logger`）在 `bootstrap()` 注入，**不在 `__init__`** | 构造序列：`KillSwitch()` 在 `_build_shared_slice` 里早期构造（bus 还没 connect），bootstrap 在 `_start_event_bus` 之后挂接 sidecar deps，不破坏 `mode_controller` 等持有的引用 |
| **D4** | sync `halt(reason)` 内部自动 dispatch 到主 loop（fire-and-forget 或 run_coroutine_threadsafe），合并 `halt` + `halt_threadsafe` 两个 API | W1-W5 调用方不再需要知道自己是 main loop 还是 worker thread，统一调 `kill_switch.halt(reason)` |
| **D5** | async 版本叫 `halt_async` / `resume_async`，给 reconciliation_system_queries 的 4 个 await 调用点用 | 用 `_async` 后缀比改写所有 sync 调用点更小 |
| **D6** | bootstrap 保留 Redis hydration 语义（halted=True 时 apply 本地，halted 不存在时不动） | I3 + 避免冷启动两个进程互相覆盖 |
| **D7** | 保留 `set_at_ts` 单调性 + source_role loopback filter | I6 + 自广播去重 |
| **D8** | **完全删除 W1-W5 fallback if/else**：调用方只写 `self.kill_switch.halt(reason)` | 资金安全 bug 根治 |
| **D9** | `KillSwitch()` 零参构造允许；未 bootstrap 时所有 sync 写都退到 local-only（不抛、不 log warning，因为这是合法的"测试 / 启动期早期"场景） | 测试与启动期 mode_controller 装配不需要边车 |
| **D10** | 保留 tuple 原子赋值（`self._state = (True, reason)`）做 read 侧无锁一致性 | Stage 6 Slice 6.2 已验证 |
| **D11** | 17 个现有 `KillSwitchSyncService` 单测 + 4 个 testcontainers 集成测试要全部继续通过（重写测试代码而非保持 import 兼容） | 测试代码改写比保留 shim 更干净 |
| **D12** | 迁移路径：写新类 → 删 `kill_switch_sync.py` → 改 `bootstrap/config.py` → 改 W1-W5 5 个文件 → 改测试 | 一次完整 commit，不留半成品 |

---

## 4. 新 KillSwitch 类的公共 API 表

| 方法 | 签名 | 调用方 | 行为 |
|---|---|---|---|
| `__init__` | `KillSwitch()` | `_build_shared_slice` (line 2916), 测试 | 仅初始化本地 `_state = (False, None)` |
| `halted` | `@property → bool` | 50+ 读路径 | 读 `_state[0]` |
| `status` | `() → dict[str, bool/str/None]` | 50+ 读路径 | 读 `_state` |
| `halt` | `(reason='manual_halt') → None` | W1-W5 + 测试 | 本地立即 + sync dispatch 跨进程 |
| `resume` | `() → None` | W1-W5 + 测试 | 本地立即 + sync dispatch 跨进程 |
| `halt_async` | `(reason='manual_halt') → coroutine` | reconciliation_system_queries 4 个 await 点 | 本地立即 + await 完整 publish |
| `resume_async` | `() → coroutine` | reconciliation_system_queries 4 个 await 点 | 本地立即 + await 完整 publish |
| `bootstrap` | `(*, hot_state_store, bus, process_role, logger) → coroutine` | `build_runtime` (替代之前的 `KillSwitchSyncService(...)` + `await bootstrap()`) | 缓存 sidecar deps + Redis hydrate + NATS subscribe |
| `stop` | `() → coroutine` | `stop_background_tasks` | 标记 loop None；不写 Redis（重启需要恢复） |
| `snapshot` | `() → dict` | dashboard / 启动 log | 完整 introspection（process_role / bootstrapped / subscribed / last_applied_ts / kill_switch state） |

---

## 5. sync `halt` 的三种执行路径

```python
def halt(self, reason: str = "manual_halt") -> None:
    # Step 1: 本地立即生效（永远）
    self._state = (True, reason)
    # Step 2: 跨进程广播（best-effort）
    self._dispatch_async_publish(halted=True, reason=reason)
```

`_dispatch_async_publish` 内部分支：

| 分支 | 触发条件 | 处理 |
|---|---|---|
| **未 bootstrap** | `_bus is None` 或 `_loop is None` 或 `loop.is_closed()` 或 `not loop.is_running()` | 直接 return（pure local mode） |
| **主 loop 线程** | `asyncio.get_running_loop() is self._loop` | `loop.create_task(coro)` fire-and-forget |
| **worker thread** | 其他情况 | `run_coroutine_threadsafe(coro, self._loop)` + `future.result(timeout=2.0)` |

`_publish` 内部主体（async）：

```
1. dedup 检查（last_published_state == new_state → return）
2. 推进 last_published_state
3. _best_effort_redis_set(payload)   # try/except warning
4. _best_effort_nats_broadcast(payload)  # try/except warning
5. log_event "kill_switch_published"
```

---

## 6. 调用方迁移清单

### 6.1 Bootstrap config (`aats/bootstrap/config.py`)

- **import**：删 `from aats.services.governance_engine.kill_switch_sync import KillSwitchSyncService`
- **`ApplicationRuntime` dataclass**：删字段 `kill_switch_sync_service: KillSwitchSyncService`
- **`stop_background_tasks`**：删 `await self.kill_switch_sync_service.stop()` 的 try/except 段，改为 `await self.kill_switch.stop()`
- **`_apply_post_init_guards`**：删 `kill_switch_sync=runtime.kill_switch_sync_service` 给 derivatives_live_guard / trial_guard 的注入
- **`_build_reconciliation_slice`**：删 `kill_switch_sync=slices.kill_switch_sync_service` 给 ExecutionRecoveryService / ExecutionLedgerRecoveryService 的注入
- **`build_runtime`**：删 `slices.kill_switch_sync_service = KillSwitchSyncService(...)` + `await ...bootstrap()`，改为 `await slices.kill_switch.bootstrap(hot_state_store=hot_state_store, bus=slices.bus, process_role=effective_process_role or "monolith", logger=get_logger("aats.governance.kill_switch"))`

### 6.2 Slice container (`_RuntimeSlices`)

- 删 `kill_switch_sync_service: KillSwitchSyncService | None = None` 字段

### 6.3 W1 trial_guard.py

- 删 `from aats.services.governance_engine.kill_switch_sync import KillSwitchSyncService`
- 删 dataclass 字段 `kill_switch_sync: KillSwitchSyncService | None = None`
- `_trigger_halt`：

```python
# before:
if self.kill_switch_sync is not None:
    self.kill_switch_sync.halt_threadsafe(reason)
else:
    self.kill_switch.halt(reason=reason)
# after:
self.kill_switch.halt(reason=reason)
```

### 6.4 W2 derivatives_live_guard.py

- 同 W1，import + dataclass 字段 + `_trigger_halt` 都删

### 6.5 W3 execution_engine/recovery.py

- 删 import + `__init__` 参数 `kill_switch_sync`
- `_halt_for_recovery`：删 if/else，留 `self.kill_switch.halt(reason=reason)`

### 6.6 W4 startup_recovery.py

- 删 import + dataclass 字段 `kill_switch_sync`
- `_halt`：删 if/else，留 `self.kill_switch.halt(reason=reason)`

### 6.7 W5 reconciliation_system_queries.py

- 删 import
- 4 个写入点改为：

```python
# before (sync paths):
sync_service = getattr(self.owner.runtime, "kill_switch_sync_service", None)
if sync_service is not None:
    await sync_service.halt(reason=reason)
else:
    self.owner.runtime.kill_switch.halt(reason=reason)
# after:
await self.owner.runtime.kill_switch.halt_async(reason=reason)
```

### 6.8 50+ 个读路径

**完全不动**：`.halted` / `.status()` API 与之前 KillSwitch 完全等价。

### 6.9 测试文件

- `tests/unit/test_kill_switch_sync.py`：17 个测试，import 改为 `from aats.services.governance_engine.kill_switch import KillSwitch`，instance 改为 `KillSwitch()` + `await ks.bootstrap(...)`
- `tests/integration/test_kill_switch_cross_process.py`：4 个 testcontainers 测试，同样 import + instance 改写

### 6.10 docs

- `docs/operations/stage7_wsl2_realrun_runbook.md` 提到 KillSwitchSyncService 的段落标注 "Slice 6.4 已合并"

---

## 7. 测试矩阵

| 测试 | 文件 | 验证 |
|---|---|---|
| T1 零参构造 | unit | `KillSwitch()` 后 `.halt('x')` → `.halted == True` 且不抛 |
| T2 bootstrap hydration | unit | 预先 `await store.set(KEY, {halted:True, reason:'r'})`, 然后 `KillSwitch().bootstrap(...)` → `.halted == True` |
| T3 sync halt 跨进程 | unit | bootstrap 后 sync `halt('x')`, 等 1s, NATS 上有事件 + Redis 有写 |
| T4 worker thread halt | unit | 在 `asyncio.to_thread` 里调 `halt('x')`, await 后 NATS + Redis 都写了 |
| T5 dedup | unit | 连续 `halt('x')` 两次, 第二次只更新本地, NATS 只发一次 |
| T6 lopback filter | unit | 模拟收到 source_role == self 的 NATS event, 本地状态不变 |
| T7 stale set_at_ts | unit | 收到 set_at_ts 比 last_applied_ts 老的 event, 本地状态不变 |
| T8 redis 不可达 | unit | mock store.set 抛, halt 不抛, log warning |
| T9 nats 不可达 | unit | mock bus.publish 抛, halt 不抛, log warning |
| T10 stop 后调 halt | unit | `await ks.stop()` 后调 `halt('x')`, 本地仍然 halted |
| T11 cross-process integration | testcontainers | 真 redis + 真 nats, 两个 KillSwitch 实例, A halt → B 1s 内 halted |
| T12 cross-process restart | testcontainers | A halt → kill A → 新 A bootstrap → halted recovered |

---

## 8. 安全网与回滚

- **git tag**：`pre-stage6-slice6.4-v1` (3cc7bf7) 已经打好
- **回滚**：`git reset --hard pre-stage6-slice6.4-v1` 可秒回到 dual-class 状态
- **风险点**：W1-W5 修改后跑全量 unit 测试，如果有任何依赖 `kill_switch_sync` 字段的旧 mock 没改完，会立刻暴露

---

## 9. WSL2 4 进程真跑验证计划

1. WSL2 `~/aats` 同步到此 commit
2. `cd ~/aats/deploy/wsl2-dev && docker compose -f docker-compose.aats.yml down && docker compose -f docker-compose.aats.yml up -d --build`
3. 看 4 个进程的启动日志，每个都应该有 `kill_switch_bootstrap_*` log
4. `curl http://127.0.0.1:8000/operator/halt -d '{"reason":"slice6.4_test"}'`（或运行已有的 cross-process script）
5. 1s 后 4 个进程的 health check 都应该看到 `halted=True`
6. `curl http://127.0.0.1:8000/operator/resume`
7. 4 个进程应该看到 `halted=False`
8. `docker kill aats-execution-1 && sleep 2 && docker compose ... up -d aats-execution-1`
9. 重启后 execution 进程的初始 halt 状态应该匹配 redis 里最后一次 state

---

## 10. 工时与依赖

- 单 conversation 可完成（用户已批准跳过审批）
- 依赖 Slice 6.1（HotStateStore Redis backend）+ Slice 6.2 既有边车架构（hydrate / NATS subscribe / EventEnvelope schema 等）
- 不依赖 Slice 6.3 / Stage 7 / 8 / 9
