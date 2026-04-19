# Stage 6 Slice 6.3 设计文档：portfolio_snapshot 跨进程缓存接 hot_state_store

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


> 状态：**待审批**
> 前置：Slice 6.1（HotStateStore Redis 配线）✅、Slice 6.2（kill_switch 跨进程同步）✅
> 后续：Slice 6.4（KillSwitch + KillSwitchSyncService 二合一去过渡 API，可选）

---

## 1. 问题陈述

### 1.1 现状

4 进程拓扑下的 portfolio_snapshot 读写路径：

**写**：execution 进程的 `PostgresPortfolioOutboxPublisher.persist_fill_projection` 在 SQLAlchemy 事务里：
1. `portfolio_repo.save_snapshot_in_session(...)` 写 `portfolio_snapshots` 表
2. `fill_outcome_repo.save_outcome_in_session(...)` 写 `fill_outcomes` 表
3. 两条 envelope（balance_delta + portfolio_snapshot）进 event_store + outbox
4. commit
5. 异步 `flush_pending` → `bus.publish_envelope` → NATS `portfolio.snapshots` topic

**读**：所有 sync caller funnel 到一个入口：

```python
# aats/services/operator/query_service.py:957
def _latest_scoped_snapshot(self):
    return self._cached(
        "latest_scoped_snapshot",
        lambda: latest_snapshot_for_scope(self.runtime.portfolio_repo, self.state_scope),
    )
```

→ `latest_snapshot_for_scope` → `PostgresPortfolioRepository.latest_for_scope` → `SELECT ... ORDER BY sequence_id DESC LIMIT 1` 加 `WHERE product_type / margin_mode`。

`_latest_scoped_snapshot` 有 8 处 caller（query_service 自己 4 处、account_queries.py 4 处、runtime_queries.py 1 处），全都是 sync method，全都经过 per-request `_cached`。

### 1.2 这不是 safety bug — 是性能优化

**关键澄清**（与 Slice 6.2 的 kill_switch 不同）：

| 维度 | Slice 6.2 kill_switch | Slice 6.3 portfolio_snapshot |
|---|---|---|
| 跨进程一致性 | **缺失**——4 个进程各自一份 in-memory state，halt 不传播 | **已经 work**——4 进程共享同一台 Postgres，gateway 直接 SELECT 能读到 execution 写的最新 snapshot |
| failure mode | gateway 喊 halt，execution 继续下单（资金风险） | dashboard 打 Postgres，慢，没有数据正确性问题 |
| 改造动机 | 修真实的资金安全 bug | 减少 dashboard 高频 SELECT 对 Postgres 的负载 |
| 复杂度 | 高（需要跨进程同步语义 + 6 个不变量 + 5 处 writer 改造） | **中**（cache 层 + miss fallback Postgres） |

**Slice 6.3 不能引入新的"正确性可能退化"的路径**——cache 是优化层，任何环节失败都必须能 fallback 到 Postgres source of truth。

**6.3 cache 的覆盖范围**（精确化，详见决策 D9）：

- 仅覆盖 `OperatorQueryService._latest_scoped_snapshot` 这一条 dashboard / operator API 入口
- **不覆盖** strategy coordinator / decision context_builder / execution recovery / reconciliation repair / startup recovery 等 production 路径
- 这些 production 路径全部直接调 `latest_snapshot_for_scope(portfolio_repo, scope)` helper（不经过 query_service），**cache 完全不干涉交易决策**

### 1.3 为什么不"什么也不做"

dashboard endpoint 在 4 进程拓扑下被 UI 自动 polling 调用（典型几秒一次）。`/system/health`、`/portfolio/latest`、`/account/positions`、`/account/balances`、`/account/state` 等端点全部经过 `_latest_scoped_snapshot`，即便 per-request `_cached` 内做了去重，每次 HTTP 请求仍然至少 1 次 Postgres SELECT。

随着 symbols 扩张和 4 进程拓扑下 gateway 成为唯一 dashboard 入口，Postgres SELECT 的 QPS 会线性增长。**预防性接 cache 比上线后再补便宜**——cache miss 兜底 Postgres，引入零正确性风险。

更关键的是：**6.3 是 hot_state_store 抽象层第一次被业务 caller 真正消费**。Slice 6.1 把 Redis 接进来零 caller，Slice 6.2 接的是 kill_switch（governance 层）。Slice 6.3 之后，当未来需要新增"跨进程共享缓存"的状态时（市场快照、风控限额等），cache 模式已经在生产线上验证。

---

## 2. Slice 6.3 目标

让 4 进程拓扑下的 portfolio_snapshot 缓存满足：

1. **写入即可见（本地 sync 路径）**：execution 进程内 `outbox publisher` commit 之后，本进程的 `portfolio_repo` 立即可见（这是现状，不动）。
2. **跨进程异步同步（≤1s）**：execution 进程写完 → gateway/decision/market 进程的 in-memory cache 在 ≤1s 内被 NATS 广播刷新（PORTFOLIO_SNAPSHOTS topic 已在 critical 集合，本 slice 不动 NATS 路由）。
3. **重启后恢复 cache（bootstrap）**：进程崩溃 + restart 之后，新进程 bootstrap 从 Redis 读最近一份 snapshot，cache 立即就绪，不必等下一次 fill。
4. **三重 fallback**：cache miss / Redis 不可达 / NATS 不可达，任何环节失败都不能让 dashboard 变 500，全部 fallback 到现有 portfolio_repo（Postgres）路径。

