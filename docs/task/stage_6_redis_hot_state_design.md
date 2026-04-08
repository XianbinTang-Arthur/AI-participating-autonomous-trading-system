# Stage 6 设计文档：Redis 热状态存储真接入

> **状态**：设计草案，待用户批准 Slice 2 之后才进入业务路径
> **作者**：Claude（基于 Stage 5 完成 + Stage 7 故障演练 + 真跑环境调研综合）
> **日期**：2026-04-08
> **基线 commit**：当前 main HEAD（4 进程拓扑已绿、Stage 5 fan-out 已端到端验证）
> **关联 roadmap**：`docs/operations/multiprocess_refactor_roadmap.md` 节点 6.x，`docs/task/stage_5_design.md` §1.2 表（原计划 Redis 推迟到 Stage 8，本文档将该顺序提前到 Stage 6，原因见 §1.4）

---

## 1. 范围与目标

### 1.1 总目标

把 `aats/storage/hot_state_store.py` 这个**已成形但完全未接业务**的骨架，按"最小可观测改动"原则真接到 4 进程拓扑里的关键 cross-process 状态读路径，让 gateway / decision 进程能在不依赖 NATS event 重建本地缓存的前提下，**直接、同步、低延迟**地拿到 execution / market 进程的最新状态。

### 1.2 在 Stage 6 范围内（本文档承诺交付）

| Slice | 内容 | 风险 | 行为变化 |
|---|---|---|---|
| **6.1** | **基础设施 + settings 接线**：`pyproject.toml` 增 `redis = ["redis>=5,<6"]` 可选依赖；`AATSSettings` 加 `hot_state_backend` / `hot_state_redis_url` / `hot_state_global_prefix` 字段；`build_runtime` 在 hybrid backend 下根据 `hot_state_backend=redis` 调 `build_hot_state_store("redis", ...)` 并 await `connect()`；`runtime` 持有 `hot_state_store` 字段；shutdown 路径 await close()；新增集成测试用 docker compose 的 aats-redis 验证 connect/get/set/health_check round-trip。 | **极低**：纯加法，没有 production 路径调用 store；现有 monolith 默认仍是 `hot_state_backend=memory`；4 进程拓扑下 hot_state_store 装上但没人用。 | 零（runtime 多一个未使用的字段而已）。 |
| **6.2** | **kill_switch 写穿透 + 跨进程读**：execution / decision role 把 kill_switch 状态变化（halt / resume）write-through 到 Redis（key=`aats:hot:system:kill_switch`，无 TTL），gateway role 在 `/system/health` / `/system/blocker-control` 读路径上**优先**从 Redis 读，Redis 缺失或 stale 时回退到本地状态机。NATS 事件总线**继续广播**，Redis 只是给 gateway 一条更快的"同步问询"路径。 | **低**：write-through 失败 fall-back 到原状态机；read 路径有 fall-back 到本地态。但要小心：gateway 在故障演练里被 kill 之后，重启后**不能盲信 Redis**——必须先 reconcile 一次最新事件再相信缓存。 | 行为变化仅限于 gateway 看到 halt 状态的延迟从 "等下一条 NATS 心跳事件 ≤ 5s" 缩短到 "Redis ping 1ms"。逻辑上等价。 |
| **6.3** | **portfolio_snapshot 缓存层**：execution role 写 portfolio_snapshot 到 Postgres outbox 之后，**最佳努力地** mirror 到 Redis（key=`aats:hot:account:portfolio:{account_id}`，TTL=120s）。decision / gateway 在 OperatorQueryService.portfolio_overview / risk pre-check 路径上**优先**从 Redis 读，miss 时回退到 Postgres。 | **中**：portfolio 是决策依赖，stale data 会导致错误下单。必须严格做 invariant check：Redis 数据带 `as_of_ts`，consumer 比对本地 `expected_min_age`，**晚于阈值就拒绝用 Redis 返回 Postgres 直查**。 | 行为变化仅限于 Postgres 读次数下降；任何 Redis miss / stale 都会优雅 degrade 到原 Postgres 路径。 |

### 1.3 **不在** Stage 6 范围（明确推迟）

