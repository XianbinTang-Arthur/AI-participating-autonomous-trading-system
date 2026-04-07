# AATS 多进程切片化重构 Roadmap

## 文档定位

| 项目 | 内容 |
|---|---|
| 创建日期 | 2026-04-07 |
| 文档作用 | 把"已完成 / 半成品 / 未动"和"补齐顺序"放在一处，避免再次出现阶段编号错位 |
| 与 `docs/task/` 的区别 | `task/` 下是按 task 编号组织的单个交付物设计；本文是跨 task 的全局视角和依赖图 |
| 维护责任 | 每完成一组节点必须回来刷新本文的"真实状态盘点"和对应 commit hash |
| 替代关系 | 不替代 `CLAUDE.md` 中关于本重构的备忘，但在两者出现分歧时以本文为准 |

---

## 1. 背景与目标

AATS 当前以单进程 monolith 形态运行：一个 Python 进程同时跑 gateway（HTTP/WS 接入）、market（行情接入与 candle 持久化）、decision（策略评估、组合分配、execution bundle 生成）和 execution（下单、对账、风控反馈）。

本次重构的最终目标是把这四块拆成四个独立进程，由 docker-compose 编排，跨进程消息走 NATS JetStream + Redis 共享热状态 + Postgres 持久化主存：

- **gateway**：HTTP/WS 接入、UI 静态资源、操作员后台
- **market**：OKX/其它交易所行情订阅、candle bronze/silver/gold 持久化
- **decision**：策略评估、组合分配、execution bundle 生成
- **execution**：交易所下单、fill 接收、风控反馈、obligation 维护、对账

最终验收信号（即"重构完成"的判定）：

1. `docker compose up gateway market decision execution` 一次性起 4 个容器，4 个 healthcheck 全绿
2. Jaeger 能看到一条"决策 → 执行 → fill → 风控"的端到端 trace，跨容器 span 正常拼接
3. Loki 单日错误率不超过 monolith 模式基线
4. 30 天 dryrun 期间无 lost-update（OptimisticLockError 都被 caller 正确 retry）、无 obligation/order 状态漂移
5. 拔掉任意一个容器并 docker compose up 重启，NATS JetStream 能 redeliver 收敛、Redis 热状态能 rebuild

---

## 2. 真实状态盘点（截至 2026-04-07）

**注**：本表代表的是 review 过的代码事实，不是脑海中的计划。任何带"半成品"标记的项都意味着"代码存在但没人在用 / 没接通 / 没门控"。

| 编号 | 项目 | 状态 | 关键缺口 |
|---|---|---|---|
| 阶段 1 | 基础设施（docker-compose / backup / restore / RUNBOOK） | 完整 | — |
| 阶段 2 | `build_runtime` 切片化（拆出 4 个 slice builder） | 完整 | — |
| 阶段 3 | `process_role` 门控 | 半成品 | settings 字段、env 解析、`_resolve_effective_process_role` 都有，但 4 个 slice builder 内部没有按 `process_role` 跳过任何组件——目前不管什么 role 都构造完整 runtime |
| 阶段 4 | NATS bus 骨架 | 半成品 | `NatsEventBus` / `HybridEventBus` / `ConsumerConfigSpec` 都已实现并有单元测试，但 `build_runtime` 里根本没 import 它，没人真的在用 |
| 加餐 A | row_version 乐观锁（OCC） | 完整 | `save_execution_bundle_versioned` 完整接口 + InMemory 实现 + Postgres CAS（含 `INSERT ... ON CONFLICT DO NOTHING` 修复）+ 单元测试覆盖 |
| 加餐 B | `scoped_runtime_lock_key`（按 role 派生 advisory lock key） | 完整 | 单元测试覆盖；前置条件：阶段 3 真门控起来后才能让每个 role 跑自己的 scheduler 不打架 |
| 阶段 5 | NATS 全量 + 跨进程消息流 | 未动 | 决策↔执行、风控→决策、reconciliation→决策等所有跨 role 的 fan-out 仍走 in-memory bus |
| 阶段 6 | Redis hot_state_store | 半成品 | 接口 + InMemory + Redis 未连接 stub。没有任何 service 在写/读 Redis |
| 阶段 7 | multiprocessing（4 容器拆分） | 未动 | 没有 4 个独立入口、docker-compose 没有 4 service、没跨进程启动验证 |
| 阶段 8 | OTel telemetry 骨架 | 半成品 | no-op fallback + config，没真的接 collector，没在任何 hot path 埋 span |
| 阶段 9 | 长周期验证（30 天 dryrun） | 未动 | 依赖前面全部到位 |

