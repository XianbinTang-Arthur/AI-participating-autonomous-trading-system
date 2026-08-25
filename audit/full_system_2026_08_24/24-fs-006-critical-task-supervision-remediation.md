# 24 FS-006 关键任务监督与健康失败路径整改证据

> 后续状态：本文件冻结 Phase 3D task-exit 整改证据。Phase 3K 已为七条固定周期关键循环补充成功进度 deadline/stalled 失败路径；当前 FS-006 裁定与新增验证见 [31-fs-006-critical-task-progress-watchdog.md](31-fs-006-critical-task-progress-watchdog.md)。下文“永久 hang/last-success 未实现”只描述 Phase 3D 当时状态。

> 阶段：Phase 3D  
> 日期：2026-08-24  
> 起始 HEAD：`00b6df0f8a8d2665d6cae3e88996843767cd1f56`  
> 分支：`codex/fs-002-kill-switch-p0`  
> 工作区：包含尚未提交的 Phase 3A/3B/3C/3D 变更  
> 当前裁定：`PARTIALLY REMEDIATED / HANG-LAG RUNTIME VERIFICATION OPEN`  
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 修复范围与证据边界

本阶段修复 `FS-006` 已证明的 task-exit 假健康路径：显式列为关键的长期 asyncio task 发生未捕获异常、被意外取消或无异常提前返回后，三个 daemon 的统一生命周期不再只等待 OS stop；它会停止独立 heartbeat、执行既有清理并返回 `1`。FastAPI monolith/gateway 路径在相同状态下让 `/healthz` 返回 `503`。

本阶段没有读取 `.env.*`，没有连接真实账户、OKX、Postgres、Redis、NATS 或 Docker，没有部署。所有动态证据来自纯内存替身。永久挂起、循环仍存活但 last-success/lag 超阈值、依赖持续断连、真实容器 restart 与跨进程告警仍未验证，因此 FS-006 不能标为 CLOSED。

实施前设计与验收范围见 [`docs/task/fs_006_critical_task_supervision_sow_2026_08_24.md`](../../docs/task/fs_006_critical_task_supervision_sow_2026_08_24.md)。

## 2. 修复前确定性复现

隔离 fake execution runtime 启动名为 `aats_execution_command_flow` 的后台 task，task 在一个 event-loop tick 后抛 `RuntimeError`；未设置外部 stop event。50ms 后：

```text
{
  'critical_task_done': True,
  'critical_task_error': 'RuntimeError',
  'process_still_waiting': True,
  'independent_heartbeat_ticks_after_failure': 4
}
{'exit_code_after_external_stop': 0}
```

这直接证明原实现同时满足“业务 task 已死、进程仍等待、heartbeat 继续刷新、最终被当成正常退出”。根因位于原 `run_process` 的单一 `await local_stop.wait()` 与无 criticality 的 `background_tasks` 列表，而不是 Docker 探针命令本身。

## 3. 已实施变更

### 3.1 显式关键任务模型

`aats/bootstrap/config.py:416-660` 新增：

- `CriticalBackgroundTaskFailure`：只记录 task name、failure kind、error type；
- runtime-owned/service-owned task 注册，重复同名不同 task 失败关闭；
- `critical_background_task_failure()` 非阻塞检查；
- `wait_for_critical_background_task_failure()` 基于 task completion 阻塞，不做轮询；
- shutdown 对已失败 runtime-owned task 记录安全元数据后继续清理，不再由首个异常截断 task 回收。

failure kind 固定为：

- `exception`：task.exception 非空；
- `cancelled`：非停机阶段被取消；
- `unexpected_completion`：长期 task 无异常提前返回。

DTO、生命周期关键事件和 health body 均不包含 `str(exception)`，避免异常正文夹带连接串或业务 payload。

### 3.2 关键任务集合

`aats/bootstrap/config.py:680-860,6245-6256` 显式监督当前启动条件实际创建的：

- OKX public market stream 与 REST fallback；
- OKX private account WS；
- reconciliation refresh；
- account refresh；
- execution sync；
- execution outbox flush；
- execution command flow；
- Phase 1 shadow monitor；
- forward-trial guard monitor；
- decision feature dispatcher；
- Stage 9 abort-hook guard；
- execution guard-signal publisher。

指标 bridge、DB housekeeping、stream cache flush、profile auto-switch 与 long/short poller 没有被不加区分地升级为关键任务。这样避免 OTel 可选依赖缺失导致 metrics bridge 正常返回时把交易进程误杀。

行情、decision dispatcher 与 abort hook 继续由原 service 自主管理 stop；新增只读 task 属性只供 runtime 观察，不改变 task 所有权。

### 3.3 daemon 退出与 heartbeat

`aats/bootstrap/process_lifecycle.py:412-545` 现在同时等待：

```text
external stop event OR first critical task completion
```

