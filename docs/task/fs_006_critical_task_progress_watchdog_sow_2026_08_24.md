# FS-006 固定周期关键任务成功进度看门狗设计及实施范围

> 文档状态：Phase 3K 实施任务 / 设计冻结  
> 最后核对：2026-08-24（起始代码基线 `00b6df0f8a8d2665d6cae3e88996843767cd1f56`）  
> 实施起点工作区：`codex/fs-002-kill-switch-p0`，包含尚未提交的 Phase 3A–3J 整改；本文件定义 Phase 3K  
> 核对范围：当前 `ApplicationRuntime`、统一 daemon 生命周期、FastAPI `/healthz`、执行/账户/对账/风控固定周期循环及既有 FS-006 证据  
> 运行时边界：仅允许纯内存与静态验证；不读取 `.env.*`，不连接真实账户、交易所、Postgres、Redis、NATS、Docker 或 WSL2 运行栈  
> 目标裁定：**PARTIALLY REMEDIATED / EVENT-DRIVEN AND TARGET RUNTIME VERIFICATION OPEN**  
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 业务目标与边界

Phase 3D 已阻断关键 task 抛异常、被取消或提前返回后进程仍持续健康的路径，但无法识别 task 仍为 pending、内部业务调用却永久等待或长期没有成功周期的情况。本阶段为有确定周期和成功完成语义的关键循环建立进程内成功进度契约：启动后超过明确预算仍未成功完成一轮时，将该 task 分类为 `stalled`，daemon 停止独立 heartbeat、清理并返回非零，FastAPI `/healthz` 返回 `503`。

本阶段只覆盖可以客观定义“成功周期”的七条 runtime-owned 固定周期任务：账户 REST 刷新、交易所执行同步、对账刷新、execution outbox flush、execution command flow、Phase 1 shadow monitor、trial guard monitor。OKX public/private WebSocket、decision dispatcher、abort hook、guard-signal publisher和 market REST fallback 暂不纳入同一超时模型：它们分别具有事件驱动、内部 fail-soft、独立 ping/freshness 或 service-owned 生命周期，若只凭“没有业务消息”判断停滞会产生误杀。事件循环整体阻塞也无法由同一 event loop 内的看门狗可靠发现。

## 2. 当前行为与根因

现有 `CriticalBackgroundTaskFailure` 只检查 `asyncio.Task.done()`。七条固定周期循环都在 `while True` 中等待业务操作，捕获普通异常后记录失败并继续；若某个 await 永久不返回，task 会一直保持 pending，关键 task waiter 也会永久等待 task completion。若依赖持续抛错，task 虽持续循环却没有成功业务周期，当前 supervisor 同样不会升级进程失败。

根因是关键任务注册表只有 task identity/criticality，没有每条任务的启动宽限、最大无成功进度时间和最后成功进度单调时钟。`background_failure_messages` 面向诊断与 event store，不提供统一、有界的进程退出判定。

## 3. 模块职责与领域模型

| 模块 | 本阶段职责 |
|---|---|
| `ApplicationRuntime` | 保存可选的关键任务进度预算；推进成功 checkpoint；把超时任务映射为安全的 `stalled` 失败；让 waiter 在最近 deadline 到期时主动醒来 |
| 七条固定周期循环 | 仅在该轮核心操作和既有 recovery 记录均成功完成后推进 checkpoint；异常、永久 await 或持续失败均不推进 |
| `process_lifecycle.run_process` | 复用既有 critical failure 路径，记录安全元数据、停止 heartbeat、清理并返回 `1` |
| FastAPI `/healthz` | 复用既有 critical failure 查询，在 stalled 时返回 `503` |
| 测试与审计 | 证明 pending task 超时、成功 checkpoint 延长 deadline、无预算任务不被误判及七条任务均声明预算 |

