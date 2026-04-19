# Stage 5 设计文档：4 进程拓扑装配 + 配套硬化

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


> **状态**：设计草案，待用户批准后才进入实施
> **作者**：Claude（基于 4 份并行调研报告综合）
> **日期**：2026-04-07
> **基线 commit**：f6baa52（`pre-slice-refactor-v1` 备份 tag 之后的当前 HEAD）
> **关联 roadmap**：`docs/operations/multiprocess_refactor_roadmap.md` 节点 1.3 / 3.1

---

## 1. 范围与目标

Stage 5 的核心目标：**把已就位的"切片化代码 + NATS HybridEventBus + 锁/迁移配套"组装成真正能跑的 4 进程拓扑**，并提供 docker-compose 一键启动入口、跨进程集成验证、回滚预案。

### 1.1 在 Stage 5 范围内（必须做）

- **5a.** 关键表 OCC（optimistic concurrency control）补齐：`order_states` / `execution_fills` 加 `row_version`
- **5b.** `InMemoryEventBus` 强类型注入清理（消除 outbox 类型注解和实际类型不匹配的 C 类问题）
- **5c.** **🚨 路由表 bug 修复**（Stage 4 集成里发现的隐患，必须在 4 进程拓扑前修复）
- **5d.** 4 entry script + Dockerfile + docker-compose app services 装配
- **5e.** 跨进程集成验证（gateway↔decision↔execution 走 NATS 真实端到端）

### 1.2 **不在** Stage 5 范围（明确推迟）

| 项 | 推迟到 | 理由 |
| --- | --- | --- |
| OTel/Jaeger 接线 | Stage 6 | `aats/bootstrap/telemetry.py` 仅骨架 |
| Redis HotStateStore | Stage 8 | `aats/storage/hot_state_store.py` 仅骨架，未被任何业务路径使用 |
| `multiprocessing.Pool` feature engine | Stage 7 | 与拓扑装配正交，先验证 4 进程可跑 |
| 30 天严格验证窗口 | Stage 9 | Stage 5 内只做"短窗"功能验证（数小时） |
| 反向兼容/灰度切换 UI | 推迟 | monolith 仍可用作回退路径，不需要 UI 层做切换 |

### 1.3 验收基线

- 4 个 docker 容器（gateway/market/decision/execution）能启动且 health endpoint 全绿
- 一笔 mock decision 从 decision_proc 经 NATS → execution_proc，落库 + 状态机推进，全程无消息丢失
- monolith 模式仍能跑（默认 `event_bus_backend=in_memory`，零退化）
- 单元测试零回归（Windows Python 3.14 全套通过）

---

## 2. 前置事实摘要（4 份调研结论）

### 2.1 row_version 现状（Investigation 1）

**当前已有 OCC 的表**：仅 `strategy_execution_bundles.row_version`（`aats/storage/sqlalchemy_models.py:234`），由 migration `0005_postgres_strategy_execution_bundle_row_version.sql` 引入。

**P0 必须补齐**（Stage 5a）：

| 表 | 文件:行 | Why |
| --- | --- | --- |
| `order_states` | `sqlalchemy_models.py:364` | execution_proc 多个 background loop 并发写 (`ExecutionOrderRepository.upsert_order` / `record_state_history`)；崩溃中段恢复时若没 row_version 检测，老 snapshot 可能覆盖新 fill 派生的状态 |
| `execution_fills` | `sqlalchemy_models.py:645` 附近 | 同进程内 reconciliation 重放 + 实时 fill outbox 重投递可能竞争 |

**P1 可推迟**（Stage 9 之前补即可）：

| 表 | 推迟理由 |
| --- | --- |
| `portfolio_snapshots` | 单写者（execution_proc 内部 portfolio slice），写路径线性，竞争只在崩溃恢复 |
| `reconciliation_state_snapshots` | 同上，单写者 |

**关键约束（_SLICE_REQUIRED_ROLES）**：`aats/bootstrap/config.py:2574-2584` 已明确——`execution / portfolio / reconciliation / startup_recovery` 4 个 slice **全部** 装在 `execution` role 下，因此上述 4 张表的写竞争是**进程内 async 协程竞争**，**不是跨进程竞争**。