**统计**：完整 4 项（阶段 1、2 + 加餐 A、B），半成品 4 项（阶段 3、4、6、8），未动 3 项（阶段 5、7、9）。

---

## 3. 依赖图

```
阶段 1 ──┐
阶段 2 ──┼──> 阶段 3 真门控 ──┐
         │                    ├──> 阶段 7 拆 4 容器 ──> 阶段 9 长周期验证
         └──> 阶段 4 集成 ────┴──> 阶段 5 NATS 全量 ┘
                                                    ▲
加餐 A OCC ─────────────────────────────────────────┤
                                                    │
阶段 6 Redis 接真 ──────────────────────────────────┤
                                                    │
阶段 8 OTel 接 collector ───────────────────────────┘
```

**关键阻塞关系**：

- **阶段 3 真门控** 和 **阶段 4 集成**（让 `build_runtime` 真的 import `NatsEventBus`）是 **阶段 5 / 7 的双重前置**。少了任意一个，多进程跑起来要么两个进程同时构造同一个 service 抢资源，要么跨进程消息根本到不了对端。
- **加餐 A OCC** 和 **阶段 6 Redis** 是"并发安全 + 共享状态"两条线，逻辑上独立，但只有阶段 7 多进程跑起来后才有真正意义。在 monolith 模式下它们更像是"为未来准备的安全网"。
- **阶段 8 OTel** 在拆进程之前接也行，但单进程下 trace 看不出多服务调用，价值低。建议放在阶段 7 之后做。

---

## 4. 补齐顺序

### 第一组：让现有"骨架"变成可证伪的"接口"

#### 节点 1.1 阶段 3 真门控

把 4 个 slice builder 内部按 `process_role` 跳过非本 role 组件。

**示例**：
- `build_decision_slice` 在 `process_role=decision` 时不构造 `ExecutionService` / `OrderManagementService`
- `build_gateway_slice` 在 `process_role=gateway` 时不构造 `RiskService` / `DecisionService`

**验收**：

```bash
AATS_PROCESS_ROLE=execution python -c "
import asyncio
from aats.bootstrap.config import build_runtime
runtime = asyncio.run(build_runtime())
assert runtime.decision_service is None
assert runtime.execution_service is not None
"
```

**单测要求**：每个 role 至少 1 个测试，断言"本 role 该有的 service 不为 None，不该有的 service 是 None"。

---

#### 节点 1.2 阶段 4 集成

让 `build_runtime` 在 `process_role != monolith` 时用 `HybridEventBus(critical_bus=NatsEventBus, observer_bus=InMemoryBus)` 替换默认的纯 `InMemoryBus`。

**关键约束**：
- monolith 仍用 `InMemoryBus`（向后兼容，单进程模式不引入 NATS 依赖）
- 任何 slice builder 不能在 import 时就 require `nats-py`——必须 lazy import，否则 monolith 模式启动会报错
- `NatsEventBus.connect()` 失败时 build_runtime 必须 fail-fast，不能静默 fallback 到 in-memory（否则跨进程消息会神秘消失）

**验收**：单个 slice 在 docker-compose 起 NATS 容器后能 publish 一条 envelope，再用另一个 NATS 客户端 subscribe 收到。

---

完成第一组后，"骨架"就变成"已接通"，后续阶段才有支点。**这一组是整个 roadmap 的关键路径，必须先做**。

---

### 第二组：跨进程数据流

#### 节点 2.1 阶段 5 NATS 全量

