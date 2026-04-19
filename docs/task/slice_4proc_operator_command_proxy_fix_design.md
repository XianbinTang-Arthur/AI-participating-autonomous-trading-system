# Slice: 4 进程 Operator Command NATS 代理修复设计

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


**状态**：待批准
**作者**：Claude (Opus 4.6)
**日期**：2026-04-08
**前置 slice**：`slice_nats_jetstream_capacity_fix_design.md`（WP4 已落地）
**触发病状**：用户从 web UI 点 `/system/rebaseline` 返回 500；DIRECT 走 docker exec
`curl -X POST http://localhost:8000/system/rebaseline` 返回
`{"detail":"Internal Server Error"}`；gateway 日志 `AttributeError: 'NoneType'
object has no attribute 'state'` 指向
`reconciliation_system_queries.py:314  portfolio_state=self.owner.runtime.portfolio_service.state`。

## 1. 背景与病根

### 1.1 病状

4 进程 docker-compose 拓扑下用户触发以下两条 operator 路径任一都会 500：

1. `POST /system/rebaseline` — `reconciliation_system_queries.rebaseline()` 在
   line 314/327/328 连续访问 `runtime.portfolio_service.state` /
   `runtime.portfolio_service.bootstrap_snapshot(...)` /
   `runtime.reconciliation_service.validate_now(...)`，三个字段在 gateway
   role 全部是 None。
2. `POST /system/resume` — 同文件 line 519
   `runtime.reconciliation_service.validate_now("resume_check:...")` 同样 NPE。

### 1.2 根因链

| 层 | 证据 | 结论 |
|---|---|---|
| **L1 HTTP mount** | `apps/api_gateway/main.py` 只在 gateway role 启 FastAPI + include routes | `/system/rebaseline`、`/system/resume` 只能从 gateway 进入 |
| **L2 Slice 门控** | `bootstrap/config.py:2815-2818` `_SLICE_REQUIRED_ROLES["portfolio"] = {None, monolith, execution}`、`["reconciliation"] = {None, monolith, execution}`  | `portfolio_service` / `reconciliation_service` 只在 execution role 装配；gateway role 下对应字段留 None |
| **L3 业务代码** | `reconciliation_system_queries.py:314/327/328` 直接访问两个 None slice 字段 | NPE → FastAPI 500 → 操作路径彻底不可用 |
| **L4 服务降级效果** | 人工 NATS + Redis 注 kill_switch 可绕开部分；但 `review_required_blocks_resume` → `operator_rebaseline_required` blocker 必须走 rebaseline 消费掉 `external_manual_activity_detected` finding | 没有合法路径触发 rebaseline → 无法清 blocker → 无法 resume → decision 不产 order_intent → 无法下单 |

### 1.3 根因总结

4 进程切片化阶段把 `portfolio_service` 和 `reconciliation_service` 从 gateway
移到 execution，但 `/system/rebaseline`、`/system/resume` 这两条依赖这些
service 的 HTTP 路径没有同步适配，留下了"有 endpoint、没后端"的断链。

这是一次**架构回归**而不是一次新 bug：在单进程 / monolith role 下业务代码继续
正确；只有在 4 进程 gateway role 下才炸。

## 2. 目标与非目标

### 2.1 目标（本 slice 必做）

- **T1**：`POST /system/rebaseline` 在 4 进程 gateway role 下恢复可用，HTTP
  响应体与 monolith 路径语义一致（返回 `report` + `recovery` + `baseline`）。
- **T2**：`POST /system/resume` 在 4 进程 gateway role 下恢复可用。
- **T3**：保持 slice 架构边界：portfolio_service/reconciliation_service 仍然
  只在 execution 进程装配；gateway 不直接访问这两个 service。
- **T4**：修复方式必须对 monolith / execution role 完全透明，不回归单进程路径。
- **T5**：引入的新通信机制具有足够通用性，后续其他"依赖 execution-only service
  的 operator endpoint"（如 resolve_stuck_submission 部分路径）可复用。
- **T6**：超时 / 错误传播 / correlation 机制有 test 保护，避免 gateway 卡死或
  把 execution 端错误吞掉。

### 2.2 非目标

- **N1**：**不**在 gateway 重建 portfolio_service 或 reconciliation_service
  （这会让 slice 架构"漏水"，两进程独立写同一份内存状态会 diverge）。
- **N2**：**不**把 FastAPI 搬到 execution 进程 / 添加第二个 HTTP 端口（设计
  意图是 gateway 承担所有对外入口）。
- **N3**：**不**在本 slice 修 `flat_signal_hold` guardrail 或 strategy signal
  强度问题（这是独立的业务层问题，rebaseline fix 不解决也不需要解决）。