新增内部可变状态 `CriticalBackgroundTaskProgress`，保存 `timeout_seconds` 与 `last_success_monotonic`。单调时间只用于同进程 deadline 计算，不持久化、不跨进程比较。`CriticalBackgroundTaskFailure.failure_kind` 扩展为 `stalled`，并可携带经过舍入的 `stalled_seconds` 与 `timeout_seconds`，不携带异常正文或业务数据。

## 4. 输入与输出接口

`register_background_task()` 和 `create_background_task()` 增加可选内部参数 `progress_timeout_seconds`。只有 `critical=True` 时允许设置；值必须是有限正数。未设置预算的关键任务保持 Phase 3D 的 task-exit-only 监督语义。

`mark_critical_background_task_success(task_name)` 推进指定任务的单调时间 checkpoint；未注册或无预算的名称属于代码契约错误并抛固定异常。`critical_background_task_failure()` 在 task 已结束时保持原分类优先，否则判断成功进度 deadline。`wait_for_critical_background_task_failure()` 同时等待 task completion 与最近进度 deadline。

公共 HTTP 成功契约不变。失败 detail 保持固定 reason，并新增可选、安全的 `stalled_seconds`、`timeout_seconds`；不会返回底层 exception message。

## 5. 数据库 Schema、表、索引与约束

无数据库 schema、表、索引、约束、migration 或数据回填。成功进度仅存在于进程内存，进程重启后从新一代 runtime 注册时重新建立启动宽限。不会写入 Postgres、Redis、NATS 或 event store。

## 6. 事务、一致性与并发

注册、checkpoint 和读取均在同一 asyncio event loop 中完成，不跨线程共享。使用 `time.monotonic()` 避免系统时钟回拨或 NTP 校时造成 deadline 失真。task completion 分类优先于 stalled 分类，避免已抛错的 task 被模糊成超时。

成功 checkpoint 只有在一轮核心业务调用以及既有 recovery 状态更新完成后才提交；因此它相当于一次轻量的“该周期完成”提交点。进度状态不参与资金或订单事务，不改变数据库原子性。

## 7. 授权、认证与数据安全

无新增认证、授权、operator mutation、交易请求或资金动作。监督 DTO、日志和 health body 只允许 task name、failure kind、error type、超时预算和停滞时长；禁止异常正文、连接 URL、账户信息、payload、secret、token 和密码。验证不读取 `.env.*`。

## 8. 错误处理与幂等

- 预算必须是有限正数；NaN、Infinity、零或负数在注册时失败；
- 非关键任务不得声明进度预算，防止配置语义自相矛盾；
- 同一 task 的重复成功 checkpoint 幂等地把 deadline 向后推进；
- 一轮业务操作失败或 failure/recovery 记录自身失败时不推进成功时间；
- pending task 超过 deadline 后稳定返回同一 `stalled` 分类，直到任务恢复并显式 checkpoint、结束或进程退出；
- 正常 shutdown 不改变既有 task cancel/await 顺序；
- 未配置进度预算的事件驱动关键任务不做静默时间推断。

## 9. 状态转换与生命周期

```text
register critical periodic task
  -> initialize startup grace at monotonic now
  -> task pending
       -> successful cycle: checkpoint -> reset deadline
       -> transient failure: no checkpoint -> retry within remaining budget
       -> permanent await / persistent failure: deadline expires -> stalled
       -> exception/cancel/completion: existing Phase 3D classification

stalled
  -> daemon: heartbeat stop -> cleanup -> exit 1
  -> FastAPI: /healthz -> 503
```

首轮成功也必须在启动预算内完成。预算取 `max(60s, 3 × 正常周期)`；reconciliation 以其实际计算出的周期为基准，outbox 因指数退避上限 30 秒采用 90 秒。该预算允许短暂抖动，同时把分钟级无进展从“无限未知”收敛为有界失败。

## 10. 缓存与性能

每条纳管任务增加两个 float 和一次成功周期字典更新；任务总数固定，开销可忽略。waiter 使用最近 deadline 作为 `asyncio.wait(timeout=...)`，没有高频轮询。health 查询只遍历固定关键任务集合。

## 11. 日志、监控与审计