| 项 | 推迟到 | 理由 |
|---|---|---|
| obligation 表 / open_order 表 缓存 | Stage 6.4（独立后续 PR） | 这两张表的写路径比 portfolio 更复杂（per-order 增量），先把 6.3 的 write-through 框架打稳再扩展 |
| 跨进程分布式锁（Redlock） | 不做 | 现有 Postgres OCC + outbox 已经够强；Redis 锁是另一个故障域，价值不大风险不小 |
| Pub/Sub 通知（Redis SUB） | 不做 | NATS 已经是事件总线，Redis 只用作 KV cache，不引入第二条事件通道 |
| 全量 in-memory dict → Redis 替换 | 不做 | 太激进。每个 dict 单独评估"是否真的需要跨进程"，本 stage 只动 kill_switch + portfolio 两处刚需 |
| 多 Redis 实例 / 哨兵 / 集群 | 不做 | 当前单机 16GB WSL2 部署，单 Redis 实例 256MB max-memory + AOF 已经够用 |

### 1.4 为什么把 Redis 提前到 Stage 6（覆盖原 stage_5_design.md §1.2 表）

`stage_5_design.md` 写于 2026-04-07，把 Redis 推迟到 Stage 8 的理由是 "OTel 接线优先"。但 Stage 7 真跑之后**实证**发现：

1. **gateway role 大量数据是问 decision/execution role 的**（recovery_view / blocker_control / system_mode 全链路），Stage 7 已经在 `runtime_queries.py` 暴露过一次 `runtime.ai_service is None` 的 NPE（gap 6 / gap 7）。**根因不是装载缺失，是缺一个跨进程同步问询通道**。当前的 workaround 是 stub + None-guard，能跑但语义贫瘠。Redis hot state 是这个语义洞的"对的修复"。
2. **OTel 接线**主要解决"事后看链路"，对系统正确性影响小；Redis 接线解决"实时看状态"，对系统正确性影响大。**正确性优先于可观测性**是项目的明确原则（用户偏好"质量优先于速度"）。
3. Stage 6 三个 slice 加起来工作量 ≈ Stage 8 OTel 集成，但带来的真金白银收益更大。

因此本文档将 stage_5_design.md §1.2 表里的 Stage 6 / Stage 8 顺序对调，并在 §6 changelog 注明。

---

## 2. 前置事实摘要

### 2.1 hot_state_store 当前状态（已有代码）

- `aats/storage/hot_state_store.py` 共 367 行：
  - `HotStateStore` Protocol（async 接口）
  - `InMemoryHotStateStore`（dict + TTL，monolith 默认）
  - `RedisHotStateStore`（async redis-py，惰性 connect，JSON 编解码，PX 毫秒 TTL）
  - `RedisHotStateConfig`（dataclass：url / pool / global_prefix / health_check）
  - `build_hot_state_store(backend, redis_config)` 工厂
  - `make_key(namespace, *parts)` helper（前缀 `aats:hot:`）
  - 命名空间常量：`NS_MARKET` / `NS_ACCOUNT` / `NS_SYSTEM` / `NS_GATEWAY_HEARTBEAT`
- `tests/unit/test_hot_state_store.py` 共 277 行：单测覆盖 key 拼接、CRUD、TTL 过期、factory；**故意不做真 Redis I/O**（留给集成测试）。
- `pyproject.toml`：**未列 redis 依赖**（`RedisHotStateStore.connect()` 在缺包时会显式报错）。
- `aats/bootstrap/settings.py`：**未列 hot_state_* 字段**。
- `aats/bootstrap/config.py::build_runtime`：**未构造 hot_state_store**，runtime 无对应字段。
- `deploy/wsl2-dev/docker-compose.yml`：**已配置** `aats-redis`（redis:7-alpine、AOF、256MB max-memory、LRU eviction、healthcheck on 6379），4 进程已经在跟它共用同一个 docker network 但谁也没真用过它。

### 2.2 哪些状态"真的需要"跨进程同步读

通过 grep 真跑期间的 cross-process query 链路 + Stage 7 故障演练 gap 6/7 经验，确认以下三类状态是 cross-process 同步读的刚需（按 frequency × correctness criticality 排序）：

