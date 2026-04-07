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
| 阶段 3 | `process_role` 门控 | 完整 | 6 个 slice builder + `_SLICE_REQUIRED_ROLES` 矩阵已就位，每个 role 只构造本职 service。配套单测：`test_process_role_settings.py`、`test_scoped_runtime_lock_key.py` |
| 阶段 4 | NATS bus 接入 build_runtime | 完整 | 单元路径 ✅ 26 个 nats_bus 单测 + 6 个 bus shutdown 单测全过；集成路径 ✅ 4 个 testcontainers + multiprocessing 跨进程测试在 WSL2 Ubuntu + Docker 28.2.2 + nats:2.10-alpine + nats-py 2.14 环境下全过 (14.27s)。回归保护：`test_ensure_stream_passes_max_age_in_seconds_not_nanoseconds` 防止 max_age 双重换算 bug 复发 |
| 加餐 A | row_version 乐观锁（OCC） | 完整 | 覆盖 `StrategyExecutionBundle` (`save_execution_bundle_versioned` 完整接口 + InMemory 实现 + Postgres CAS) + `OrderStateModel` (Stage 5a-1，BIGINT row_version + `__mapper_args__['version_id_col']` + outbox `_persist_order_state_with_retry` 三次重试控制流)。`FillEventModel` 由 SELECT-then-INSERT 改成原子 `INSERT ... ON CONFLICT DO NOTHING` 消除 TOCTOU 竞态。`PortfolioSnapshotModel` / `ReconciliationStateSnapshotModel` 经 5a-2 review 决策跳过 row_version（autoincrement PK + append-only 语义）；reconciliation 改成 `pg_insert(...).on_conflict_do_nothing(["snapshot_id"])` 满足幂等。配套单测：`test_strategy_bundle_optimistic_lock.py`、`test_order_state_row_version.py`、`test_p1_snapshot_idempotency.py` |
| 加餐 B | `scoped_runtime_lock_key`（按 role 派生 advisory lock key） | 完整 | 单元测试覆盖；前置条件：阶段 3 真门控起来后才能让每个 role 跑自己的 scheduler 不打架 |
| 阶段 5 | NATS 全量 + 跨进程消息流 | 半成品 | Stage 5b：outbox publisher 类型清理为 `EventBus` 协议；Stage 5c：HybridBusRouting 路由表用真实 topics 常量并加严格未知 topic 检查（修真 bug）。剩余：决策↔执行、风控→决策、reconciliation→决策的 fan-out 真正切到 NATS critical 流的端到端验证 |
| 阶段 6 | Redis hot_state_store | 半成品 | 接口 + InMemory + Redis 未连接 stub。没有任何 service 在写/读 Redis |
| 阶段 7 | multiprocessing（4 容器拆分） | 装配完成（待 docker 真跑） | Stage 5d 完成：`aats/bootstrap/process_lifecycle.py` 统一 `build → start → wait SIGTERM → stop` 编排；4 个 entry script (`apps/{api_gateway,market_gateway,decision_engine,execution_engine}/main.py`) 各自显式 `process_role`；`deploy/wsl2-dev/Dockerfile` (python:3.12-slim + tini PID 1 + .[nats]) 与 `docker-compose.aats.yml` (4 service 共享 `aats-base:dev` 镜像，AATS_PROCESS_ROLE 区分)；Stage 5e 完成 `tests/smoke/test_4proc_pipeline.py` 同进程 4 runtime 并发 boot/run/stop smoke。**待补**：在 WSL2 上 `docker compose -f docker-compose.aats.yml up -d` 真跑一次 4 容器 healthcheck 全绿验证；任意容器 `docker kill` 后 NATS redeliver 收敛回归 |
| 阶段 8 | OTel telemetry 骨架 | 半成品 | no-op fallback + config，没真的接 collector，没在任何 hot path 埋 span |
| 阶段 9 | 长周期验证（30 天 dryrun） | 未动 | 依赖前面全部到位 |

**统计**：完整 6 项（阶段 1、2、3、4 + 加餐 A、B），装配完成待真跑 1 项（阶段 7），半成品 3 项（阶段 5、6、8），未动 1 项（阶段 9）。

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

**进度（2026-04-07）**：

| Step | 内容 | 状态 |
|---|---|---|
| Step 1 | `pyproject.toml` 加 `[project.optional-dependencies] nats = ["nats-py>=2.7"]` | ✅ |
| Step 2 | `AATSSettings` 加 `event_bus_backend / nats_url / nats_stream_name / nats_stream_max_age_seconds` + validator + 41 单测 | ✅ |
| Step 3 | `aats/bootstrap/config.py` 加 `_construct_event_bus` 工厂 + `_start_event_bus` 生命周期，`_build_shared_runtime_slice` 调它；`HybridEventBus` 加 `start/close`；`NatsEventBus` 加 `start(topics=)` 便利方法 + 23 单测 | ✅ |
| Step 4 | `ApplicationRuntime.stop_background_tasks` best-effort `await bus.close()`，顺序在 db dispose 之前 + 6 单测 | ✅ |
| Step 5 | testcontainers 集成测试：单 bus + HybridEventBus 路由 round-trip — 见 `tests/integration/test_nats_event_bus_roundtrip.py` | ✅ 写好 + WSL2 实跑全过 |
| Step 6 | 跨进程 round-trip：`multiprocessing.Process` (spawn) 起独立 publisher 子进程 — 同上文件 `TestCrossProcessNatsRoundTrip` | ✅ 写好 + WSL2 实跑全过 |
| Step 7 | 更新本 roadmap（即本节修改） | ✅ |