把所有真正跨 `process_role` 的 fan-out 切到 NATS：

| 消息流 | from role | to role | 当前实现 | 目标 |
|---|---|---|---|---|
| execution_intent | decision | execution | InMemoryBus | NATS critical |
| fill_event | execution | decision | InMemoryBus | NATS critical |
| risk_event | execution | decision | InMemoryBus | NATS critical |
| reconciliation_result | execution | decision | InMemoryBus | NATS critical |
| metrics_sample | * | gateway | InMemoryBus | NATS observer |
| dashboard_refresh_hint | * | gateway | InMemoryBus | NATS observer |

**关键约束**：同 `process_role` 内部仍走 in-memory bus（HybridEventBus 已经做了路由分发）。

**验收**：双进程（gateway+execution）docker-compose 起来后，gateway 发 intent → execution 收到并下单 → fill → gateway 看到 portfolio 更新。

---

#### 节点 2.2 阶段 6 Redis 接真（可与 2.1 并行）

把跨进程读写的"热状态"迁到 Redis。第一批候选：

- portfolio snapshot（execution 写、decision/risk 读）
- order obligation map（execution 写、decision/risk 读）
- open order set（execution 写、decision 读）

**关键约束**：必须是 write-through——Postgres 写完才写 Redis。Redis 失败要 fail-fast，不能 fallback 到 stale 数据，否则决策会基于过期 portfolio 下单导致重复持仓。

**验收**：execution 写 obligation → decision/risk 进程能立即 get 到。

---

### 第三组：真的拆 4 个进程

#### 节点 3.1 阶段 7 multiprocessing

- 4 个独立入口脚本：`scripts/run_gateway.py` / `run_market.py` / `run_decision.py` / `run_execution.py`
- 4 个 Dockerfile target 或 4 套 entrypoint
- docker-compose 4 个 service，各自 healthcheck

**验收**：
1. `docker compose up gateway market decision execution` 一切正常
2. 4 个 healthcheck 全绿
3. 任意一个容器 `docker kill` 后 `docker compose up` 自动恢复，且 NATS JetStream redeliver 不丢消息

---

### 第四组：可观测性 + 长周期验证

#### 节点 4.1 阶段 8 OTel 接 collector

- 接 Jaeger / OTLP collector
- 在跨进程 NATS publish/subscribe 处注入/提取 trace context（telemetry skeleton 已经预留 inject/extract 接口）
- 在 4 个 slice builder 的 service 构造路径埋初始 span

**验收**：Jaeger 能看到一条决策 → 执行 → fill → 风控的端到端 trace，4 个进程的 span 拼成一条完整链路。

---

#### 节点 4.2 阶段 9 长周期验证

30 天 dryrun。期间监控：

- Loki 错误率（与 monolith 基线对比）
- Jaeger latency p50/p95/p99
- Postgres 是否触发 OptimisticLockError，触发频率与 caller retry 是否成功收敛
- Redis hit rate / connection pool 健康度
- NATS JetStream redeliver 次数 / 是否有未 ack 消息堆积

**验收**：30 天结束时所有指标平稳、无 lost-update、无 obligation/order 漂移。

---

## 5. 风险与门槛