若发现 critical failure，先 set `heartbeat_stop`，再记录不含异常正文的 `process_lifecycle_critical_task_failed`，最后通过 `finally` 清理 runtime 并返回 `1`。正常 stop 仍返回 `0`，且 clean shutdown 期间 heartbeat 按原契约保持到业务清理结束。

### 3.4 FastAPI health 失败

`apps/api_gateway/main.py:268-286` 保持 `/healthz` 无认证与成功 response shape 不变；若 lifespan runtime 已观测到 critical failure，则抛 HTTP `503`，detail 只含固定 reason、task name、failure kind、error type。该变更使 monolith 中的 task 失败可以由 Compose healthcheck 观察，但本阶段没有把它外推成容器实际 restart 已验证。

## 4. 修复后确定性复现

使用与修复前相同语义的 fake execution command task，不设置外部 stop：

```text
process_lifecycle_critical_task_failed
{
  'critical_task_done': True,
  'process_returned_without_external_stop': True,
  'exit_code': 1,
  'failure_kind': 'exception',
  'error_type': 'RuntimeError',
  'heartbeat_stopped': True,
  'heartbeat_ticks': 1
}
```

利用链已从“外部停止后 exit 0”翻转为“自身检测、heartbeat 停止、exit 1”。exception、unexpected completion 与 cancellation 三类均有机器断言。

## 5. 测试与静态验证

最终结果：

| 检查 | 结果 |
|---|---|
| 新增 FS-006 对抗测试 + 既有 lifecycle/health | `41 passed` |
| bootstrap/process/gateway/decision/guard 相关回归 | `178 passed` |
| `__new__` minimal runtime 兼容修复后回归 | `47 passed` |
| Ruff application（仓库要求，含 `--fix`） | `All checks passed` |
| FS-001/002/003/006 新增测试 Ruff | `All checks passed` |
| 最终全量 unit | `4170 passed, 30 skipped, 1665 warnings, 85 subtests passed in 102.85s` |

首次全量运行在第 822 项暴露 minimal runtime 未初始化新增 dataclass 字段，结果为 `821 passed, 1 failed`。随后把 shutdown clear 改为与该类现有兼容模式一致的 `getattr`，相关 47 项及全量 suite 全部通过。该中间失败属于已修复开发回归，保留记录而不隐藏。

全量 warnings 与既有审计一致，主要来自 sqlite datetime deprecation 与 LongShort poller 测试的 AsyncMock contract；本阶段没有把 warning 数量写成零。

## 6. 已验证、未验证与未知

### 静态/隔离已验证

- task exception、cancellation、normal completion 均可被分类；
- daemon 无需外部 stop 即返回 `1`；
- critical failure 路径设置 heartbeat stop；
- 非关键 task 正常完成不触发进程失败；
- 重复 task name 不静默覆盖；
- health 失败使用 `503` 且不暴露异常正文；
- failed task 不再阻断其余 runtime-owned task 的 cancel/await；
- 现有 lifecycle、health、service stop 与全量 unit 兼容。

### 运行时未验证

- WSL2 四进程容器中 task crash 后的实际 exit code、Docker health transition 与 restart policy；
- NATS/Redis/Postgres/OKX 断连下各内部 loop 的真实退出/重连行为；
- supervisor 事件到 Prometheus/Loki/外部告警的送达；
- stop_background_tasks 在真实连接 drain 下是否满足有界 RTO。

### 仍未知/未实现

- 永久 await/hang、event loop block；
- task 活着但没有业务进展的 last-success/lag；
- dependency-connected、queue depth、restart count 和 task generation；
- critical subsystem 失败时是否还需跨进程 halt，而不仅是当前进程非零退出；
- FastAPI health `503` 到容器实际 restart 的时间上界。

## 7. 当前裁定与上线影响

`FS-006` 的“critical task 已结束但独立 heartbeat 永久绿”代码路径已收口，但原 finding 的完整验收同时要求 crash、hang、lag、dependency disconnect 与生产等价容器行为。当前状态因此是：

```text
PARTIALLY REMEDIATED / HANG-LAG RUNTIME VERIFICATION OPEN
```

系统门禁 `G4` 从原始 `FAIL` 更新为 `PARTIAL / 未放行`。要关闭 FS-006，至少仍需：

1. 为每个 critical subsystem 建 last-success/lag/dependency 状态和明确超时预算；
2. 对永久 hang、依赖断连、event-loop stall 做隔离故障注入；
3. 在 WSL2 生产等价四进程 Compose 验证 nonzero、health、restart、告警与停机上界；
4. 独立 reviewer 核对关键任务清单与误杀/漏监控风险；
5. 将 supervisor 状态纳入 FS-007 trading-readiness packet。

Phase 3D 不构成部署或真实资金授权。**REAL-MONEY PRODUCTION: NO-GO**。