| 状态 | 写方 | 读方 | 现状 | 不接 Redis 的副作用 |
|---|---|---|---|---|
| `kill_switch.halted` | execution（reconciliation / risk）；gateway（operator manual halt） | gateway（health/blocker UI） / decision（pre-decision check） / execution（pre-submit check） | 各进程独立 in-memory；NATS 广播 `kill_switch_*` 事件后各方更新本地态 | gateway role 在 reconnect / 心跳间隙看到 stale 状态；故障演练 #2.2 那种重启场景下 RestartCount=1 之后第一次 health 查询可能反映上一次状态 |
| `portfolio_snapshot.latest[account_id]` | execution（reconciliation refresh / fill 之后） | decision（pre-decision risk pre-check）/ gateway（UI portfolio_overview） | Postgres `portfolio_snapshots` 表 + execution 进程内 `_latest_snapshots` dict | decision 每次 risk pre-check 都要打 Postgres（高频 SELECT，毫秒级延迟），4 进程同时 query 容易把 Postgres 顶到瓶颈 |
| `runtime_mode.snapshot()` | gateway（operator manual） / decision（profile rollback） / risk_engine（auto degrade） | gateway（mode endpoint）/ decision（pre-decision gate） / execution（pre-submit gate） | InMemoryEventBus 广播 + 各进程 RuntimeModeController in-memory dict | gateway 看到的 mode 与 decision 实际生效的 mode 在边界期不一致 |

`runtime_mode` 这一项**不在本 stage 实施**，因为它涉及 RuntimeModeController 的 immutable 语义（mode 切换走完整 outbox），改造成本远大于 kill_switch 和 portfolio_snapshot。

### 2.3 没在原 stage_5_design 文档里列的依赖关系

- 4 进程都已能 ping `aats-redis:6379`（同一个 docker network `aats-dev_aats`）
- 没有任何 `redis-py` import 在 production 代码（grep 验证）
- `aats/bootstrap/process_lifecycle.py` 已经统一管理 startup/shutdown 钩子，hot_state_store.connect/close 可以挂到这条钩子上
- monolith 模式（默认 `event_bus_backend=in_memory`）下 Redis 应该**完全不被启用**，确保 Stage 6 不影响 monolith 用户

---

## 3. Slice 6.1 详细设计（settings + bootstrap + 集成测试）

### 3.1 改动文件清单

- `pyproject.toml`：新增可选依赖组 `redis`
- `aats/bootstrap/settings.py`：加 3 个字段（`hot_state_backend` / `hot_state_redis_url` / `hot_state_global_prefix`）
- `aats/bootstrap/config.py::build_runtime`：在 shared slice 内根据 settings 构造 hot_state_store；加到 `_RuntimeSlices` 和 `ApplicationRuntime`
- `aats/bootstrap/process_lifecycle.py` 或 `_apply_post_init_guards`：startup 时 await `hot_state_store.connect()`（仅 redis backend）
- `apps/*` 4 个 main.py：shutdown 时 await `runtime.hot_state_store.close()`（如果是 redis backend）
- `tests/unit/test_settings_hot_state.py`：新增，覆盖 settings 字段解析、env 变量加载、validator
- `tests/integration/test_hot_state_redis_roundtrip.py`：新增（仅在 `AATS_RUN_REDIS_INTEGRATION=1` 时跑），用 testcontainers 拉真 Redis 跑 connect/set/get/expire/close 端到端
- `deploy/wsl2-dev/docker-compose.aats.yml`：4 个 service 的 environment 段加 `AATS_HOT_STATE_BACKEND: redis` + `AATS_HOT_STATE_REDIS_URL: redis://redis:6379/0`
- `deploy/wsl2-dev/Dockerfile`：在 pip install 行加 `aats[nats,redis]` 让基础镜像含 redis-py
- `docs/operations/stage7_wsl2_realrun_runbook.md`：新增 §10 Stage 6 验证记录

### 3.2 settings.py 字段定义

```python
# ── Stage 6：跨进程热状态存储（HotStateStore）─────────────────
# monolith 默认 in_memory，零外部依赖；4 进程拓扑应设为 redis，让 gateway
# / decision / execution 共享同一份 kill_switch / portfolio 缓存。
hot_state_backend: Literal["memory", "redis"] = Field(
    default="memory",
    description="HotStateStore backend: memory (single-proc) | redis (multi-proc).",
)
hot_state_redis_url: str = Field(
    default="redis://127.0.0.1:6379/0",
    description="Redis URL. Only used when hot_state_backend=redis.",
)
hot_state_global_prefix: str = Field(
    default="",
    description="Global key prefix for multi-env Redis sharing (e.g. 'dev:' / 'prod:').",
)
```