复用 `process_lifecycle_critical_task_failed`，在 `failure_kind=stalled` 时追加安全的 `stalled_seconds` 和 `timeout_seconds`。FastAPI health detail 同步这些字段。审计文档必须区分：代码/纯内存已验证的固定周期进度监督、未覆盖的事件驱动任务、无法在同 event loop 内检测的整体阻塞，以及未执行的目标 WSL2/Compose 故障注入。

## 12. 测试策略

新增/扩展纯内存对抗测试：

1. pending critical task 在极短测试预算到期后被分类为 `stalled`；
2. daemon 无需外部 stop 返回 `1` 且 heartbeat 停止；
3. FastAPI `/healthz` 对 stalled 返回 `503` 且无敏感正文；
4. deadline 前 checkpoint 会延长等待，不被旧 deadline 误判；
5. 未配置预算的 pending 事件驱动 task 不被时间误判；
6. 非法预算和非关键预算失败关闭；
7. 七条固定周期任务的注册声明含显式预算；
8. 单个循环只在成功路径推进 checkpoint，异常路径不推进；
9. 既有 exception/cancel/completion 分类保持不变。

完成后运行 FS-006 focused、生命周期/health/相关循环回归、Ruff 和全量 unit suite。真实 Docker、NATS、Redis、Postgres、OKX 和 WSL2 不在本阶段执行。

## 13. 迁移、回滚与兼容

无数据迁移。内部方法参数是 additive change，未声明预算的现有调用保持兼容。行为有意收紧：七条固定周期关键任务连续无成功周期达到预算后将退出/不健康，而不再无限假健康。

不提供生产关闭开关，因为开关会重新打开已证明的 hang/持续失败路径。若目标环境出现误杀，应根据该 subsystem 的真实 SLO 调整代码中预算公式并补充证据，不应全局禁用监督。回滚此阶段只可作为隔离诊断手段，不构成安全生产回滚方案。

## 14. 配置与环境隔离

本阶段不新增环境变量或用户可调配置，避免操作员无意把安全 deadline 配成无限大。预算从已校验的现有循环 interval 派生，并设 60 秒硬下限。测试通过显式注册参数使用毫秒级预算，不改生产默认配置。

## 15. 代码组织与依赖

预计修改：

- `aats/bootstrap/config.py`：进度模型、注册校验、checkpoint、deadline 检测和七条循环接入；
- `aats/bootstrap/process_lifecycle.py`：stalled 安全元数据日志；
- `apps/api_gateway/main.py`：stalled 安全元数据 health 输出；
- `tests/unit/test_fs006_critical_task_supervision.py`：hang、checkpoint、预算与 health 对抗测试；
- 当前文档索引与 `audit/full_system_2026_08_24` 追加 Phase 3K 证据。

不新增第三方依赖，不改变 public trading API，不重构 service-owned WebSocket/dispatcher，不触碰 OrderState 的 Postgres/JSON/Redis 三层持久化。

## 16. 文档、运维手册、部署与验收标准

验收标准：

- 七条固定周期任务均有明确成功 checkpoint 和进度预算；
- pending/hang 与持续失败在预算后映射为 `stalled`；
- daemon 复用既有失败路径停止 heartbeat、清理并返回 `1`；
- FastAPI health 返回 `503`，日志/响应不包含异常正文；
- checkpoint 会正确延长 deadline，未纳管事件驱动任务不会因安静期误杀；
- focused、相关、Ruff、全量 unit 和文档链接检查通过；
- 审计状态至多更新为 `PARTIALLY REMEDIATED / EVENT-DRIVEN AND TARGET RUNTIME VERIFICATION OPEN`。

本阶段不部署。要关闭 FS-006，仍需为事件驱动任务建立各自的连接/freshness/queue-lag 契约，在外部 supervisor 层验证 event-loop stall，并在生产等价 WSL2 四进程栈完成 hang、依赖断连、restart、告警和停机上界故障注入。任何以上未完成项都不得被文档表述为已验证，真实资金上线继续 **NO-GO**。