**不在本 slice 范围**：
- ai_service / recovery_posture cache（更后的 slice）
- portfolio history list 缓存（cache 只覆盖 latest，history 仍直接打 Postgres）
- balance_deltas / fill_events cache
- 替换或重写 outbox publisher 事务语义（commit 路径不动）
- 修改 NATS topic 集合（PORTFOLIO_SNAPSHOTS 已就绪，**不会触发 §10.5 部署纪律**）

---

## 3. 现有调用点盘点

### 3.1 写入路径（2 处）

| # | 调用点 | 文件:行 | 调用上下文 | 6.3 改造 |
|---|---|---|---|---|
| W1 | `PostgresPortfolioOutboxPublisher.persist_fill_projection` | `aats/services/portfolio_service/outbox.py:66` | async；外层 `await asyncio.to_thread(_persist_fill_projection_sync)`；commit 之后异步 `flush_pending` | commit 后 `await self._cache_publish(snapshot)` (best-effort) |
| W2 | `PostgresPortfolioOutboxPublisher.persist_bootstrap_snapshot` | `aats/services/portfolio_service/outbox.py:36` | async；进程启动期一次性 | 同上 |

**关键**：cache publish 在 `to_thread` commit **回到 async 上下文之后**调，这样 cache.publish 是 async 直接 await，不需要 sync→async 桥接。

### 3.2 读取路径（8 处 sync caller）

所有 caller 全部 funnel 到 `OperatorQueryService._latest_scoped_snapshot`：

| # | 调用点 | 文件:行 | 用途 |
|---|---|---|---|
| R1 | `query_service._phase5_balance_view` | query_service.py:1003 | 余额视图（非 phase5 路径） |
| R2 | `query_service._build_blocker_control` | query_service.py:1102 | blocker control 渲染 |
| R3 | `query_service._aggregate_local_positions / _local_position_margin_summary` | query_service.py:1985 | 本地仓位聚合 |
| R4 | `query_service._account_pnl_overview` | query_service.py:7329 | PnL 总览 |
| R5 | `account_queries.build_portfolio_latest` | account_queries.py:25 | `/portfolio/latest` 端点 |
| R6 | `account_queries.balances` | account_queries.py:40 | `/account/balances` 端点 |
| R7 | `account_queries.positions` | account_queries.py:50 | `/account/positions` 端点 |
| R8 | `account_queries.build_account_state` | account_queries.py:76 | `/account/state` 端点 |
| R9 | `runtime_queries.build_system_health` | runtime_queries.py:296 | `/system/health` 端点 |

**Slice 6.3 的关键约束：所有 sync caller 不动**。`_latest_scoped_snapshot` 内部加 cache 优先逻辑，sync 签名保持不变。

理由：
- 8 处全 sync，把它们改成 async 会传染到上层 facade 层 → traversal 上百处 caller
- 6.3 的目标是"减少 Postgres 打"，不是"sync→async 全线重构"
- cache + fallback 模式让 sync caller 享受到 cache 优势（hit 时零 I/O），miss 时退化到原有路径

---

## 4. 设计

### 4.1 三层架构

```
┌──────────────────────────────────────────────────────────────────┐
│  PortfolioRepository (Postgres / InMemory, existing)              │
│  - source of truth                                                │
│  - portfolio_snapshots 表                                         │
│  - sync API：latest_for_scope / history / save_snapshot           │
│  - 6.3 改造：完全不动                                              │
└────────────────────┬─────────────────────────────────────────────┘
                     │ fallback when cache miss
                     ↑
┌────────────────────┴─────────────────────────────────────────────┐
│  PortfolioSnapshotCache (NEW)                                    │
│  - in-memory dict[scope_fingerprint, PortfolioSnapshot]          │
│  - 与 KillSwitchSyncService 同形态：sidecar + 两条数据通路        │
│  - 写：execution 进程的 outbox commit hook 调 publish()           │
│  - 读：sync get() 返回当前 dict 内容                              │
│  - bootstrap：从 Redis hydrate 一次（任意进程都可以）              │
│  - 订阅 NATS PORTFOLIO_SNAPSHOTS：每次收到 envelope 更新本地 dict  │
└──────────┬─────────────────────┬─────────────────────────────────┘
           │ best-effort          │ best-effort
           ↓                     ↓
┌──────────┴────┐    ┌───────────┴─────────────────────────────────┐
│  HotStateStore │    │  EventBus (HybridEventBus)                  │
│  Redis         │    │  NATS topic: portfolio.snapshots            │
│  key:          │    │  - 已在 DEFAULT_CRITICAL_TOPICS             │
│  aats:hot:     │    │  - 跨进程实时广播                            │
│  portfolio:    │    │  - 4 进程都已 subscribe-able                │
│  latest:<scope>│    │                                             │
│  - 持久化最近   │    │                                             │
│    快照         │    │                                             │
│  - bootstrap    │    │                                             │
│    时读一次     │    │                                             │
└────────────────┘    └─────────────────────────────────────────────┘
```

### 4.2 关键决策

**决策 D1：双通路 cache（NATS push 实时 + Redis bootstrap hydrate）**

- **execution 写**：commit 之后 best-effort 双写：Redis SET（持久化）+ NATS publish 已经由 outbox publisher 自然完成（PORTFOLIO_SNAPSHOTS 已在 critical topics），cache 只额外补一次 Redis SET
- **gateway/decision/market 读**：
  - 启动 bootstrap：await Redis GET 一次 → 解析 → 写本地 dict
  - 运行时：subscribe NATS PORTFOLIO_SNAPSHOTS topic → handler 解析 envelope → 更新本地 dict
  - sync caller：直接读本地 dict；miss → fallback 当前 portfolio_repo 路径