- **N4**：**不**修改现有的 `OperatorActionRecord` 持久化流程或 audit 事件结构。
- **N5**：**不**扩大到 `halt()` 方法（halt 只依赖 kill_switch，已经跨进程 NATS
  同步，不需要代理）。

## 3. 方案对比

| 方案 | 描述 | Pros | Cons | 判定 |
|---|---|---|---|---|
| **A. Gateway 内直接重建 portfolio/reconciliation slice** | 放宽 `_SLICE_REQUIRED_ROLES` 让 gateway 也装 | 无需新通信机制 | 两份 in-memory state 跨进程分叉；违反 slice 架构；风险面极大 | ❌ 拒绝 |
| **B. Gateway 只做 DB 写 + 等 execution 周期刷新** | gateway 直接用 `baseline_import_service`（在 shared slice）写 event_store，跳过 in-memory 同步，等 execution 的 `_refresh_reconciliation_loop` 60s 后自然触达 | 改动面最小 ~50 行 | UX 降级（HTTP 响应拿不到 fresh report）；resume() 没法走这条路（validate_now 的报告需要立即用于 resume_check） | ⚠️ 部分可用，不推荐 |
| **C. NATS 请求-响应代理**（本文选定） | 新增 `system.operator_command_requests` / `..._responses` 两个 topic + `OperatorCommandClient`（gateway 端）+ `OperatorCommandWorker`（execution 端），gateway 发请求 → 按 correlation_id 等响应 → 返回 HTTP | 保持 slice 边界；通用性强；UX 与 monolith 一致 | 需要新文件 + 新机制；必须处理 timeout、correlation 错位、错误传播 | ✅ 采纳 |
| **D. 把 `/system/rebaseline` HTTP endpoint 搬到 execution 上** | 在 execution 进程 mount 一个 minimal FastAPI | 业务代码零改动 | execution 进程新开第二个 HTTP port，docker-compose 多一对映射；两处 HTTP 入口违反 gateway "单入口" 目标 N2 | ❌ 拒绝 |

**选定 C：NATS 请求-响应代理。**

## 4. 架构设计

### 4.1 通信模型

```
                ┌─────────────────────┐
                │  gateway (FastAPI)  │
                └──────────┬──────────┘
                           │ POST /system/rebaseline
                           ▼
         reconciliation_system_queries.rebaseline()
                           │ (if portfolio_service is None)
                           ▼
              OperatorCommandClient.invoke("rebaseline", ...)
                           │ publish(OPERATOR_COMMAND_REQUESTS,
                           │         correlation_id=X, command="rebaseline",
                           │         payload={reason, actor_role, ...})
                           ▼
                    NATS JetStream
                           │
                           ▼
                 OperatorCommandWorker
                           │ (subscribed on execution process)
                           │ dispatch via command name
                           ▼
         _local_rebaseline(payload) → monolith path unchanged
                           │ returns dict (report, baseline, ...)
                           ▼
              publish(OPERATOR_COMMAND_RESPONSES,
                      correlation_id=X, success=True, result=...)
                           ▼
                    NATS JetStream
                           │
                           ▼
              OperatorCommandClient._dispatch_response()
                           │ resolve future[correlation_id]
                           ▼
              return to HTTP handler
                           │
                           ▼
             gateway sends HTTP 200 + dict
```

### 4.2 Topic 归属

- `OPERATOR_COMMAND_REQUESTS = "system.operator_command_requests"`
- `OPERATOR_COMMAND_RESPONSES = "system.operator_command_responses"`

两条都归 **critical**（DEFAULT_CRITICAL_TOPICS），因为丢包会让 operator 操作
彻底失败（HTTP 超时、系统卡在 blocker 里无法 resume）。落 `AATS_EVENTS` stream
（长保留 stream），与其他 operator_actions 一类保持归属一致。

### 4.3 Schema

新文件 `aats/schemas/operator_command.py`：

```python
class OperatorCommandRequest(SchemaBase):
    correlation_id: str        # new_id("opcmd")
    command: Literal["rebaseline", "resume"]
    payload: dict[str, Any]    # 命令相关参数（reason, actor_*, ...）
    requested_at: datetime
    requested_by_role: str     # source process role（gateway）

class OperatorCommandResponse(SchemaBase):
    correlation_id: str        # 对应 Request 的 correlation_id
    success: bool
    result: dict[str, Any] | None    # success=True 时的业务返回
    error_type: str | None           # success=False 时的 exception 类名
    error_message: str | None        # success=False 时的 exception str
    responded_at: datetime
    responder_role: str        # source process role（execution）
```

