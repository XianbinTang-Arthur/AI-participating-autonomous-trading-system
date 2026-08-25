# 31 FS-006 固定周期关键任务成功进度看门狗整改证据

> 阶段：Phase 3K  
> 日期：2026-08-24  
> 起始 HEAD：`00b6df0f8a8d2665d6cae3e88996843767cd1f56`  
> 分支：`codex/fs-002-kill-switch-p0`  
> 工作区：包含尚未提交的 Phase 3A–3K 变更  
> 当前裁定：`PARTIALLY REMEDIATED / EVENT-DRIVEN AND TARGET RUNTIME VERIFICATION OPEN`  
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 本阶段修复范围

Phase 3D 只检测关键 task 的 exception、unexpected cancellation 与 unexpected completion。本阶段补上可在当前架构内可靠定义的一类“task 活着但没有成功进展”：七条 runtime-owned 固定周期关键循环注册成功进度预算，并只在一次核心业务周期及既有 recovery 记录成功完成后推进单调时钟 checkpoint。pending await 永久不返回，或依赖持续失败导致一直没有成功周期时，supervisor 在预算到期后返回 `failure_kind=stalled`。

纳管任务：

- `aats_reconciliation_refresh`；
- `aats_okx_account_refresh`；
- `aats_okx_execution_sync`；
- `aats_execution_outbox_flush`；
- `aats_execution_command_flow`；
- `aats_phase1_shadow_monitor`；
- `aats_trial_guard_monitor`。

设计与验收边界见 [`docs/task/fs_006_critical_task_progress_watchdog_sow_2026_08_24.md`](../../docs/task/fs_006_critical_task_progress_watchdog_sow_2026_08_24.md)。

## 2. 为什么不能覆盖全部关键任务

OKX public/private WebSocket、decision dispatcher、abort hook、guard-signal publisher 与 market REST fallback 的健康语义不等同于“固定周期内必须收到业务消息”。WebSocket 在安静期仍可通过 ping/pong 和连接状态保持健康；decision dispatcher 可以合法等待 queue；若强行用统一无消息阈值会误杀。本阶段因此不把这些任务伪装成已解决，而是保留其各自 connection/freshness/queue-lag 契约和目标运行验证缺口。

同一 event loop 内的 watcher 也不能可靠检测整个 loop 被同步代码完全阻塞；该故障需要容器外部 heartbeat/lag supervisor。FS-006 因此仍未 CLOSED。

## 3. 代码变更

### 3.1 进度模型与注册约束

`ApplicationRuntime` 新增 `CriticalBackgroundTaskProgress`，只保存：

- 有限正数 `timeout_seconds`；
- `time.monotonic()` 的 `last_success_monotonic`。

`register_background_task()` / `create_background_task()` 接受可选 `progress_timeout_seconds`。非关键任务声明预算、NaN、Infinity、零或负数均在 task ownership/registry 变更前失败。未设置预算的关键任务保持 task-exit-only 语义。

`mark_critical_background_task_success()` 只允许推进已注册的进度任务，避免拼错名称静默失去监督。shutdown 同步清空 task 与 progress registry。

### 3.2 stall 分类和有界等待

`CriticalBackgroundTaskFailure.failure_kind` 扩展 `stalled`，并只附带经过舍入的 `stalled_seconds` 与 `timeout_seconds`。task 已结束时仍优先返回 exception/cancel/completion，不会用超时掩盖真实退出原因。

critical waiter 不做高频轮询：它同时等待任一 task completion 与最近进度 deadline。checkpoint 在 waiter 睡眠期间更新时，旧 deadline 醒来后会重新计算新 deadline，不会误报。

### 3.3 失败收敛

daemon 复用 Phase 3D 路径：stalled 后停止独立 heartbeat、执行既有 cleanup 并返回 `1`。生命周期结构化日志增加安全的停滞时长和预算。FastAPI `/healthz` 返回 `503`，detail 只含固定 reason、task name、failure kind、error type 与可选超时元数据，不包含异常正文或业务 payload。

### 3.4 预算

固定周期预算为 `max(60s, 3 × interval)`：