**为什么双通路**：
- **NATS 路径**：实时性好（< 50ms 与 6.2 同 budget）；但启动后需要等下一次 fill 才会有数据
- **Redis 路径**：bootstrap 时一次性 hydrate；但运行时 polling 浪费 QPS
- **二者互补**：bootstrap 用 Redis 立即可用，运行时用 NATS 实时刷新，**没有 background polling task**

**决策 D2：完全不动 sync caller**

`_latest_scoped_snapshot` 内部改造：

```python
def _latest_scoped_snapshot(self):
    return self._cached(
        "latest_scoped_snapshot",
        self._latest_scoped_snapshot_uncached,
    )

def _latest_scoped_snapshot_uncached(self):
    cache = getattr(self.runtime, "portfolio_snapshot_cache", None)
    if cache is not None:
        cached = cache.get_sync(self.state_scope)
        if cached is not None:
            return cached
    return latest_snapshot_for_scope(self.runtime.portfolio_repo, self.state_scope)
```

8 处 caller 零修改。

**决策 D3：Redis key 设计 & scope 隔离**

- key: `aats:hot:portfolio:latest:{scope_fingerprint}`
- scope_fingerprint: `f"{product_type}:{margin_mode}:{primary_symbol}"`，与 Postgres 表的 scope 列保持一致
- 多 scope 共存：cache 本地 dict 用 scope_fingerprint 做 key，理论上一个 cache 实例可以装多个 scope（虽然 4 进程拓扑下每个进程只有一个 active scope）
- **不设 TTL**：与 kill_switch 同模式。snapshot 是覆盖式更新，没有自然过期。设了反而 dashboard 偶发 miss → 增加 fallback 开销

**决策 D4：Redis value 序列化**

- 写：`snapshot.model_dump(mode="json")` → JSON-friendly dict（Decimal → str）→ `hot_state_store.set` 内部 `json.dumps`
- 读：`hot_state_store.get` 返回 dict → `PortfolioSnapshot.model_validate(dict)` 重建 Decimal
- caller 拿到的仍是 `PortfolioSnapshot` 实例，类型一致

**决策 D5：写路径 hook 在 outbox publisher 的 async 包装层**

- `persist_fill_projection` (async) commit 之后调 `await self.snapshot_cache.publish(snapshot)`
- `persist_bootstrap_snapshot` (async) 同样调
- best-effort：cache.publish 内部 try/except 包住，Redis 写失败不抛

⚠️ **精确定义**：`cache.publish(snapshot)` 内部做两件事：
1. **同步更新本地 in-memory dict**（execution 进程自己 dashboard 拉取时立即受益，不必 fallback PG）
2. **best-effort 写 Redis**（持久化，给其他进程 bootstrap hydrate 用）

**不广播 NATS**——NATS 广播由 outbox publisher 现有的 `flush_pending` 流程负责（PORTFOLIO_SNAPSHOTS envelope 已经进 event_store + outbox）。这避免了重复广播 + 顺序竞态。

为什么必须同步更新本地 dict？满足 D8 的"4 个 role 统一装 cache"语义：execution 进程自己也是 cache 消费者（自己的 dashboard 拉取走 cache 路径），不能等 NATS 绕一圈回来才拿到刚 commit 的 snapshot。

cache 在 4 进程拓扑下的 NATS 路径：
- execution 进程的 outbox publish → NATS broker → PORTFOLIO_SNAPSHOTS topic
- gateway/decision/market 进程的 cache subscriber 收到 → 更新本地 dict
- execution 进程**也订阅** PORTFOLIO_SNAPSHOTS（fanout）→ 自己收到自己广播 → 用 D6 的 `snapshot_ts <= 本地` idempotent 比较自然 noop（本地 dict 已经在 publish 同步阶段被更新，远端事件 ts 必然 == 本地 ts）

**决策 D6：用 `snapshot_ts` 做 idempotent 比较（不依赖 source_role 跳过）**

snapshot 自带 `snapshot_ts` (datetime)。`PortfolioSnapshotCache._handle_remote_event` 比较远端 `snapshot_ts` 与本地 dict 内同 scope 的 `snapshot_ts`：
- 远端 `snapshot_ts > 本地` → 用远端覆盖本地
- 远端 `snapshot_ts <= 本地` → noop（**这一条机制同时覆盖三个场景**）

三个 noop 场景：
1. **execution 进程自己收到自己广播**：D8 要求所有 role 都订阅 PORTFOLIO_SNAPSHOTS，execution 自己 publish 之后会通过 NATS 收到自己的事件——本地 dict 已经在 commit hook 同步阶段被更新过了，远端事件的 ts 必然 == 本地 ts，noop
2. **乱序 / 重投事件**：旧 snapshot_ts 自然 < 本地，丢弃
3. **同毫秒时间戳**（罕见 corner case）：execution 是单一 writer + 单线程 commit，同毫秒内两次 commit 概率极低；即便发生，两个 snapshot 反映的是同一时刻相邻状态，noop 或覆盖都不影响正确性

注：与 6.2 用 `time.time()` set_at_ts 不同，6.3 用业务字段 `snapshot_ts`，因为：
- snapshot 自带这个字段，无需额外塞进 envelope
- snapshot_ts 是 outbox publisher commit 时刻（utc_now()），单调性由"execution 进程是唯一 writer"保证
- 即便有 NATS 重投或乱序，snapshot_ts 是稳定排序键
- **不需要 EventEnvelope.source_role 字段**，对 envelope schema 零依赖