### 4.4 OperatorCommandClient（gateway 侧）

```python
class OperatorCommandClient:
    def __init__(self, bus, *, process_role, logger, timeout_seconds=30.0)
    async def bootstrap(self) -> None:         # 订阅 response topic
    async def invoke(self, command, payload)   # 发请求 + await future
    async def _handle_response(message)        # response subscriber callback
    async def stop(self) -> None
```

关键设计点：

1. `invoke()` 在调用前先在内部 `dict[correlation_id, Future]` 里注册 future，
   然后 publish 请求，然后 `asyncio.wait_for(future, timeout)`。超时后清理
   dict entry 并抛 `OperatorCommandTimeoutError`。
2. `_handle_response()` 按 correlation_id 取 future；未知 id 记 warning 后
   丢弃（不抛，避免订阅 handler 异常把 NATS client 搞死）。
3. `bootstrap()` 在 gateway runtime build 时即订阅 response topic，确保
   subscribe 先于任何 invoke 发生。

### 4.5 OperatorCommandWorker（execution 侧）

```python
class OperatorCommandWorker:
    def __init__(self, bus, owner_runtime, *, process_role, logger)
    async def bootstrap(self) -> None          # 订阅 request topic
    async def _handle_request(message)         # request subscriber callback
    async def _dispatch(command, payload)      # 本地业务调用
    async def stop(self) -> None
```

关键设计点：

1. `_dispatch()` 内部根据 `command` 路由到 `OperatorQueryService` 上已存在的
   方法（`rebaseline`、`resume`），**直接复用 monolith 路径的业务逻辑**。
   因为 worker 跑在 execution 进程，所有 runtime field 都非 None。
2. 业务调用 throw 时，包成 `OperatorCommandResponse(success=False)` 发回，
   不让异常冒到订阅 handler 之外。
3. 发送端 source_role 标为 `execution`，client 收到后可以在日志里展示跨进程
   trace。

### 4.6 `reconciliation_system_queries.py` 修改

```python
async def rebaseline(self, *, reason, actor_role, actor_identity, auth_source):
    runtime = self.owner.runtime
    if runtime.portfolio_service is None or runtime.reconciliation_service is None:
        # 4-proc gateway path：委派给 execution
        client = runtime.operator_command_client
        if client is None:
            raise RuntimeError(
                "rebaseline_requires_operator_command_client: "
                "gateway runtime missing client wiring"
            )
        return await client.invoke(
            command="rebaseline",
            payload={
                "reason": reason,
                "actor_role": actor_role,
                "actor_identity": actor_identity,
                "auth_source": auth_source,
            },
        )
    # monolith / execution path：原逻辑不变
    ... 原 260~421 行代码 ...
```

`resume()` 的适配镜像同样的 if 分支。`halt()` 不修改（只用 kill_switch，已跨进程）。

### 4.7 `bootstrap/config.py` 修改

1. `ApplicationRuntime` dataclass 新增两个字段：
   ```python
   operator_command_client: OperatorCommandClient | None = None
   operator_command_worker: OperatorCommandWorker | None = None
   ```
2. `build_runtime()` 末尾，在 runtime 实例化后：
   - gateway role → 构造 client，bootstrap 订阅 response topic
   - execution role → 构造 worker，bootstrap 订阅 request topic
   - monolith role → 两者都不建（走原 in-memory 路径）
3. `start_background_tasks()` 不需要额外 task（subscribe callback 是 NATS 内部
   task）。`stop_background_tasks()` 调 client.stop() / worker.stop()。

### 4.8 `baseline_import.py` 辅助修改

`_persist_baseline()` 的 `portfolio_state` 参数改为 `PortfolioState | None`，
None 时跳过 `portfolio_state.load_exchange_snapshot(...)` 调用。

本修改本 slice 不严格需要（gateway 不直接调 baseline_import），但：
1. 明确表达"in-memory portfolio 更新是 in-proc 可选项"
2. 让未来的 execution-side worker 能在 portfolio_service 已注入 bootstrap_snapshot
   的情况下 skip 双重更新
3. 降低下次有人从 gateway 直接调 baseline_import 时重新踩坑的概率

## 5. 工作包拆分