> **影响**：P0 表的 OCC 主要防护"同进程内多协程 / 崩溃恢复时序"，不是防护"两个独立进程同时写"。这会简化 retry 策略（不需要分布式锁，只需要 SQLAlchemy 层的 stale-update detection）。

### 2.2 InMemoryEventBus 注入现状（Investigation 2）

**只有 2 个 C 类问题**（类型注解和实际类型不一致）：

| 文件:行 | 现状 | 修法 |
| --- | --- | --- |
| `aats/services/portfolio_service/outbox.py:11,28` | `from aats.bus.memory_bus import InMemoryEventBus` + `bus: InMemoryEventBus` | 改为 `from aats.bus.base import EventBus` + `bus: EventBus` |
| `aats/services/execution_engine/outbox.py:10,38` | 同上 | 同上 |

**Why this matters**：当前 build_runtime 注入的实际是 `HybridEventBus`（Stage 4 后默认 in_memory，但生产 hybrid 模式下注入的是 `HybridEventBus`），运行期 duck-typing 工作正常，但 type checker 看不出来。一旦未来加 mypy 严格模式或者重构时被静态检查工具误判为"用错类型"，会有人误删 NATS 路径。

**Hot state store**：`aats/storage/hot_state_store.py` 是 Stage 8 骨架，目前**没有任何业务路径**调用它，不在 Stage 5 范围。

### 2.3 6 条跨 role 消息流的路由审计（Investigation 3 — 🚨 重大风险）

**核心 bug**：`aats/bus/nats_bus.py:47-64` 的 `DEFAULT_CRITICAL_TOPICS` 与 `aats/events/topics.py` 实际使用的 topic 名**几乎全部不匹配**：

| `DEFAULT_CRITICAL_TOPICS` 写的 | `events/topics.py` 实际用的 | 是否匹配 |
| --- | --- | --- |
| `execution_intents` | `execution.order_intents` (`ORDER_INTENTS`) | ❌ |
| `execution_orders` | `execution.order_updates` (`ORDER_UPDATES`) | ❌ |
| `execution_fills` | `execution.fill_events` (`FILL_EVENTS`) | ❌ |
| `risk_events` | `risk.decisions` (`RISK_DECISIONS`) | ❌ |
| `reconciliation_results` | `reconciliation.reports` (`RECONCILIATION_REPORTS`) | ❌ |
| `portfolio_snapshots` | `portfolio.snapshots` (`PORTFOLIO_SNAPSHOTS`) | ❌ |
| `position_targets` | `strategy.position_target` (`POSITION_TARGETS`) | ❌ |
| `decisions` | （不存在直接对应的 `topics.py` 常量） | ⚠️ |

**为什么 Stage 4 集成测试还是过的？** 因为 `HybridBusRouting.route_for()` 在 topic 不在两个集合时会**默认 fallback 到 `critical`**（`route_for` 的 `default_route` 参数）。所以 6 条核心消息流确实都进了 NATS，**但完全是靠"未知 topic 默认走 critical"这条 catch-all 蒙混过关的，路由表本身完全失效**。

**直接后果**：

1. observer 集合也写错了——`metrics_samples` / `dashboard_refresh_hints` 这些 topic 在代码里根本没被任何 publisher 用到（grep 验证），observer 路径目前是死代码
2. 一旦未来某条 topic 被不小心加到 observer 集合（比如有人想优化 fill 推送性能），路由表的 bug 会立刻让 fill 走内存路径丢失
3. JetStream `subjects` 配置依赖正确的 topic 名，路由表如果将来被用作 subject 注册的来源，会导致 stream 注册的 subject 和实际 publish 的 subject 不一致 → 消息进 stream 但 durable consumer 收不到

**Why must fix now**：4 进程拓扑下，错位的路由会从"看不出来的隐患"立刻变成"跨进程消息丢失"。Stage 4 单进程 + fallback 时注意不到，4 进程 + 跨主机 NATS 时第一笔决策就会暴露。

### 2.4 4 进程拓扑现状（Investigation 4）

**已有的 entry 文件**：