| 任务 | 默认或派生周期 | 成功进度预算 |
|---|---:|---:|
| account refresh | 15s | 60s |
| execution sync | 5s | 60s |
| reconciliation | `min(stale/2, 60s)`，默认 60s | 默认 180s |
| outbox flush | 失败退避上限 30s | 90s |
| command flow | 默认 1s | 60s |
| Phase 1 shadow | 最大 5s | 60s |
| trial guard | 默认 15s | 60s |

预算从现有设置派生，不新增可把安全 deadline 配成无限大的环境开关。

## 4. 对抗性验证

纯内存测试证明：

- 永久 pending 的 critical task 在 20ms 测试预算后让 daemon 无需外部 stop 返回 `1`；
- checkpoint 会延长 deadline，waiter 不使用过期 deadline 误报；
- 未配置预算的 pending event-driven task 不被时间分类；
- 非法预算和非关键任务预算失败关闭；
- 七条固定周期任务均声明预算，private WS 没有被错误套用；
- stalled health 返回 `503` 且只包含安全元数据；
- Phase 3D 的 exception/cancel/completion 与异常正文不泄漏断言继续通过。

## 5. 测试与静态检查

最终结果：

| 检查 | 结果 |
|---|---|
| FS-006 focused | `19 passed, 1 warning` |
| 生命周期、Gateway health、对账、command、shadow、trial guard 扩大相关回归 | `118 passed, 1 warning` |
| Ruff application（仓库要求，含 `--fix`） | `All checks passed` |
| 全量 unit（项目内独立 basetemp） | `4296 passed, 30 skipped, 1666 warnings, 85 subtests passed in 107.00s` |

扩大回归第一次运行出现 `14 failed, 119 passed, 4 errors`：14 项来自注册校验重构调用了测试替身未绑定的 helper，已改为无状态类级校验并由 focused/related/full suite 证明兼容；4 项来自 Windows 系统临时目录 `PermissionError`。仓库规定的原样全量命令也在 `87 passed` 后因同一系统临时目录权限中止。随后使用仓库内已忽略的全新 `--basetemp` 完整重跑并通过；这些中间失败保留，不写成首次即通过。

warnings 与 Phase 3J 一致，主要是 SQLite datetime adapter deprecation、LongShort poller 测试 AsyncMock 未 await，以及 pytest cache path warning；本阶段未声称 warning debt 已清零。

## 6. 已验证、未验证与未知

### 已验证

- 七条固定周期关键任务具备启动宽限和 last-success deadline；
- hang/pending 与持续无成功周期共享有界 stalled 失败语义；
- daemon nonzero/heartbeat stop 和 FastAPI `503` 代码/纯内存路径成立；
- checkpoint 延长 deadline，event-driven task 不因安静期误杀；
- 超时元数据不含异常正文、账户或连接信息；
- 全量 unit 与 application Ruff 兼容。

### 未验证

- WSL2 四进程中真实 task hang 后的容器 health/restart 时间；
- Postgres、Redis、NATS、OKX 持续断连下七条循环的真实超时与误杀边界；
- Prometheus/Loki/外部告警送达；
- shutdown/drain 的目标 RTO；
- event-driven 任务的 connection freshness、queue lag 与 service-specific last success；
- event loop 整体阻塞的外部检测；
- 独立 reviewer 对任务清单和预算的复核。

### 未执行

没有读取 `.env.*`，没有连接账户、交易所、数据库、Redis 或 NATS，没有启动/停止/部署容器，没有执行 integration test 或任何资金动作。

## 7. 当前裁定

FS-006 已从“只检测 task 结束”推进为“另可检测七条固定周期资金关键循环的永久 await 与连续无成功周期”。但事件驱动任务、整体 event-loop stall、目标依赖、容器 restart/告警和独立复核仍开放，因此当前状态只能更新为：

```text
PARTIALLY REMEDIATED / EVENT-DRIVEN AND TARGET RUNTIME VERIFICATION OPEN
```

G4 保持 `PARTIAL / 未放行`，FS-006 仍是 P1 HARD BLOCKER。**REAL-MONEY PRODUCTION: NO-GO**。