加 validator：`hot_state_backend == "redis"` 时校验 url 非空且以 `redis://` 或 `rediss://` 开头。

### 3.3 build_runtime 构造逻辑

在 `_build_shared_runtime_slice` 内（与 `bus`、`event_store` 同层），根据 settings 构造 store。**不在此处 connect**——connect 是 I/O，要放到 startup hook 里：

```python
# _build_shared_runtime_slice
hot_state_store = build_hot_state_store(
    backend=settings.hot_state_backend,
    redis_config=RedisHotStateConfig(
        url=settings.hot_state_redis_url,
        global_prefix=settings.hot_state_global_prefix,
    ) if settings.hot_state_backend == "redis" else None,
)
slices.hot_state_store = hot_state_store
```

`ApplicationRuntime` 加字段：

```python
hot_state_store: HotStateStore  # always present, may be in-memory in monolith
```

### 3.4 startup / shutdown 钩子

参考 `aats/bus/nats_bus.py` 的 connect / close 在 `process_lifecycle.py` 里的对接方式，在同一个位置加 `hot_state_store.connect()` / `hot_state_store.close()`。具体调用点 6.1 实施时再 grep 确定，避免文档过早描述。

### 3.5 集成测试

```python
# tests/integration/test_hot_state_redis_roundtrip.py

@pytest.mark.skipif(
    os.getenv("AATS_RUN_REDIS_INTEGRATION") != "1",
    reason="opt-in: testcontainers redis is heavy",
)
class TestRedisHotStateStoreRoundTrip(unittest.IsolatedAsyncioTestCase):
    async def test_connect_set_get_expire_close(self) -> None:
        with RedisContainer("redis:7-alpine") as redis_container:
            url = f"redis://{redis_container.get_container_host_ip()}:{redis_container.get_exposed_port(6379)}/0"
            store = RedisHotStateStore(RedisHotStateConfig(url=url))
            await store.connect()
            try:
                key = make_key("system", "kill_switch")
                await store.set(key, {"halted": True, "reason": "test"}, ttl_seconds=60)
                value = await store.get(key)
                self.assertEqual(value, {"halted": True, "reason": "test"})
                self.assertTrue(await store.exists(key))
                self.assertTrue(await store.health_check())
            finally:
                await store.close()
```

`pyproject.toml` 加 `redis-integration` optional 组：

```toml
redis-integration = [
  "redis>=5,<6",
  "testcontainers>=4.0",
]
```

### 3.6 单元测试

```python
# tests/unit/test_settings_hot_state.py

class TestHotStateSettings(unittest.TestCase):
    def test_default_is_memory_backend(self):
        settings = AATSSettings()
        self.assertEqual(settings.hot_state_backend, "memory")

    def test_redis_backend_requires_valid_url(self):
        with self.assertRaises(ValidationError):
            AATSSettings(hot_state_backend="redis", hot_state_redis_url="not-a-url")

    def test_global_prefix_default_empty(self):
        settings = AATSSettings()
        self.assertEqual(settings.hot_state_global_prefix, "")

    def test_env_var_loading(self):
        # AATS_HOT_STATE_BACKEND / AATS_HOT_STATE_REDIS_URL / AATS_HOT_STATE_GLOBAL_PREFIX
        # 都遵循 pydantic-settings 的 env 加载机制
        ...
```

### 3.7 Slice 6.1 行为变化矩阵

| 场景 | Before Slice 6.1 | After Slice 6.1 |
|---|---|---|
| monolith 启动 | `runtime` 无 hot_state_store 字段 | `runtime.hot_state_store` 是 InMemoryHotStateStore（**未被任何业务路径调用**） |
| 4 进程拓扑启动（hot_state_backend=memory，默认） | 同上 | 同上（每个进程一个独立 in-memory dict，但**没有谁会往里写**） |
| 4 进程拓扑启动（hot_state_backend=redis，via env var） | startup 不调 redis | startup 调 `await store.connect()`，ping Redis；shutdown 调 `await store.close()` |
| Redis 容器 down 但 backend=redis | 启动直接 NPE 或忽略 | startup 阶段 ping 失败 → 抛 RuntimeError → 4 进程启动失败（**fail-fast 是设计意图**：production 配 backend=redis 就必须保证 redis 可达） |
| 任何业务路径 | 没人调 hot_state_store | 没人调 hot_state_store（**Slice 6.1 的关键点：纯加法**） |