| 文件 | 状态 | 实际功能 |
| --- | --- | --- |
| `apps/api_gateway/main.py` | ✅ 真入口 | FastAPI lifespan + uvicorn via `scripts/start_api.py` |
| `apps/decision_engine/main.py` | ✅ 真入口 | `asyncio.run(...)` via `scripts/run_local.py` |
| `apps/market_gateway/main.py` | ⚠️ 占位符 | 仅 log "market gateway placeholder" |
| `apps/execution_engine/main.py` | ⚠️ 占位符 | 仅 log "execution engine placeholder" |
| `apps/{ai,feature,governance,portfolio,reconciliation}_service/main.py` | ⚠️ 占位符 | 同上，Stage 5 不需要单独装配（已合到 4 大 role 内） |

**docker-compose 现状**：`deploy/wsl2-dev/docker-compose.yml` 已有完整基础设施（postgres / redis / nats / loki / grafana / jaeger），**但没有 4 个 app service**。

**Dockerfile 现状**：**不存在**。需要新建 1 个统一的 multistage Dockerfile，用 `AATS_PROCESS_ROLE` env 选 role，避免维护 4 份。

**health endpoint 现状**：`/system/health` 已存在于 gateway role，其他 role 没有 HTTP server，需要**轻量 health check 机制**——直接用 docker healthcheck `python -c "..."` 探活进程内部状态即可，不需要每个进程都跑 HTTP server。

---

## 3. 重大发现 / 风险清单

### R1（高危，必须 Stage 5 处理）— 路由表错位

**位置**：`aats/bus/nats_bus.py` `DEFAULT_CRITICAL_TOPICS` / `DEFAULT_OBSERVER_TOPICS`
**影响**：6 条核心消息流 100% 走 fallback，路由表本身死代码；一旦修改不当会立即丢失关键消息
**修法**：见 5c 子任务

### R2（中危）— 单元测试 Windows Python 3.14 vs WSL Python 3.12 漂移

**现象**：WSL 跑 28 个 sqlalchemy/legacy/derivatives 单测失败，Windows 全部通过
**已采纳的应对**：在 `reference_wsl2_dev_env.md` 已记录"WSL 不跑全套单测，只跑 bus / bootstrap 子集 + 集成测试"。Stage 5 各 sub-step 验证仍以 Windows Python 3.14 单测全套为准。

### R3（中危）— 多 entry 之间的优雅停止顺序

**风险**：4 个进程的 `SIGTERM` 处理目前各写各的，docker-compose 的 stop_grace_period 默认 10s，不够 NATS durable consumer drain
**应对**：在 5d 子任务中统一通过 `apps/_common/lifecycle.py`（新增）封装 `await_runtime_shutdown(signum)`，并在 docker-compose 显式设置 `stop_grace_period: 30s`

### R4（低危）— Postgres pool size × 4 进程 = 40 连接

**现状**：`docker-compose.yml` 已设 `max_connections=200`，余量充足
**确认**：每个 process_role 各自 build_runtime 时按需创建 engine，pool_size 默认 10，4 进程 × 10 + reconciliation 后台 = 50 左右，安全

### R5（低危）— monolith 默认是否要继续工作

**约束**：用户明确"monolith 仍可用作回退"。所有 5a-5e 改动必须在 monolith（`AATS_PROCESS_ROLE` 未设 + `event_bus_backend=in_memory`）下零回归
**验证**：每个 sub-step 完成后跑 `pytest tests/unit -q` Windows 全套

---

## 4. 子任务拆分

### 5a. 关键表 OCC 补齐

**目标**：`order_states` 和 `execution_fills` 加 `row_version BIGINT NOT NULL DEFAULT 1`，UPDATE 时校验 stale；写路径加 retry。

**步骤**：

1. 写 migration `migrations/0006_postgres_order_states_row_version.sql`
   - `ALTER TABLE order_states ADD COLUMN row_version BIGINT NOT NULL DEFAULT 1`
   - 同步 `execution_fills`
   - 包含 down migration（`DROP COLUMN`）
2. 在 `aats/storage/sqlalchemy_models.py` 给两张表的 ORM model 加 `row_version: Mapped[int] = mapped_column(...)`，参照 `strategy_execution_bundles` 的 `__mapper_args__ = {"version_id_col": row_version}` 模式
3. 改 `PostgresExecutionOrderRepository.upsert_order` 让 `version_id_col` 自动递增；捕获 `StaleDataError` 并 reload + retry（最多 3 次，超出抛 `OptimisticLockExhaustedError`）
4. 同样改造 fill repository
5. 写 4 个单测：
   - 正常路径 row_version +1
   - 并发模拟：两个 session 同时 UPDATE，第二次抛 StaleDataError
   - retry 上限触发
   - migration up/down 幂等