**决策 D7：bootstrap 不阻断 build_runtime**

与 6.2 同：cache.bootstrap() 内部所有失败（Redis 不可达 / parse 失败 / NATS subscribe 失败）都 try/except 吞掉 + log warning。build_runtime 不会因为 cache 启动失败而 fail。

**决策 D8：execution 进程也维护 cache（统一行为）**

不是只有 gateway 进程才装 cache。**4 个 process_role 都装 cache**：
- execution：写完调 cache.publish + 自己的本地 dict 也立即更新（避免本进程 dashboard 拉时还要 fallback）
- gateway/decision/market：通过 NATS 广播被动更新

这样 cache 类不需要按 process_role 分支判断，统一行为。代码更简单。

**决策 D9：cache 注入点严格限定在 `query_service._latest_scoped_snapshot`，不污染 production 路径**

**已 grep 验证（2026-04-08）**：

- `_latest_scoped_snapshot` 共 **10 处 caller**，**全部位于 `OperatorQueryService` 子类**（query_service / account_queries / runtime_queries），**全部是 dashboard / operator API 路径**
- `latest_snapshot_for_scope` helper 直接被以下 **production 路径**调用，**全部绕过 query_service**：

| 文件:行 | 上下文 |
|---|---|
| `strategy_engines/coordinator.py:850` | `_latest_portfolio_snapshot()` 内部，strategy coordinator 直接打 portfolio_repo |
| `decision_engine/context_builder.py:97-98` | **决策上下文构建**，用 `snapshots_for_scope` 拿 history list 再 `latest_matching_snapshot` 取最新 |
| `execution_engine/recovery.py:109/242/312` | execution 启动期 recovery |
| `recovery_control/startup_recovery.py:483` | 进程启动 recovery |
| `reconciliation_service/repair.py:105/219/268` | reconciliation 修复路径 |
| `reconciliation_service/replay.py:337` | reconciliation 回放路径 |

这些 production caller 全部直接调 `repo.latest_for_scope` / `repo.history_for_scope`，**完全不经过 cache**。

**6.3 cache 注入策略**：

- **只改** `OperatorQueryService._latest_scoped_snapshot` 内部（query_service.py:957），在 `_cached` lambda 内加 cache 优先逻辑
- **不 wrap** `PortfolioRepository`
- **不修改** `latest_snapshot_for_scope` helper
- **不 monkey-patch** 任何 portfolio_repo 实例

**结果**：

- ✅ 6.3 cache **完全不影响交易决策路径**（context_builder / coordinator / risk / recovery / reconciliation 全部直接打 PG，cache 不干涉）
- ✅ 性能优化定性 **100% 成立**：cache 失败任何环节，dashboard 自动 fallback PG，production 路径根本不知道 cache 存在
- ✅ 6.3 真实价值更精确：覆盖 dashboard polling 链路 + 验证 hot_state_store 抽象层 + 跨进程数据流 + 模式化样板，为后续 slice 扩展铺路

**未来扩展**（不在 6.3 范围）：

- 如果发现 strategy coordinator / decision context_builder 也需要 cache 优化，**单独的后续 slice** 评估 freshness budget 后再加（freshness 严格 = 拒绝 ts > N 秒的 cache 内容），**不在 6.3 一起做**

### 4.3 数据格式

**Redis key**：`aats:hot:portfolio:latest:derivatives:isolated:BTC-USDT`（举例）

**Redis value**（JSON，PortfolioSnapshot.model_dump(mode="json") 完整 dump）：
```json
{
  "decision_id": "decision-2026-04-08-1234",
  "snapshot_ts": "2026-04-08T11:56:28.965+00:00",
  "balances": {"USDT": "12345.67"},
  "positions": [...],
  "total_equity": "12345.67",
  ...
}
```

**NATS topic**：`portfolio.snapshots`（**已存在**，不动）
**NATS subject**：`aats.portfolio.snapshots`
**NATS payload**：现有 `EventEnvelope`，event_type=`PortfolioSnapshotPublished`（不变），cache subscriber 解析 envelope.payload 得到 PortfolioSnapshot

**新增到 `DEFAULT_CRITICAL_TOPICS`**：**不需要**，PORTFOLIO_SNAPSHOTS 已在。

### 4.4 启动 bootstrap 顺序

`build_runtime` 当前顺序（Slice 6.2 之后）：

1. settings 校验
2. database_runtime 初始化
3. hot_state_store.connect()（Slice 6.1）
4. event_store / bus 构造 + `_start_event_bus`
5. **`KillSwitchSyncService` 构造 + bootstrap**（Slice 6.2，subscribe NATS KILL_SWITCH_STATE）
6. **`PortfolioSnapshotCache` 构造 + bootstrap**（Slice 6.3 NEW，subscribe NATS PORTFOLIO_SNAPSHOTS）
7. `_build_*_slice` 6 个 builder
8. `_build_portfolio_slice` 内部把 cache 注入 `PostgresPortfolioOutboxPublisher`（execution role 才有 outbox）
9. wire / startup_recovery / post_init_guards
10. `ApplicationRuntime(...)` 构造（含 cache 字段）

`bootstrap()` 内部（与 6.2 同模板）：