### 3.8 Slice 6.1 完成判定

1. `pyproject.toml` 有 `redis` 和 `redis-integration` 两个 optional 组
2. `tests/unit/test_settings_hot_state.py` 全绿
3. `AATS_RUN_REDIS_INTEGRATION=1 python -m pytest tests/integration/test_hot_state_redis_roundtrip.py` 全绿
4. `docker compose -f docker-compose.aats.yml --env-file .env.wsl2 up -d --force-recreate` 后 4 容器 healthy
5. 4 进程启动日志里能看到 `hot_state_store_connected backend=redis url=redis://redis:6379/0`
6. 现有 1232 单测 + 26 集成测试零回归
7. monolith 仍能 `python -m apps.api_gateway.main` 起来（默认 backend=memory，不依赖 redis）

### 3.9 Slice 6.1 回滚

- 如果 6.1 跑出问题：删除 settings.py 三个字段、删除 build_runtime 那两行、删除 4 个 main.py 的 close 调用、改回 docker-compose 不加 hot_state env var
- 因为 6.1 是纯加法，**回滚不会影响任何 production 路径**

---

## 4. Slice 6.2 详细设计（kill_switch 跨进程接 Redis）

> ⚠️ 本 slice **不在本次提交**实施。本节先把设计写出来供评审，6.1 验收之后单独 PR。

### 4.1 改动思路

1. 在 `aats/services/governance_engine/kill_switch.py::KillSwitch` 内加 `_hot_state_store: HotStateStore | None` 字段，由 `build_runtime` 注入。
2. `KillSwitch.halt()` / `resume()` 在改完 in-memory state 之后 best-effort 写 Redis：
   ```python
   if self._hot_state_store is not None:
       try:
           await self._hot_state_store.set(
               make_key(NS_SYSTEM, "kill_switch"),
               {"halted": self._state[0], "reason": self._state[1], "as_of_ts": time.time()},
               ttl_seconds=None,  # 永不过期，靠主动写覆盖
           )
       except Exception as exc:
           log_event(self.logger, "hot_state_kill_switch_write_failed", level="warning", error=str(exc))
   ```
3. `KillSwitch.snapshot()` 加一个 `prefer_hot_state: bool = False` 参数，gateway role 调用时传 True，会**先**问 Redis，miss 或异常时 fall-back 到 in-memory。
4. gateway 的 `runtime_queries.py::kill_switch_state` / `system_blocker.py` / `health_service.py` 在读 kill_switch 状态时改用 `runtime.kill_switch.snapshot(prefer_hot_state=True)`。

### 4.2 不变量

- `kill_switch._state`（in-memory tuple）**永远是本进程内的真相**；Redis 只是给跨进程读快照
- write-through 失败**不阻塞**业务（best-effort），write-through 成功则 fast-path 生效
- 重启进程时**不**从 Redis 重建 kill_switch 状态——让 NATS 事件流重放是源头真相，Redis 只是缓存

### 4.3 风险

- gateway 重启的瞬间问 Redis 拿到一个 stale 值，反而误导 UI——**缓解**：snapshot 函数在比较 `as_of_ts` 时如果 > N 秒（默认 60s）就拒绝 hot state 数据
- write-through 的 race：execution role 同一秒内连续 halt + resume，Redis 顺序可能跟 in-memory 不一致——**缓解**：halt/resume 都带 timestamp，Redis 只接受 timestamp 单调递增的写

### 4.4 测试

- 单元测试：fake hot_state_store 注入，断言 halt/resume 触发了 set 调用且 payload 正确
- 集成测试：4 进程拓扑下 gateway POST `/system/halt` → 立刻 GET `/system/health` → 应当看到 `halted=True`，无需等待 NATS 事件
- 故障演练：杀 Redis 容器，验证 4 进程 health 不退化（只是 hot path 退到 fall-back）

---

## 5. Slice 6.3 详细设计（portfolio_snapshot 缓存）

> ⚠️ 本 slice **不在本次提交**实施。本节先把设计写出来供评审，6.2 验收之后单独 PR。