6. 跑 Windows 全套单测验证零回归

**验收**：单测全绿；migration 在 dev Postgres 上 up/down 各跑一次成功

**回滚 tag**：`pre-stage-5a-v1`

---

### 5b. InMemoryEventBus 强类型清理

**目标**：消除 outbox 文件里的 `bus: InMemoryEventBus` 注解，改为 `bus: EventBus` 协议类型。

**步骤**：

1. 编辑 `aats/services/portfolio_service/outbox.py`：
   - 第 11 行 `from aats.bus.memory_bus import InMemoryEventBus` → `from aats.bus.base import EventBus`
   - 第 28 行 `bus: InMemoryEventBus` → `bus: EventBus`
2. 编辑 `aats/services/execution_engine/outbox.py`：同上 line 10/38
3. 跑 `pytest tests/unit -q` 验证零回归（这两个文件没有专属单测，主要是 build_runtime 集成路径）
4. grep 全仓 `InMemoryEventBus` 检查是否还有遗漏的强类型注入位置；如有，酌情加入本子任务

**验收**：grep `InMemoryEventBus` 只剩定义、单测里的 fixture、`memory_bus.py` 内部、build_runtime 中"in_memory backend 的工厂分支"（这些都正确）；其它业务路径全部用 `EventBus` 协议

**回滚 tag**：`pre-stage-5b-v1`

---

### 5c. 路由表 bug 修复（R1） 🚨

**目标**：让 `DEFAULT_CRITICAL_TOPICS` / `DEFAULT_OBSERVER_TOPICS` 用 `aats.events.topics` 模块的真实常量名，并锁死"未知 topic → 抛错"而非"silent fallback"。

**步骤**：

1. 重写 `aats/bus/nats_bus.py:47-74`：
   ```python
   from aats.events import topics

   DEFAULT_CRITICAL_TOPICS: frozenset[str] = frozenset({
       topics.ORDER_INTENTS,
       topics.ORDER_UPDATES,
       topics.FILL_EVENTS,
       topics.RISK_DECISIONS,
       topics.RECONCILIATION_REPORTS,
       topics.PORTFOLIO_SNAPSHOTS,
       topics.POSITION_TARGETS,
       topics.AI_DECISION_BRIEFS,
       topics.STRATEGY_EXECUTION_BUNDLES,
       topics.POLICY_DECISIONS,
       # ... 全集需要遍历 topics.py，下面 step 2 列规则
   })
   ```
2. 遍历 `aats/events/topics.py` 全部 ~45 个常量，按以下规则**人工归类**（不能机械分类），列入 critical / observer 二选一：
   - **critical**：决策、订单、成交、风险、对账、portfolio 余额变动、strategy 执行流（任何丢失会导致状态不一致的）
   - **observer**：health snapshot、debug、metrics、operator action 通知（丢失只影响可观测性）
   - 归类时在 nats_bus.py 的 frozenset 里**每条加注释**，写明"为什么是 critical/observer"
3. 把 `HybridBusRouting.route_for()` 的 `default_route="critical"` 改为 `default_route=None`，未知 topic 抛 `UnroutedTopicError`，**强制开发者显式归类**
4. 写新单测：
   - `test_all_topics_module_constants_are_routed`：枚举 `topics.py` 全部 module-level 常量，确保每条都在 critical 或 observer 中
   - `test_unknown_topic_raises_unrouted_topic_error`：未知 topic publish 立即抛错，不静默
   - `test_hybrid_publish_sends_to_critical_for_real_order_intents`：用真实常量 `topics.ORDER_INTENTS` 验证路由到 critical bus
5. **回归**：跑 Stage 4 的 testcontainers 集成测试 `tests/integration/test_nats_event_bus_roundtrip.py`，确保改完之后路由仍然正确（这次是真的对，不是 fallback）
6. **跨文件对照检查**：grep `bus.publish(` / `bus.publish_envelope(` 找全部 publish 点，确保 topic 参数都是 `topics.XXX` 常量而不是字符串字面量；如果发现字面量，单独列入 5c 的 follow-up