```python
async def bootstrap(self, *, scope_fingerprint: str) -> None:
    self._loop = asyncio.get_running_loop()
    # Step 1: Redis hydrate
    try:
        stored = await self._hot_state_store.get(self._key_for(scope_fingerprint))
    except Exception as exc:
        log_event(..., "portfolio_snapshot_cache_bootstrap_redis_failed", level="warning", ...)
        stored = None
    if isinstance(stored, dict):
        try:
            snapshot = PortfolioSnapshot.model_validate(stored)
            self._latest[scope_fingerprint] = snapshot
            log_event(..., "portfolio_snapshot_cache_bootstrap_hydrated", scope=scope_fingerprint, snapshot_ts=str(snapshot.snapshot_ts))
        except Exception as exc:
            log_event(..., "portfolio_snapshot_cache_bootstrap_parse_failed", level="warning", ...)
    else:
        log_event(..., "portfolio_snapshot_cache_bootstrap_empty", scope=scope_fingerprint)
    # Step 2: NATS subscribe
    try:
        await self._bus.subscribe(topics.PORTFOLIO_SNAPSHOTS, self._handle_remote_event)
        self._subscribed = True
        log_event(..., "portfolio_snapshot_cache_subscribed", topic=topics.PORTFOLIO_SNAPSHOTS)
    except Exception as exc:
        log_event(..., "portfolio_snapshot_cache_subscribe_failed", level="warning", ...)
    self._bootstrapped = True
```

### 4.5 ApplicationRuntime 字段

```python
@dataclass
class ApplicationRuntime:
    ...existing fields...
    hot_state_store: HotStateStore                     # Slice 6.1
    kill_switch_sync_service: KillSwitchSyncService    # Slice 6.2
    portfolio_snapshot_cache: PortfolioSnapshotCache  # Slice 6.3 NEW
    ...
```

`stop_background_tasks` 在 `bus.close()` 之前 best-effort `await cache.stop()`（与 6.2 同）。

### 4.6 monolith / 单进程降级

monolith 模式（hot_state_backend=memory，bus=InMemoryBus）下：

- `PortfolioSnapshotCache.bootstrap()`：从 InMemoryHotStateStore.get 拿到 None → 不动本地 dict
- `PortfolioSnapshotCache.publish(snapshot)`：写 InMemoryHotStateStore（自进程内可读）
- 同进程 outbox publish 后 InMemoryEventBus 同步交付到 cache subscriber → `_handle_remote_event` 检测 source_role 跳过（如果有 source_role 字段，没有就直接更新本地 dict 无副作用）
- 完全 backward-compatible，所有现有 monolith 测试零修改

测试里直接调 `portfolio_repo.save_snapshot(...)`（绕过 cache）也仍然工作：
- 直接打 in-memory portfolio_repo
- cache 不会被绕过的写更新，但下次 sync caller 读时 cache miss → fallback portfolio_repo → 拿到正确数据

### 4.7 故意不做

- **周期性 Redis poll 兜底 NATS 掉包**：与 6.2 同决策。Stage 5/6.2 真跑 0 丢包，poll 是冗余复杂度。如果实盘期发现掉包，再加 Slice 6.5。
- **Redis CAS / Lua 脚本**：execution 进程是唯一 writer，没有并发写。LWW 简单覆盖即可。
- **history list 接 cache**：history 调用频率远低于 latest，且需要分页，引入 cache 收益有限。本 slice 只覆盖 latest。
- **cache 内做 scope 校验 / 合法性检查**：cache 是被动的 KV 容器，scope 隔离靠 key 命名空间，不在 cache 内做 business validation。

---

## 5. 不变量

| # | 不变量 | 如何保证 |
|---|---|---|
| I1 | execution 进程内的写入对本地 portfolio_repo 立即可见 | outbox publisher 现有事务不变，cache 只是补一层 |
| I2 | 4 进程内任何一次 portfolio commit 都会在 ≤1s 内被另外 3 个进程的 cache 看到 | NATS 广播延迟 < 50ms（与 6.2 同 budget，PORTFOLIO_SNAPSHOTS 已在 critical 路径） |
| I3 | 进程崩溃 + restart 之后 cache 能恢复最近一份 snapshot | `bootstrap()` 在 build_runtime 内 await Redis 读 |
| I4 | Redis 不可达：cache 读 fallback Postgres，cache 写 best-effort 跳过 | bootstrap / publish 内部 try/except |
| I5 | NATS 不可达：cache subscriber 收不到广播；下次 sync caller miss → fallback Postgres | bootstrap subscribe 失败不抛；远端事件路径独立于本地 dict 写入 |
| I6 | cache miss 不破坏读：所有 sync caller 在 cache 为空时 fallback 到现有 portfolio_repo 路径 | `_latest_scoped_snapshot` 内 cache.get_sync 返回 None 时 fallback |
| I7 | 8 处 sync caller API / 签名不变 | `_latest_scoped_snapshot` 仍是 sync method，cache 注入是私有字段 |
| I8 | 乱序 / 重投的 NATS 事件不会让 cache 退到旧 snapshot；同 snapshot_ts 视为 noop（execution 单一 writer 几乎不会发生） | `_handle_remote_event` 用 `snapshot_ts <= 本地` 判定 noop（D6）|
| I9 | scope 隔离：不同 product_type/margin_mode 的 snapshot 互不污染 | Redis key + 本地 dict key 都用 scope_fingerprint 命名空间 |

---

## 6. 实现要点（步骤分解）

### Step 1 — 新增 `PortfolioSnapshotCache` + 单测

文件：`aats/services/portfolio_service/snapshot_cache.py`