| 风险 | 触发节点 | 缓解措施 |
|---|---|---|
| 阶段 3 门控漏掉一个组件，两个进程同时构造同一个 service 引发并发写竞争 | 节点 3.1（阶段 7）拆进程那一刻 | 加餐 A OCC + 加餐 B scoped_runtime_lock_key 已就位，最差是抛 OptimisticLockError 让 caller retry，不会丢钱。但是节点 1.1 的单测必须 cover 每个 role 真的不构造非本 role 组件 |
| NATS JetStream stream 配置不当，重启后 redeliver 风暴 | 节点 1.2 集成第一次起来 | `max_deliver=5` + `ack_wait=30s` 已配，需在节点 2.1 集成测试里显式跑过一次进程 `kill -9` 验证 redeliver 收敛 |
| Redis 拿到 stale 数据导致决策基于旧 portfolio 下单 | 节点 2.2 接真 | hot_state_store 必须是 write-through（Postgres 写完才写 Redis），且 Redis 失败必须 fail-fast 不能 fallback 到 stale |
| 多进程下 NATS 客户端连接泄漏 / event loop 死锁 | 节点 3.1 起 4 容器 | 每个 slice builder 必须有 graceful shutdown 路径，且对应单元测试必须 cover `bus.close()` 路径 |
| OCC 只覆盖 `save_execution_bundle_versioned`，其它跨进程会写的表没做 OCC | 节点 2.1 NATS 全量铺开后 | 在节点 2.1 之前必须评估每条写路径——portfolio_snapshot / order_state / fill / reconciliation_state_snapshot——是否要补 row_version 或者用 advisory lock 串行化 |
| 拆进程后老的 in-memory bus / 老的 InMemory hot_state_store 还在被某些 service 偷偷用 | 节点 3.1 之后 | 在节点 3.1 之前必须 grep 全 codebase，确认没有 service 直接 import `InMemoryEventBus` / `InMemoryHotStateStore`；只能通过 build_runtime 注入 |

---

## 6. 范围之外但要记得的事

这些不是本 roadmap 的节点，但与重构相关，必须在对应时点处理：

1. **加餐 A OCC 只覆盖 `StrategyExecutionBundle`**：其它跨进程会写的表（portfolio_snapshot / order_state / fill / reconciliation_state_snapshot）目前**没有 row_version**。在节点 2.1（阶段 5 NATS 全量）之前，必须逐表评估并补齐。

2. **加餐 B scoped_runtime_lock_key 已就位**：每个 role 派生不同的 advisory lock key，可以让 4 个 slice 各自跑 scheduler 不打架。但前提是节点 1.1（阶段 3 真门控）已经让每个 role 只跑自己负责的 scheduler job——否则两个 role 都跑同一个 job 时锁是按 role 分的反而会让两边都拿到锁。

3. **阶段 1 的 backup/restore**：在节点 4.2（阶段 9 长周期验证）之前，必须 dry-run 一次完整的 backup → 删表 → restore → 跑 dryrun 流水，确认 backup 真的能用。这条容易被推迟到出事时才发现 backup 是坏的。

4. **Postgres advisory lock 与 OCC 的边界**：advisory lock 是粗粒度串行化（一段时间只让一个 role 写），OCC 是细粒度乐观锁（任何 role 都能写但靠 row_version 检测冲突）。两者不冲突，但每条写路径应该明确选一个，不要同时用两个机制。

5. **NATS subject 命名**：所有 subject 必须以 `aats.` 开头（`NatsBusConfig.subject_for` 已经强制了），但跨环境（dev / staging / prod）应当用不同的 stream name 隔离。在节点 1.2 集成时把 stream name 加上环境前缀。

---

## 7. 跟踪与更新

### 7.1 更新责任

- **每完成一组节点必须回来刷新第 2 节"真实状态盘点"**：把对应行从"半成品/未动"改成"完整"，并在"关键缺口"列填入 commit hash 范围
- **每开始一组节点必须更新本文档头部"创建日期"为修订日期**，并在文档底部追加一行 changelog
- **若发现节点间依赖关系判断错误**：必须先回来改第 3 节依赖图，再开始动代码

### 7.2 节点完成判定

只有满足以下两个条件，节点才算"完成"：

1. 该节点所列的所有"验收"步骤都跑通过
2. 对应的单元测试 + 集成测试（如适用）已合并入 main，且全量测试套件无退化

**禁止**仅凭"代码写完了"就把节点标记为完整。

### 7.3 下次评估时间

- 第一组（节点 1.1 + 1.2）目标：在动手写阶段 7 拆进程之前必须全部完成
- 重大里程碑：第一组完成后回到本文档评估"是否需要重排第二组顺序"
- 长期：每月至少 review 本文档一次，确认与代码事实同步

---

## Changelog

- 2026-04-07：首版。基于 review 完阶段 1-4 + 加餐 OCC 后的真实状态盘点，确认阶段编号与原 9 阶段计划对齐。