**验收**：
- 所有 topics.py 常量在路由表中显式归类（注释标明 critical/observer）
- 未知 topic publish 抛 `UnroutedTopicError`
- testcontainers 集成测试 4 个 case 仍 PASS
- Windows 单元测试零回归

**回滚 tag**：`pre-stage-5c-v1`（这是 Stage 5 风险最高的子任务，必须有独立 tag）

> **警告**：5c 必须在 5d 之前完成。一旦进入 4 进程拓扑，路由错位会立刻成为消息丢失事故，而不是隐患。

---

### 5d. 4 进程拓扑装配

**目标**：4 entry script + 1 统一 Dockerfile + docker-compose app services + 优雅停止

**步骤**：

1. **新建 `apps/_common/lifecycle.py`**：封装 SIGTERM 处理 + `await runtime.stop_background_tasks()` + 退出码语义
2. **重写占位符 entry**：
   - `apps/market_gateway/main.py`：`asyncio.run` + `build_runtime(process_role="market")` + lifecycle.run_until_signal()
   - `apps/execution_engine/main.py`：同上 role="execution"
   - `apps/decision_engine/main.py`：保留现有结构，统一通过 lifecycle helper
   - `apps/api_gateway/main.py`：保留 FastAPI lifespan，但 lifespan 内部走 build_runtime(process_role="gateway")
3. **新建 `deploy/wsl2-dev/Dockerfile`**：
   - multistage：builder 装 build deps（gcc/python3-dev），runtime 仅装 wheels
   - `ARG AATS_PROCESS_ROLE` → `ENV AATS_PROCESS_ROLE=$AATS_PROCESS_ROLE`
   - `ENTRYPOINT ["python", "-m", "apps.${AATS_PROCESS_ROLE}_entry"]` 用 sh wrapper 选 entry
   - **不**装 dev 工具（pytest 等）
4. **扩展 `deploy/wsl2-dev/docker-compose.yml`**，加 4 个 app service：
   ```yaml
   services:
     gateway:
       build: { context: ../.., dockerfile: deploy/wsl2-dev/Dockerfile, args: { AATS_PROCESS_ROLE: gateway } }
       depends_on: [postgres, nats, redis]
       environment:
         AATS_PROCESS_ROLE: gateway
         AATS_EVENT_BUS_BACKEND: hybrid
         AATS_NATS_URL: nats://nats:4222
         # ...
       healthcheck:
         test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/system/health')"]
         interval: 10s
         timeout: 3s
         retries: 3
       stop_grace_period: 30s
     market:
       # 同上，role=market，无 healthcheck HTTP，用 python -c 探活进程文件
     decision: ...
     execution: ...
   ```
5. **健康检查策略**：
   - gateway：HTTP `/system/health`
   - market/decision/execution：探活进程内部"runtime ready flag"——在 `build_runtime` 完成后写 `/tmp/aats_<role>_ready`，docker healthcheck 检查文件存在 + mtime 在 60s 内
6. **环境变量约定**：在 `deploy/wsl2-dev/.env.wsl2.template` 加新字段，注释说明
7. **写一份 `deploy/wsl2-dev/README.md` 的 Stage 5 章节**：4 进程启动顺序、停止命令、查看日志、回滚到 monolith 的命令

**验收**：
- `docker compose up -d` 4 个 service 都进入 `healthy`
- `docker compose ps` 看到 4 个 running 状态
- 任意一个 service `docker compose stop <name>` 30s 内 graceful exit（非 SIGKILL）
- monolith 模式（`unset AATS_PROCESS_ROLE && python scripts/start_api.py`）仍然能工作

**回滚 tag**：`pre-stage-5d-v1`

---

### 5e. 跨进程集成验证

**目标**：在 5d 的 4 进程拓扑上跑端到端的"决策 → NATS → 执行"路径，证明消息真的过 NATS 不走内存。

**步骤**：

1. **写一个 mock decision injector script**：`scripts/stage5_inject_test_decision.py`
   - 通过 gateway 的 `/admin/inject_decision` REST endpoint（或临时新加一个 dev-only endpoint）注入一笔 decision
   - 不直接连 NATS，让 gateway/decision_proc 走正常路径
