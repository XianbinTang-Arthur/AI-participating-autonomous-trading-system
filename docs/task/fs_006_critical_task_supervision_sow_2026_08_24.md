# FS-006 关键后台任务监督与健康失败路径修复设计及实施范围

> 后续状态：本文件冻结 Phase 3D task-exit 范围。Phase 3K 已为七条固定周期关键循环追加成功进度 deadline/stalled 监督；现行增量设计见 [`fs_006_critical_task_progress_watchdog_sow_2026_08_24.md`](fs_006_critical_task_progress_watchdog_sow_2026_08_24.md)。下文关于 hang/last-success 尚未实现的表述只代表 Phase 3D 当时状态。

> 文档状态：Phase 3D 实施任务 / 设计冻结  
> 最后核对：2026-08-24（起始代码基线 `00b6df0f8a8d2665d6cae3e88996843767cd1f56`）  
> 当前工作区：`codex/fs-002-kill-switch-p0`，包含尚未提交的 Phase 3A/3B/3C 变更  
> 核对范围：当前 bootstrap、4 进程生命周期、FastAPI healthcheck、关键 service task 与 Phase 2 审计证据  
> 运行时边界：仅使用内存替身复现；未读取 `.env.*`，未连接真实账户、交易所、数据库、NATS 或 Redis，未部署  
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 业务目标与边界

本阶段关闭 `FS-006` 中已被确定性证明的关键任务“静默死亡”路径：关键交易或风控后台 task 因未捕获异常、被意外取消或非预期正常结束后，daemon 进程不得继续以独立心跳伪装健康；统一生命周期必须停止健康心跳、执行既有清理并返回非零退出码。FastAPI monolith/gateway 路径无法直接复用 daemon 的退出编排，因此 `/healthz` 必须在已注册关键 task 结束时返回 `503`，交由容器健康策略处理。

本阶段不声称解决永久挂起、事件循环整体阻塞、业务循环仍在运行但不再取得进展、第三方依赖持续断开后内部重连永不成功等“task 未结束但已失效”问题。这些问题需要每个 subsystem 的 last-success/lag/dependency 状态与超时预算，不能仅凭 `asyncio.Task.done()` 可靠判断。

## 2. 当前行为与根因

修复前的隔离替身复现让 `aats_execution_command_flow` 启动后立即抛出 `RuntimeError`。50ms 后观测结果为：

```text
critical_task_done=True
critical_task_error=RuntimeError
process_still_waiting=True
independent_heartbeat_ticks_after_failure=4
exit_code_after_external_stop=0
```

根因是 `run_process` 启动业务任务与独立 heartbeat 后只 `await local_stop.wait()`；`ApplicationRuntime.background_tasks` 只是停机时的 cancel/await 容器，没有 criticality 分类、完成监督或失败信号。独立心跳不读取业务 task 状态，因此 task 已失败时仍更新容器健康文件。FastAPI `/healthz` 又无条件返回 `ok`。

## 3. 模块职责与领域模型

| 模块 | 本阶段职责 |
|---|---|
| `ApplicationRuntime` | 显式注册关键 task；给出只含安全元数据的失败快照；等待任一关键 task 非预期结束；完整回收已失败 task |
| service-owned task | 通过只读属性暴露由 service 自主管理的长期 task，所有权与 stop 顺序保持不变 |
| `process_lifecycle.run_process` | 同时等待 OS stop 与关键 task 失败；关键失败优先返回非零并立即停止 heartbeat |
| FastAPI `/healthz` | 在 lifespan runtime 的关键 task 已失败时返回 `503`，不暴露异常文本 |
| 审计与文档 | 明确已修复范围、剩余 hang/lag 风险及静态/隔离验证边界 |

新增不可变 `CriticalBackgroundTaskFailure`，仅记录 `task_name`、`failure_kind` 和 `error_type`。`failure_kind` 限于 `exception`、`cancelled`、`unexpected_completion`，不保存或返回可能夹带连接串、请求参数或凭证的异常正文。

## 4. 输入/输出接口

`ApplicationRuntime` 新增内部监督接口：

- 注册已创建 task，并明确 `critical=True/False`；
- `critical_background_task_failure()`：非阻塞读取首个已结束关键 task；
- `wait_for_critical_background_task_failure()`：等待任一已注册关键 task 结束。

`run_process` 公共参数保持不变。正常 stop 返回 `0`；关键 task 发生 exception、unexpected cancellation 或 unexpected completion 时返回 `1`。`/healthz` 的成功 body 保持 `{"status":"ok","process_role":...}`；失败使用 HTTP `503`，detail 只给固定 reason、task name、failure kind 与 error type。

## 5. 数据库 schema、表、索引与约束

无数据库 schema、table、index、constraint、migration 或持久化状态变更。监督状态仅存在于当前进程内存，不把瞬态 task 对象写入 Redis、Postgres 或 event store。

## 6. 事务、一致性与并发

关键注册表只在单一 asyncio event loop 的启动/停止阶段修改；读取使用 task 的原子 `done/cancelled/exception` 状态，不跨线程共享。`run_process` 使用两个独立 waiter 竞争：外部 stop 与 critical failure。若两者同轮完成且发现 critical failure，失败语义优先，避免异常退出被误计为正常停止。

service-owned task 仍由原 service 的 `stop()` 取消和回收；runtime 只持观察引用，不改变所有权。runtime 直接创建的 task 继续由 `stop_background_tasks()` 回收。

## 7. 授权、认证与数据安全

无新增认证、授权、操作员写接口或资金动作。健康失败响应不得包含 `str(exception)`、连接 URL、请求 payload、账户信息、密钥、token 或密码；日志同样只记录 task name、failure kind 和 exception class。验证不得读取 `.env.*`。