```python
class PortfolioSnapshotCache:
    def __init__(
        self, *,
        hot_state_store: HotStateStore,
        bus: EventBus,
        process_role: str,
        logger: Logger,
    ): ...

    async def bootstrap(self, *, scope_fingerprint: str) -> None: ...
    async def stop(self) -> None: ...
    async def publish(self, snapshot: PortfolioSnapshot) -> None: ...
    def get_sync(self, scope: RuntimeStateScope) -> PortfolioSnapshot | None: ...

    async def _handle_remote_event(self, message: dict[str, Any]) -> None: ...
    async def _best_effort_redis_set(self, scope_fingerprint: str, snapshot: PortfolioSnapshot) -> None: ...
    @staticmethod
    def _scope_fingerprint(scope: RuntimeStateScope) -> str: ...
    @staticmethod
    def _key_for(scope_fingerprint: str) -> str: ...
```

文件：`tests/unit/test_portfolio_snapshot_cache.py`（~15 用例，参考 `test_kill_switch_sync.py` 模板）

覆盖：
- bootstrap hydrate（Redis 有 / Redis 空 / Redis 失败 / parse 失败）
- publish happy path / Redis 失败 / fresher snapshot 覆盖 / 旧 snapshot 不覆盖
- get_sync hit / miss / scope 隔离（两个 scope 互不污染）
- _handle_remote_event：apply 新事件 / skip 旧事件 / parse 失败 / 多 scope
- snapshot() 自省

### Step 2 — `build_runtime` 接线 + lifecycle

文件：`aats/bootstrap/config.py`

1. import `PortfolioSnapshotCache`
2. `_RuntimeSlices` 加 `portfolio_snapshot_cache: Any = None`
3. 在 `KillSwitchSyncService.bootstrap()` 之后构造 cache + bootstrap：

```python
slices.portfolio_snapshot_cache = PortfolioSnapshotCache(
    hot_state_store=hot_state_store,
    bus=slices.bus,
    process_role=effective_process_role or "monolith",
    logger=get_logger("aats.portfolio.snapshot_cache"),
)
await slices.portfolio_snapshot_cache.bootstrap(
    scope_fingerprint=PortfolioSnapshotCache._scope_fingerprint(state_scope),
)
log_event(
    get_logger("aats.bootstrap"),
    "portfolio_snapshot_cache_initialized",
    process_role=effective_process_role or "monolith",
    bootstrap_state=slices.portfolio_snapshot_cache.snapshot(),
)
```

4. `ApplicationRuntime` 加 `portfolio_snapshot_cache: PortfolioSnapshotCache | None`
5. `stop_background_tasks` 在 `kill_switch_sync_service.stop()` 之后 + `hot_state_store.close()` 之前 best-effort `await portfolio_snapshot_cache.stop()`

### Step 3 — `_build_portfolio_slice` 注入 cache 到 outbox publisher

文件：`aats/bootstrap/config.py:_build_portfolio_slice`（execution role 才执行）+ `aats/services/portfolio_service/outbox.py`

`PostgresPortfolioOutboxPublisher` 加 `snapshot_cache: PortfolioSnapshotCache | None = None` 字段（默认 None 兼容现有调用方）。

`persist_fill_projection` (async) 改：
```python
async def persist_fill_projection(self, *, snapshot, ...):
    await asyncio.to_thread(self._persist_fill_projection_sync, snapshot=snapshot, ...)
    await self.flush_pending()
    if self.snapshot_cache is not None:
        await self.snapshot_cache.publish(snapshot)  # NEW: best-effort
```

`persist_bootstrap_snapshot` (async) 同样加。

⚠️ cache.publish 在 flush_pending **之后**调，不是之前——保证 NATS envelope 已经投递出去后再写 Redis 兜底。

### Step 4 — `_latest_scoped_snapshot` 接 cache

文件：`aats/services/operator/query_service.py:957`

```python
def _latest_scoped_snapshot(self):
    return self._cached(
        "latest_scoped_snapshot",
        self._latest_scoped_snapshot_uncached,
    )

def _latest_scoped_snapshot_uncached(self):
    cache = getattr(self.runtime, "portfolio_snapshot_cache", None)
    if cache is not None:
        cached = cache.get_sync(self.state_scope)
        if cached is not None:
            return cached
    return latest_snapshot_for_scope(self.runtime.portfolio_repo, self.state_scope)
```

8 处 caller 全部受益，零修改。

### Step 5 — 全量 unit 回归

```bash
python -m unittest discover -s tests/unit -t .
```

预期：1276 + N（N = Step 1 新增）全过，零退化。

### Step 6 — 集成测试（testcontainers Redis + 共享 InMemoryEventBus）

文件：`tests/integration/test_portfolio_snapshot_cache_cross_process.py`

参考 `test_kill_switch_cross_process.py` 模板，4 用例：

1. **跨进程实时广播**（I2）：service A publish snapshot → service B 的 cache.get_sync 返回该 snapshot
2. **重启 hydrate**（I3）：service A publish → 关掉 service B → 新 service B bootstrap 后 cache 已就绪
3. **fresher 排序**（I8）：先 publish 一个 snapshot_ts=T0，再 publish 一个 snapshot_ts=T0-10s 的旧 snapshot → cache 仍然是 T0
4. **scope 隔离**（I9）：两个 service 用不同 scope_fingerprint → 互不污染

`AATS_RUN_REDIS_INTEGRATION=1` + `.[redis-integration]` 双重 gating，默认 skip。

### Step 7 — WSL2 4 进程真跑验证

验证矩阵（参考 runbook §10.3）：