**集成测试发现 + 修复的真实 bug**（2026-04-07）：

1. **`max_age` 双重换算 bug**（`aats/bus/nats_bus.py:269`）：原代码把秒预乘 1e9 后传给 `StreamConfig.max_age`，但 nats-py 2.14 的 `max_age` 字段以**秒**为单位（`# in seconds`），内部 `_to_nanoseconds()` 自行换算 → 双重换算后值变成 6×10^19，NATS server JSON parser 直接 reject `code=400 err_code=10025 description='invalid JSON'`。修复：直接传秒。回归测试：`test_ensure_stream_passes_max_age_in_seconds_not_nanoseconds`。
2. **集成测试 EventEnvelope schema 写错**：原测试用 `EventEnvelope(topic=..., key=..., ts=..., payload=...)` 缺少 `event_type` 和 `source_component` 必填字段。已修。
3. **同 NATS server 上多个 stream 想 claim 同一 subject**：JetStream 不允许两个不同 stream 拥有相同 subject。在 `asyncTearDown` 加 `_purge_all_streams` 做隔离；observer verifier 用独立 stream 名 `AATS_RT_OBSERVER_VERIFIER`。

**集成测试运行方式**：

```bash
# 1. 安装可选依赖
pip install -e .[nats-integration]

# 2. 确保本地 docker 可用（Windows 上启动 Docker Desktop 或 WSL2 内的 docker）
docker info

# 3. 设置环境变量并运行
AATS_RUN_NATS_INTEGRATION=1 python -m pytest tests/integration/test_nats_event_bus_roundtrip.py -v
```

**默认行为**：环境变量未设置或依赖未装时，整组测试 `unittest.skipUnless(...)` 跳过，不影响 CI / 单元套件。

**实跑环境**（已验证 2026-04-07）：
- WSL2 Ubuntu 22.04 LTS（用户 WSL 在 `F:\WSL\Ubuntu\ext4.vhdx`）
- Python 3.12.3（venv `~/aats-venv`）
- Docker 28.2.2（WSL 内置 systemd dockerd）
- nats-py 2.14.0、testcontainers 4.14.2、nats:2.10-alpine