2. **写一个 verify script**：`scripts/stage5_verify_pipeline.py`
   - 查 Postgres `order_states` 表，确认 decision_id 对应的订单已落库
   - 查 NATS JetStream 的 stream info（`nats stream info AATS_EVENTS`）确认消息已持久化
   - 检查 `execution_fills` 是否生成（mock 路径下应该立刻生成 fill）
3. **跑端到端**：
   ```bash
   docker compose up -d
   # 等 healthcheck 全绿
   docker compose exec gateway python scripts/stage5_inject_test_decision.py
   docker compose exec gateway python scripts/stage5_verify_pipeline.py
   ```
4. **崩溃恢复测试**（短窗）：
   - `docker compose stop execution`
   - 注入 3 笔 decision
   - `docker compose start execution`
   - 等 30s
   - verify script 确认 3 笔都落库（durable consumer 重连续传）
5. **观测性 sanity check**：
   - `docker compose logs gateway | grep -i error` 零 error
   - 每个 service 的 stdout 有 `runtime ready` 日志
   - Loki / Grafana 暂不接入（推迟到 Stage 6），只看 stdout

**验收**：
- 端到端路径成功
- 崩溃恢复路径成功
- 4 个 service 持续运行 ≥ 30 分钟无 OOM、无重启（短窗，**不是** Stage 9 的 30 天）
- 所有 5a-5d 的 tag 之间可以 `git reset --hard` 回滚

**回滚 tag**：`pre-stage-5e-v1`

> **说明**：5e 是验证步骤，不写新代码（除了 dev-only 的 inject/verify scripts）。这两个 script 应该在 Stage 5 完成后保留在仓库 `scripts/` 下作为冒烟测试基线。

---

## 5. 推荐执行顺序与依赖

```
5b (cleanup, 5min)
  └─→ 5c (routing fix, 高风险, 必须先于 5d)
        └─→ 5a (OCC, 与 5c 独立但建议并入此阶段保持 commit 颗粒度)
              └─→ 5d (4 进程装配, 依赖前三者已稳定)
                    └─→ 5e (集成验证)
```

### 为什么 5b 在 5c 之前

5b 是 4 行修改，几分钟完工，跑完单测就过；5c 是 Stage 5 最大风险点，需要充足的认知带宽。先把 5b 这个无脑收尾干完，避免和 5c 的高风险改动混在一个 commit 里互相干扰诊断。

### 为什么 5c 在 5a 之前

路由表 bug 是 Stage 4 集成里发现的隐患，**已知会在 4 进程下立刻爆炸**。5a 是更大的工程量但不阻塞 4 进程（OCC 没有 row_version 时只是失去保护，不是直接出错）。先解决"会爆炸的"，再做"会变好但不会爆炸的"。

### 为什么 5e 在最后

集成验证依赖前 4 个 sub-step 全部稳定。任何一个未完成都会让 5e 的失败原因混淆。

---

## 6. 回滚方案

### 6.1 安全网

进入 Stage 5 之前，先打 tag：

```bash
git tag pre-stage-5-v1
git push origin pre-stage-5-v1   # 用户决定是否推（本地够用）
```

每个 sub-step 完成后打子 tag：`pre-stage-5a-v1` / `pre-stage-5b-v1` / ... / `pre-stage-5e-v1`

### 6.2 回滚命令

| 想回到 | 命令 |
| --- | --- |
| Stage 5 之前（Stage 4 完成态） | `git reset --hard pre-stage-5-v1` |
| 5a 之前 | `git reset --hard pre-stage-5a-v1` |
| 5c 之前（路由表回到旧的 fallback 状态） | `git reset --hard pre-stage-5c-v1` |

### 6.3 docker-compose 回滚

5d 装配的 docker-compose 改动是**新增**而非**修改**已有 service。回滚只需 `docker compose down --remove-orphans` 然后 git reset 到 Stage 4 即可，不会污染数据卷。

数据库 migration 0006 是 Stage 5a 引入的，**有 down migration**。回滚 5a 时执行：

```bash
psql -h localhost -p 5432 -U aats -d aats -f migrations/0006_postgres_order_states_row_version.down.sql
```

### 6.4 monolith 回退路径

整个 Stage 5 不破坏 monolith 模式。任何时候只要：

```bash
unset AATS_PROCESS_ROLE
unset AATS_EVENT_BUS_BACKEND
python scripts/start_api.py
```