| 验证维度 | 不变量 | 期望证据 |
|---|---|---|
| 4 容器 healthy | — | docker ps 全 `Up (healthy)` |
| 4 个 cache 启动 | — | 每个容器一行 `portfolio_snapshot_cache_initialized` 日志 |
| 4 个 NATS durable consumer for portfolio.snapshots push_bound | — | `aats-{role}-portfolio_snapshots` consumer pending=0 |
| 触发一次 paper trading fill → 4 进程 cache 同步刷新 | I2 | 4 个 `portfolio_snapshot_cache_remote_applied snapshot_ts=...` 日志在同 ms bucket |
| 杀 gateway → 重启 → bootstrap 从 Redis hydrate cache | I3 | 新进程 `portfolio_snapshot_cache_bootstrap_hydrated snapshot_ts=...` 日志 |
| Dashboard 拉取实时反映 cache 命中 | I7 | log 验证 `_latest_scoped_snapshot_uncached` 走 cache 路径而非 fallback |
| Redis 中有 key | — | `redis-cli GET aats:hot:portfolio:latest:<scope>` 返回 JSON |

⚠️ **不需要 NATS stream 重建**：PORTFOLIO_SNAPSHOTS 已在 DEFAULT_CRITICAL_TOPICS，subjects 集合无变化，不触发 §10.5 部署纪律。

### Step 8 — runbook §10 后追加 §11 + changelog

文件：`docs/operations/stage7_wsl2_realrun_runbook.md`

新增 §11 `Stage 6 Slice 6.3：portfolio_snapshot 跨进程缓存（2026-04-XX）`：
- 11.1 边界与目标
- 11.2 实现要点
- 11.3 真跑验证矩阵
- 11.4 故障演练 / 部署纪律
- 11.5 完成判定
- 11.6 留给 Slice 6.4 / 6.5 的工作

旧 §11 Changelog 重命名为 §12，加 Slice 6.3 完成条目。

### Step 9 — 提交 + ff merge

- `feat(stage6-slice6.3): PortfolioSnapshotCache + 跨进程 cache 配线`（代码 + 测试）
- `docs(stage6-slice6.3): runbook §11 记录 Slice 6.3 真跑验证`
- `git checkout main && git merge --ff-only feat/stage6-slice6.3`
- `backup/pre-slice6.3` tag 保留紧急回滚点

---

## 7. 测试策略

### 7.1 单元测试（Step 1）

`tests/unit/test_portfolio_snapshot_cache.py`，预计 **15 用例**：

- **bootstrap**（4）：empty / hit / Redis fail / parse fail
- **publish**（4）：happy / Redis fail / fresher 覆盖 / 旧 snapshot 拒绝
- **get_sync**（3）：hit / miss / scope 隔离
- **_handle_remote_event**（3）：apply / skip stale / parse fail
- **snapshot()** 自省（1）

### 7.2 集成测试（Step 6）

`tests/integration/test_portfolio_snapshot_cache_cross_process.py`，**4 用例** + 1 个轻量 round-trip 单 service smoke。

- testcontainers redis:7-alpine + InMemoryEventBus 共享实例（与 6.2 同模式）
- gating: `AATS_RUN_REDIS_INTEGRATION=1` + `.[redis-integration]`

### 7.3 全量回归

- `python -m unittest discover -s tests/unit -t .` → 1276+N 全过
- Slice 6.1 9 个 Redis 集成测试 + Slice 6.2 4 个跨进程集成测试 → 全部 regression 全绿

### 7.4 4 进程真跑（Step 7）

按 Step 7 的 7 维度矩阵验证 I1/I2/I3 + dashboard 命中 cache 的日志证据。

---

## 8. 风险与回滚

### 8.1 风险点

| # | 风险 | 缓解 |
|---|---|---|
| R1 | cache.publish Redis 写失败累积 → 大量 warning 日志噪音 | best-effort + 日志限频（cache.publish 失败连续 N 次后降级到每分钟一次） — **本 slice 不做**，先观察真实噪音水平 |
| R2 | snapshot.model_validate 在 cache 反序列化时失败 → cache 永远 miss | parse 失败 log error + cache 维持上次内容；下次 sync caller fallback Postgres，无影响 |
| R3 | 4 进程同一时刻收到 NATS 广播 → cache 写并发 | 本地 dict 写是 sync 操作，asyncio 单线程 loop 内无 race；handler 是 await 链路，串行执行 |
| R4 | execution 进程 outbox publish 到 NATS 但 Redis SET 失败 → gateway 进程依赖 NATS 路径才能拿到，bootstrap 后第一次 dashboard 拉取走 fallback Postgres | 完全可接受，与不接 cache 的现状等价 |
| R5 | gateway 进程 cache 没及时收到 NATS 广播（消息队列 lag）→ dashboard 看到旧 snapshot | snapshot_ts 业务时间戳暴露给 UI，UI 自然显示 lag；6.2 实测延迟 < 50ms |
| R6 | 测试 / monolith 模式下 PortfolioSnapshotCache 引入额外 bus subscribe → InMemoryBus 内消息流量增加 | 无影响：InMemoryBus 是同进程内 list+dispatch，subscribe 一个 handler 只是多一次 await 调用 |

### 8.2 回滚

每个 Step 独立 git tag：
- `pre-slice6.3-step1` @ 6.2 完工 commit
- `pre-slice6.3-step2` @ Step 1 完成
- `pre-slice6.3-step3` @ Step 2 完成
- ... 以此类推

任何 Step 出问题：`git reset --hard pre-slice6.3-step<N>`。

**最小回滚成本**：所有 cache caller 都用 `getattr(runtime, "portfolio_snapshot_cache", None)`，如果发现 cache 引入 bug，最小修复 = 在 build_runtime 临时跳过 cache 构造，cache 字段为 None，所有 caller 自动 fallback Postgres。零业务影响。