### 5.1 改动思路

1. `aats/services/portfolio_service/positions.py::PortfolioService.publish_snapshot` 写完 Postgres 之后 best-effort 写 Redis：
   ```python
   await self._hot_state_store.set(
       make_key(NS_ACCOUNT, "portfolio", account_id),
       {"snapshot_id": ..., "as_of_ts": ..., "positions": ..., "equity": ...},
       ttl_seconds=120,  # stale 阈值；超过 2 分钟必须查 Postgres
   )
   ```
2. `aats/services/operator/query_service.py::OperatorQueryService.portfolio_overview` 优先问 Redis：
   ```python
   if self._hot_state_store is not None:
       try:
           cached = await self._hot_state_store.get(make_key(NS_ACCOUNT, "portfolio", account_id))
           if cached and time.time() - cached["as_of_ts"] < 30:
               return PortfolioOverview.model_validate(cached)
       except Exception:
           pass  # fall-through to postgres
   # postgres path
   ...
   ```
3. decision_engine 的 risk pre-check 同样改成"先问 Redis，后回退 Postgres"。

### 5.2 不变量

- Postgres 永远是 source of truth；Redis 是 stale-tolerant cache
- 任何 Redis hit 都必须验证 `as_of_ts` 在合理范围内（默认 30s），否则视为 miss
- write 失败不阻塞业务

### 5.3 风险

- 高风险点：decision 拿到 stale portfolio → 错误下单。**缓解**：30s threshold 比"过期"更紧，且 risk pre-check 会再做一次 Postgres 严格 cross-check
- Redis 内存压力：每个 account_id 一个 portfolio，TTL 120s，按当前 1 个 account 估 < 10KB，128 MB Redis 远远够用
- 双写不一致：Postgres 已 commit 但 Redis 写失败 → 下次 Redis miss 会触发 Postgres 直查，自然恢复

### 5.4 测试

- 单元：mock hot_state_store，断言 publish_snapshot 后 set 被调用
- 集成：4 进程拓扑下 execution publish snapshot → decision 在 100ms 内通过 Redis 读到（无需走 Postgres SELECT）
- 故障演练：让 Redis 内存写满触发 LRU eviction，验证 fall-back 路径正常

---

## 6. 完成判定

每个 slice 单独完成判定。本文档承诺 Slice 6.1 全部条件达标后才进入 6.2 PR。Stage 6 整体收尾要求：

1. 4 进程拓扑下，执行 Slice 6.1 + 6.2 + 6.3 全部完成
2. 单元测试 + 集成测试零回归
3. 真跑环境下：
   - gateway 调 `/system/halt` 后立即 GET `/system/health` 返回 `halted=true`，**无 NATS 事件等待**
   - decision risk pre-check 看到的 portfolio 与 execution 写入时间差 < 100ms（log 验证）
   - 杀 Redis 容器后 4 进程**继续工作**，仅 hot path 退到 fall-back，无 5xx
   - 4 容器（market/decision/execution/gateway）的 startup log 显示 `hot_state_store_connected backend=redis`
4. `docs/operations/stage7_wsl2_realrun_runbook.md` 增加 §10 Stage 6 验证记录段

---

## 7. 回滚

- **Slice 6.1 回滚**：删除 settings 字段 + build_runtime 调用 + docker-compose env var → 完全回到当前状态
- **Slice 6.2 回滚**：把 KillSwitch 内 hot_state_store 字段设为 None，gateway 仍然走 in-memory snapshot
- **Slice 6.3 回滚**：把 portfolio_overview 内的 Redis 优先路径改成直接走 Postgres
- 所有 slice 都设计成"加法 + fall-back"，因此**任何一个回滚都不会破坏业务正确性**，只会损失性能优化收益

---

## 8. Changelog

- 2026-04-08：首版。基于 Stage 5 完成 + Stage 7 真跑环境调研写出 3 slice 设计。明确把 stage_5_design.md §1.2 表里 Redis (Stage 8) / OTel (Stage 6) 的顺序对调，理由：Redis 修的是正确性问题（cross-process 状态同步），OTel 修的是可观测性问题，正确性优先。本次 commit 只交付 Slice 6.1（settings + 接线 + 集成测试，纯加法零行为变化），6.2 / 6.3 等 6.1 验收后再单独 PR。