就回到 Stage 0 的单进程行为。这是用户明确的硬约束（monolith 不退化），也是 Stage 5 任何决定的兜底。

---

## 7. 验收标准

| 项 | 检查方式 | 通过条件 |
| --- | --- | --- |
| **5a OCC** | `pytest tests/unit/test_*_row_version*.py -v` | 4 个新单测全绿 |
| **5b 类型清理** | `grep -rn "InMemoryEventBus" aats/services/` | 仅出现在 fixture / 工厂分支 |
| **5c 路由修复** | `pytest tests/unit/test_nats_bus_skeleton.py tests/integration/test_nats_event_bus_roundtrip.py` | 全绿 + 新增的 `test_all_topics_module_constants_are_routed` 通过 |
| **5d 拓扑装配** | `docker compose up -d && docker compose ps` | 4 个 service 全 `healthy` |
| **5d 优雅停止** | `docker compose stop gateway` 计时 | ≤ 30s 退出，非 SIGKILL |
| **5e 端到端** | inject + verify script | 决策完整流转，Postgres + JetStream 双重落地 |
| **5e 崩溃恢复** | stop execution → 注入 → start execution | durable consumer 续传无丢失 |
| **monolith 不退化** | Windows `pytest tests/unit -q` 全套 | 单测零回归 |
| **roadmap 同步** | 编辑 `multiprocess_refactor_roadmap.md` 节点 1.3 | 标记 Stage 5 完整，列出新打的 tag |
| **memory 同步** | 编辑 `~/.claude/.../memory/project_slice_refactor.md` | 进度更新到 Stage 5 完整 |

---

## 8. 工作量预估

| Sub-step | 预估 | 备注 |
| --- | --- | --- |
| 5b | 5 min | 4 行编辑 + 跑单测 |
| 5c | 6-8h | 路由表全集分类 + 4 个新单测 + 集成回归 + 全仓 publish 点对照 |
| 5a | 6-8h | migration + ORM + repo retry + 4 个单测 |
| 5d | 8-12h | Dockerfile + lifecycle helper + 4 entry 重写 + compose 扩展 + healthcheck |
| 5e | 3-5h | inject/verify scripts + 端到端跑通 + 崩溃恢复测试 |
| **总计** | **约 25-35 小时** | 不计 debug 和"路由分类卡壳"的预期超时 |

> **不做时间承诺**。这是工程量量级估计，用来判断 sub-step 颗粒度是否合适，不是 deadline。如果 5c 路由分类比预期更难（topics.py 里有不明确归属的常量），先停下来和用户对齐。

---

## 9. 待用户确认事项

在动手之前需要用户回答以下几个决策：

1. **5c 路由表归类**：是否在分类过程中遇到模糊归属（例如 `STRATEGY_PROFILE_REJECTIONS` 是 critical 还是 observer）时，**先停下来和用户对齐**而不是擅自决定？建议是。
2. **5a row_version 范围**：当前设计只覆盖 `order_states` + `execution_fills` 两个 P0 表。是否也想顺手把 `portfolio_snapshots` / `reconciliation_state_snapshots` 一起做了（避免 Stage 9 之前还要再开一次 migration 窗口）？
3. **5d Dockerfile 基础镜像**：用 `python:3.12-slim-bookworm`（小、安全更新及时）还是 `python:3.12-bookworm`（全功能、apt 装东西更顺手）？建议 slim。
4. **5e 验证脚本位置**：放 `scripts/stage5_*.py`（一次性）还是 `tests/smoke/test_4proc_pipeline.py`（持续 CI）？建议先放 `scripts/`，Stage 9 验证窗口再考虑提升到 smoke。
5. **是否在 Stage 5 完成后立即标记 `pre-slice-refactor-v1` tag 为"过时"**？建议**不**——保留它作为最后兜底，直到 Stage 9 实盘验证窗口结束。

---

## 10. 备注

- 本设计文档**不含具体代码**，所有"步骤 X"的实现细节在进入对应 sub-step 时再写
- 文档完成后，请用户在 (a) 全部批准 / (b) 部分批准（指出哪些 sub-step 需要修改）/ (c) 全部驳回 之间选择
- 批准后立即打 `pre-stage-5-v1` tag，然后从 5b 开始执行