## 8. 错误处理与幂等

- 已存在的不同 task 不得以同名覆盖关键注册项，重复注册同一 task 允许幂等；
- task 抛异常：分类为 `exception`，保留 exception class，进程返回 `1`；
- task 被意外取消：分类为 `cancelled`，进程返回 `1`；
- 长期 task 无异常返回：分类为 `unexpected_completion`，进程返回 `1`；
- 正常 stop 先胜出时保持返回 `0`，随后按既有顺序取消所有任务；
- shutdown await 已失败 task 时记录安全告警并继续回收后续资源，不让首个 task 异常截断整个清理链；
- 没有关键 task 的 runtime 只等待正常 stop，保持兼容。

## 9. 状态转换与生命周期

```text
build runtime
  -> register/start long-running tasks
  -> start independent heartbeat
  -> wait(stop_event OR critical task completion)
       -> stop_event: clean shutdown -> exit 0
       -> critical exception/cancel/completion:
            stop heartbeat -> cleanup -> exit 1

FastAPI lifespan
  -> runtime attached to app.state
  -> /healthz checks current critical failure
       -> none: 200 ok
       -> present: 503 unhealthy
```

正常关停时 heartbeat 仍保持到业务清理结束；关键失败时 heartbeat 在业务清理前停止，防止失败期间继续制造新鲜健康信号。

## 10. 缓存与性能

无缓存和外部 I/O。每个进程新增一个小型 task-name 映射、一个 critical waiter 和一个 stop waiter；规模等于固定后台任务数。正常运行不轮询、不增加定时请求，使用 asyncio task completion 通知，性能影响可忽略。

## 11. 日志、监控与审计

新增结构化生命周期事件，至少包含 process role、task name、failure kind、error type；禁止记录异常正文。`/healthz` 失败 body 提供相同安全字段，便于 Docker/运维区分健康失败原因。

本阶段没有新增 last-success timestamp、lag seconds、restart count 或 dependency-connected 指标。因此这些字段仍必须在 FS-006 后续工作中设计；不能把“task 尚未 done”当成业务健康证明。

## 12. 测试策略

新增/更新纯内存 adversarial 单测覆盖：

1. 关键 task 抛异常后 `run_process` 在有限时间内返回 `1`，无需外部 stop；
2. 关键 task 无异常结束也返回 `1`；
3. 关键 task 被意外取消也返回 `1`；
4. 非关键 task 正常结束不触发进程失败；
5. 正常 stop 保持 `0`；
6. failed task 的清理异常不会阻断其他 background task 回收；
7. 关键 task 注册名不能静默覆盖；
8. market、decision、execution 与 guard 的指定长期任务进入关键注册表；
9. `/healthz` 在无失败时保持原成功 body，在关键失败时返回 `503` 且无异常正文；
10. 修复前利用链重放后观测 process 已返回 `1`、heartbeat 已停止。

随后运行 focused tests、bootstrap/process/gateway 相关单测、Ruff 与全量 unit suite。真实 Docker/WSL2、NATS、Redis、OKX 与数据库故障注入属于运行时验证，不在本地静态/内存测试中伪造为已完成。

## 13. 迁移、回滚与兼容

无 DB migration。正常生命周期 API 兼容；行为有意收紧：此前被静默忽略的关键 task 结束现在导致非零退出或 health `503`。不提供关闭监督的生产 feature flag，因为这会重新打开已证明失败路径。

回滚代码会恢复“任务死、心跳活”的缺陷，不属于安全生产回滚方案。若新监督错误分类任务，应修正该任务的 criticality 或生命周期契约，并在隔离环境验证，不应整体禁用监督。

## 14. 配置与环境隔离

无新环境变量、配置文件或 secret。关键任务集合由代码显式声明，不能由未经验证的运行时配置降级。测试使用 fake runtime、fake tasks 和 FastAPI app state，不启动容器或外部依赖。

## 15. 代码组织与依赖

预计修改：

- `aats/bootstrap/config.py`：关键 task 模型、注册、检查、等待与清理；
- `aats/bootstrap/process_lifecycle.py`：stop/failure 竞争与 heartbeat 失败语义；
- `aats/services/market_gateway/gateway.py`、decision trigger、abort hook：只读 task 暴露；
- `apps/api_gateway/main.py`：health `503`；
- 新的 FS-006 对抗测试及现有 lifecycle/health 测试；
- `docs/code_review/README.md` 与 `audit/full_system_2026_08_24` 状态证据。

不新增第三方依赖，不重构业务循环，不改变 public trading API，不触碰 OrderState 三层持久化。

## 16. 文档、运维手册与验收标准

本阶段验收标准：

- 修复前确定性 crash 利用链不再表现为“process waiting + heartbeat alive + later exit 0”；
- exception、unexpected cancellation 与 unexpected completion 均产生安全分类并触发非零退出；
- 关键失败时 heartbeat 不继续更新；
- FastAPI healthcheck 能把已失败关键 task 映射为 `503`；
- 指定交易、行情、决策与 guard 长期 task 有显式 criticality；
- failed task 不截断后续 shutdown 清理；
- focused、相关、全量 unit 与 Ruff 通过；
- 审计状态至多更新为 `PARTIALLY REMEDIATED / HANG-LAG RUNTIME VERIFICATION OPEN`，不得直接宣告生产放行。

即使以上通过，永久挂起、业务 last-success/lag、依赖断连、容器实际 restart、关键任务身份与跨进程告警仍需 WSL2 故障注入和指标建设验证。FS-006 在这些项目完成前保持开放，且 AATS 真实资金上线保持 **NO-GO**。