### 8.3 紧急回退命令

```bash
# 完整回退到 6.2 完工状态
git reset --hard 0fd1889  # Slice 6.2 merge commit

# 部分回退（保留 cache 类，不接线）
git revert <step3-commit>  # 撤销 build_runtime 接线
```

---

## 9. 完成判定

- [ ] `PortfolioSnapshotCache` 类 + ~15 个 unit test 全绿
- [ ] `build_runtime` 装配 + `ApplicationRuntime.portfolio_snapshot_cache` 字段 + stop_background_tasks 清理（零行为变化检查点：113 个 build_runtime 相关 test 全过）
- [ ] `PostgresPortfolioOutboxPublisher` 注入 + `persist_fill_projection` / `persist_bootstrap_snapshot` async 包装层加 cache.publish
- [ ] `OperatorQueryService._latest_scoped_snapshot` 改造，cache 优先 fallback Postgres
- [ ] 全量 1276+N unit test 全绿，零退化
- [ ] `tests/integration/test_portfolio_snapshot_cache_cross_process.py` 4 用例 testcontainers 全绿
- [ ] Slice 6.1 9 个 Redis 集成 + Slice 6.2 4 个跨进程集成 regression 全绿
- [ ] 4 进程真跑：4 个 `portfolio_snapshot_cache_initialized` 日志 + 4 个 NATS durable consumer push_bound + paper trading fill 触发后 4 进程 cache 同步刷新 + 杀 gateway restart 后 bootstrap 自动 hydrate
- [ ] runbook §11 记录
- [ ] 2 commits + ff merge to main + backup tag 保留

---

## 10. 留给 Slice 6.4 / 6.5 的工作

Slice 6.3 完成后，hot_state_store 抽象层第一次被业务 caller 真正消费。但还有未完成的 6.x 工作：

1. **Slice 6.4 KillSwitch + KillSwitchSyncService 二合一**：当前 sync API 是为了 ~30 个 caller 零侵入而保留的过渡形态。等 6.3 落地稳定后可以把 `KillSwitch` 内部直接换成 `KillSwitchSyncService` 的薄壳，统一 API。**纯 API 整洁重构，可推迟到实盘前最后一波 cleanup**。
2. **Slice 6.5 Redis poll reconciler**：如果实盘期发现 NATS 偶发掉包导致 cache 漂移，加周期 Redis poll（默认 5s）作为 reconciler。当前实测 50ms 内同步 + 0 丢包，**不主动开**。
3. **Stage 8 OTel/Jaeger 接 collector**：是 6.x 完成后的下一个必须 milestone。Slice 6.3 真跑期间可以顺手在 cache.bootstrap / publish / handle_remote_event 加 trace span 准备工作，但**不在本 slice 范围**。

---

## 11. 与 Slice 6.2 的对比 — 为什么 6.3 复杂度更低

| 维度 | Slice 6.2 kill_switch | Slice 6.3 portfolio_snapshot |
|---|---|---|
| 是否 safety bug | ✅ 真实资金风险 | ❌ 性能优化，正确性兜底已经 work |
| 写路径数量 | 5 处生产 writer（W1-W5），含 sync→async 转换 | 2 处（outbox publisher 的两个 commit hook） |
| 读路径数量 | ~30 处 sync read 全部不动，但需要严格的本地 cache 新鲜度保证 | 8 处 sync read，cache miss 时 fallback Postgres，新鲜度要求弱 |
| 跨进程时序 | 必须 < 1s（因为 halt 是阻断决策的紧急动作） | 1s 是 nice to have（dashboard 几秒级 polling 节奏） |
| 不变量数量 | 7 个（I1-I7） | 9 个（I1-I9，但更多是 fallback 兜底，弱约束） |
| 部署纪律 | 触发 NATS stream 重建（DEFAULT_CRITICAL_TOPICS 加新 topic） | **不触发**，PORTFOLIO_SNAPSHOTS 已在 critical 集合 |
| 回滚成本 | 中（5 处 writer 改回去 + sync→async 链回退） | 低（cache 字段置 None 即自动 fallback） |
| 改动文件数 | ~16 个（含 routes / blocker_control / test_recovery 配套 await 链） | ~5 个（1 新建 + outbox + query_service + bootstrap + ApplicationRuntime） |

**结论**：Slice 6.3 的核心难点在"sync caller 怎么读 async cache"——决策 D2（cache 注入是私有字段，sync caller 通过 `get_sync` 同步读本地 dict）已经把这个难点解决。剩下的全是模板化复用 6.2 的 sidecar 模式。

---

## 12. 用户审批清单

- [ ] 同意"6.3 不是 safety bug 是性能优化"的定性 → 接受 cache miss fallback Postgres 的兜底策略
- [ ] 同意决策 D1：双通路（NATS push 实时 + Redis bootstrap hydrate）
- [ ] 同意决策 D2：8 处 sync caller 完全不动，cache 注入在 `_latest_scoped_snapshot` 内部
- [ ] 同意决策 D5：cache.publish 只写 Redis，不重复广播 NATS（依赖 outbox publisher 已有的 NATS 通路）
- [ ] 同意决策 D8：4 个 process_role 都装 cache（包括 execution 自己也订阅自己广播）
- [ ] 同意 Step 7 真跑验证矩阵（不需要 NATS stream 重建，零部署纪律风险）
- [ ] 同意 9 个不变量 I1-I9 的语义
- [ ] 同意按 Step 1-9 顺序实施