| WP | 内容 | 文件 | 估算 |
|---|---|---|---|
| **WP1** | Topic 定义 + 路由归属 + stream 容量分配 | `aats/events/topics.py`、`aats/bus/nats_bus.py` | ~10 行 |
| **WP2** | OperatorCommand schema | `aats/schemas/operator_command.py`（新文件） | ~50 行 |
| **WP3** | OperatorCommandClient + Worker 实现 | `aats/services/operator/command_bridge.py`（新文件） | ~250 行 |
| **WP4** | `reconciliation_system_queries.py` rebaseline/resume 加 gateway 分支 | `.../reconciliation_system_queries.py` | ~40 行 |
| **WP5** | `baseline_import.py` portfolio_state 设 Optional | `.../baseline_import.py` | ~10 行 |
| **WP6** | `bootstrap/config.py` 装配 client/worker + dataclass 字段 + start/stop 钩子 | `aats/bootstrap/config.py` | ~60 行 |
| **WP7** | 单元测试 | `tests/unit/test_operator_command_bridge.py`（新） | ~200 行 |
| **WP8** | 集成测试 + 冷烟断言 | `tests/integration/test_4proc_operator_commands.py`（新） | ~150 行 |

## 6. 验证方案

### 6.1 冷烟断言（必须通过才算修复）

**S1**：4 进程 docker-compose 启动后 `POST /system/rebaseline` 返回 200，
响应体包含 `report`（review_required=false）、`baseline`（baseline_status
="rebaseline_completed"）、`recovery.recovery_state ∈ {"rebaseline_completed",
"normal_operation"}`。

**S2**：`POST /system/resume` 返回 200，`runnable=true`，`blockers=[]`。

**S3**：`system_mode.execution_blocked=false`，`kill_switch.halted=false`。

**S4**：decision 进程日志出现 `decision_cycle_completed` 且 `target_position_qty`
非零（前提条件：strategy signal 不是 flat_hold——这部分在 N3 范围外）。

**S5**：如果 S4 的 decision 能产出 non-flat target，则 execution 进程日志出现
`order_intent_submitted` 且有对应 `order.submitted` NATS 事件写到 event_store。

### 6.2 单元测试覆盖

- `test_client_timeout_raises` — response 没来时超时抛
- `test_client_dispatches_by_correlation_id` — 乱序响应、未知 id 各自正确处理
- `test_worker_success_path` — 成功命令返回 success=True + result
- `test_worker_failure_wraps_exception` — 业务抛错时包成 success=False
- `test_worker_unknown_command_rejected` — 未注册命令返回 error
- `test_rebaseline_gateway_delegates_to_client` — gateway role 下 rebaseline()
  走 client.invoke，不触发 portfolio_service 访问

### 6.3 回归保护

- monolith role 下 `test_operator_api.py` 既有用例全部保持通过
- 新增 `test_rebaseline_monolith_path_unchanged` 显式断言 monolith 路径里
  `client is None and worker is None` 且走 in-proc 分支

## 7. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| Response 丢包导致 gateway 卡超时 | HTTP 客户端等 30s 后拿到 timeout error | 设置合理 timeout；记录 correlation_id 方便人工排查；超时后不做 retry（避免重复 halt） |
| Client 和 Worker 订阅延迟 > publish | 响应到达时 client subscribe 未就绪 → future 永远不被 resolve | `bootstrap()` 严格在 `build_runtime` 返回前完成 subscribe；单测 assert subscribe 先于 invoke |
| Worker 并发处理多个 request 时 race | 两个 rebaseline 同时跑可能导致 kill_switch/baseline 乱序 | Worker 内部用 `asyncio.Lock()` 串行化所有 dispatch（牺牲吞吐，换正确性） |
| Execution 进程 crash 时 gateway 已发 request 但响应永远不来 | HTTP 卡 30s 然后 error | timeout 控制最坏情况；下次重试即可（rebaseline 是幂等的：读当前 OKX 状态 + 写新 baseline） |
| JetStream stream 满导致 publish 失败 | HTTP 立即 error，符合"failed fast" | 已有 Slice nats-capacity 的 8 GB 容量兜底；本 slice 的 2 条 topic 写入量极低（operator 手动触发） |

## 8. 依赖关系

- **前置**：`slice_nats_jetstream_capacity_fix_design.md` WP4 已落地（server
  max_file_store=8GB，AATS_EVENTS 4GB）— 本 slice 两条新 topic 吃不了多少容量。
- **后置**：后续可以用同一套 bridge 把 `resolve_stuck_submission` / 其他依赖
  execution 侧 runtime 的 operator 调用也代理过去（复用 WP3 的 client/worker）。

---

**Commit 策略**：按 WP 顺序一次 commit，每条 commit 绿色 PR 可独立部署。WP1-WP3
作为 foundational commits（新 topic / schema / bridge 模块），WP4-WP6 作为
wiring commits（改业务代码 + 装配），WP7-WP8 作为 test commits。最后一条
commit 附上本 design doc 链接和 RUNBOOK 更新（§9.11）。