跑出来 `Ran 4 tests in 14.270s OK`，且发现并修复了 1 个生产代码 bug。

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
- 2026-04-07：阶段 3 真门控合并完成（半成品 → 完整）；阶段 4 NATS 接入 build_runtime 的 Step 1-4 完成，1176 单测全过；Step 5-6 的 testcontainers + multiprocessing 跨进程集成测试已写在 `tests/integration/test_nats_event_bus_roundtrip.py`，通过 `AATS_RUN_NATS_INTEGRATION=1` + `pip install -e .[nats-integration]` 双重 gating，默认 skip 不影响主套件。Step 7（本 roadmap 更新）随之完成。节点 1.2 仍保留"基本完整"状态，待集成测试在 Docker 环境真跑过一次绿才能升级为"完整"。
- 2026-04-07（同日续）：在 WSL2 Ubuntu 22.04 + Docker 28.2.2 + Python 3.12 venv 真跑 Step 5-6 集成测试 `Ran 4 tests in 14.270s OK`。过程中发现并修复 1 个生产代码 bug：`aats/bus/nats_bus.py` 把 `stream_max_age_seconds` 预乘 1e9 后传给 `StreamConfig.max_age`，但 nats-py 2.14 的字段以秒为单位、内部自行换算 → 双重换算后值变成 6×10^19，NATS server 直接 reject `invalid JSON`。修复方式：直接传秒；并补回归单测 `test_ensure_stream_passes_max_age_in_seconds_not_nanoseconds`。集成测试还修了 `EventEnvelope` 必填字段缺失（`event_type` / `source_component`）和同 server 多 stream subject overlap 隔离问题。**节点 1.2 升级为"完整"**，阶段 4 整体升级为"完整"。
- 2026-04-07（同日续 · Stage 5 收尾）：一次性闭环 Stage 5 的 6 个子任务，从 1184 单测推进到 1206 单测全过（+22）、1 skipped、零退化。
  - **5b** `4df7594 refactor(stage5b): outbox publisher 强类型清理为 EventBus 协议`：把 outbox publisher 内部的 bus 字段从 `Any` 收紧为 `EventBus` 协议，消除"什么 bus 都能塞进来"的隐式契约风险。配套测试 +0（已被现有 outbox 套件覆盖）。
  - **5c** `9751d08 fix(stage5c): NATS HybridBusRouting 路由表用真实 topics 常量 + 严格未知 topic 检查`：修真 bug——原 HybridBusRouting 路由表写的是字符串字面量而不是 `aats/topics.py` 的常量，且未知 topic 默认走 observer，会导致跨进程 critical 消息被静默降级到 in-memory observer 路径。改成显式常量映射 + unknown topic raise。
  - **5a-1** `a6139be fix(stage5a-1): order_states 加 row_version OCC + fill_events 改原子幂等`：`OrderStateModel` 加 `row_version BIGINT NOT NULL DEFAULT 1` + `__mapper_args__['version_id_col']`；outbox `_persist_order_state_with_retry` 三次重试控制流封装 `StaleDataError`；`FillEventModel` 由 SELECT-then-INSERT 改成原子 `pg_insert(...).on_conflict_do_nothing(["fill_id"])` 消除 TOCTOU。Migration `0006_postgres_order_state_row_version.sql`。配套单测 `test_order_state_row_version.py`。
  - **5a-2** `b8e1b45 fix(stage5a-2): reconciliation 状态快照改幂等插入 + P1 决策文档化`：原 P1 设计文档要求 `portfolio_snapshots` / `reconciliation_state_snapshots` 加 row_version，review 后判断技术性错误——两表都是 append-only，前者 autoincrement int PK 天然无冲突，后者 string PK 但唯一冲突等价于"同一份历史快照"。决策跳过 row_version；`reconciliation_repo_postgres.save_state_snapshot` 改成 `pg_insert(...).on_conflict_do_nothing(["snapshot_id"])` 满足崩溃恢复重复 enqueue 的幂等语义。Migration `0007_postgres_p1_snapshot_idempotency.sql` 仅文档化决策（无 schema 改动）。配套单测 `test_p1_snapshot_idempotency.py`（+5）。
  - **5d** `fc6107a feat(stage5d): 4 进程拓扑 entry + Dockerfile + 4 服务 compose 装配`：新增 `aats/bootstrap/process_lifecycle.py`（`_resolve_process_role` + `run_process` + `run_process_sync` + 跨平台 SIGTERM/SIGINT 处理），重写 `apps/{market_gateway,decision_engine,execution_engine}/main.py` 三个 daemon entry 用 `run_process_sync(process_role=...)` 替换手写 build_runtime+asyncio.run，`apps/api_gateway/main.py` 的 lifespan 显式 `process_role=AATS_PROCESS_ROLE 解析`；新增 `deploy/wsl2-dev/Dockerfile`（multi-stage python:3.12-slim builder + runtime + tini PID 1 + 非 root `aats` UID 1000 + `pip install -e ".[nats]"`）和 `deploy/wsl2-dev/docker-compose.aats.yml`（YAML anchors 共享 `aats-base:dev` 镜像 + 4 个 service 用 `AATS_PROCESS_ROLE` 区分 + gateway 暴露 `127.0.0.1:8000:8000` + execution 1536M 内存）。配套单测 `test_process_lifecycle_and_entries.py`（+14）。
  - **5e** `42a14d0 test(stage5e): 4 进程拓扑同进程 smoke 测试`：新增 `tests/smoke/test_4proc_pipeline.py`（+3 smoke），同进程并发构造 4 份 runtime（asyncio.gather）→ start_background_tasks → slice 矩阵断言（每个 role 只装本职 service） → EventBus 实例独立性断言 → market role in-memory bus `publish_envelope(persist=False)` → subscribe 闭环。pure in-memory 不依赖 docker/Postgres/NATS，能在没有外部依赖的前提下捕获 boot/teardown 回归。WSL2 真 docker compose 4 容器实跑由人工/CI nightly 验证。
  - 安全网：每个子任务都有自己的 `pre-stage-5{b,c,a-1,a-2,d,e}-v1` git tag，可独立回滚。
  - **状态变化**：加餐 A OCC 由"完整（仅 bundle）"扩展为"完整（bundle + order_state + fill + P1 snapshot 决策）"；阶段 5 由"未动"→"半成品"（5b/5c 完成，5d/5e 已装配，剩余真正端到端跨进程 fan-out 切到 NATS critical 流验证）；阶段 7 由"未动"→"装配完成（待 docker 真跑）"（4 entry + Dockerfile + compose 全部就位，差 WSL2 上 `docker compose up -d` 真跑 healthcheck 全绿这一个 gate）。
  - **下一关（不在本次 commit 范围内）**：在 WSL2 上拉真 docker compose 起 4 容器，跑 healthcheck + 任意容器 `docker kill` 后 NATS redeliver 收敛回归；通过后阶段 7 升级为"完整"，进入阶段 5 真正剩余的 NATS critical fan-out 验证。
