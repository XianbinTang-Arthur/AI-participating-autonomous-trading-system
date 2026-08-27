# RDP Workflow 调度策略

> 文档状态：现行操作说明
> 最后核对：2026-08-27（起始 HEAD `9c4112c6`，含当前控制面收口候选；以本文档所在 HEAD 为准）
> 核对范围：task daemon、scheduler、queue、workflow dispatch 与自动风险收敛静态契约；不证明当前 daemon 健康

## 1. 架构

```text
configs/rdp_workflows/*.json
  -> workflow_scheduler 计算 UTC due slots
  -> governance.rdp_task_queue (pending)
  -> rdp_task_daemon FOR UPDATE SKIP LOCKED claim
  -> workflow_dispatcher 执行 tasks
  -> done / failed + exit/error/log tail
  -> Operator /rdp/tasks/status
```

标准 `aats-rdp-daemon` 命令：

```text
python scripts/rdp_task_daemon.py --poll-interval 10 --enable-scheduler
```

该进程由 Compose 管理并写数据库 heartbeat。`scripts/rdp_start.py` 和 `rdp_realtime_daemon.py` 是 legacy shim，新调度不依赖它们。

## 2. 配置契约

每个 `configs/rdp_workflows/<name>.json` 至少定义：

- workflow name/description；
- ordered tasks；
- task command、timeout、failure policy；
- schedule.enabled；
- frequency：custom/hourly/daily/weekly；
- UTC minute/hour/weekday 或 interval。

新增 workflow 必须同步：

1. `configs/rdp_workflows/*.json`；
2. `rdp_task_db.VALID_WORKFLOWS`；
3. `scripts/rdp_task_daemon.py::WORKFLOW_TIMEOUTS`；
4. Operator/API 可用性；
5. schedule/calendar/runbook；
6. allowlist/coverage/queue 测试。

## 3. 当前调度

当前 10 项及 enabled 状态见 [Workflow 调度日历](rdp_workflow_calendar.md)。关键限制：

- `decision_cycle` disabled；
- `release_cycle` disabled；
- `release_cycle` 还在 `ENQUEUE_BLOCKED_WORKFLOWS`，API/scheduler/retry/daemon 都不能把它变成可执行任务；
- `observation_cycle` 与 `reliability_cycle` 已从低频 decision 关注点拆成独立小时任务；
- candles/microstructure 每 15 分钟，REST history 每小时。

`observation_cycle` 的持久化执行会在各 release 的 observation、rollback recommendation、
effectiveness 阶段之后调用内部 pending-risk enforcer。该动作不是 `release_cycle`，不会创建新
release；它只在精确 post-apply provenance、clean attempt、combo lock 和数据库终态证明下
执行回滚/取消/soft pause，其他状态一律转人工 reconciliation。

## 4. Scheduler state

正常路径以 governance DB 中 operational scheduler state 为真源。

- DB 读取成功时，即使 state 为空也不回灌旧文件。
- DB 失败时可退化读取 `artifacts/operations/workflow_scheduler_state.json`，并承担 stale 风险。
- 保存顺序为 DB 先、文件后；DB 写失败不会更新文件，避免 ghost slot。
- `bootstrap_completed_at` 是 bootstrap 已完成的权威信号，和 active bootstrap stage 互斥。

首次初始化：

- bootstrap sequence：`data_maintenance → research_cycle`；
- 其他定时 workflow 将 `last_processed_slot` 初始化到当前 slot，不追溯 epoch。

## 5. Queue 并发

- 同一 workflow 最多一条 pending/running，数据库 partial unique index 兜底。
- `db_create_task_if_idle()` 以单条原子 INSERT 吸收 API/scheduler race。
- claim 只选择 `earliest_start_at <= now()`，并使用 `FOR UPDATE SKIP LOCKED`。
- daemon 挂掉后的 orphan running 在启动恢复中标为 failed，exit `-3`。
- retry 是新 task，不篡改原失败记录。

## 6. Catch-up 语义

Scheduler 根据 last processed slot 枚举至最新 slot：

- custom：按 interval minutes；
- hourly：按 interval hours + minute；
- daily：按 UTC hour/minute；
- weekly：按 UTC weekday/hour/minute。

短暂 daemon 停机后可能有多个 missed slots，但同 workflow active 唯一约束会限制实际并行。运维需观察队列积压和输入幂等，不能假设“只跑最新一次”或“无限并发补齐”。

## 7. 手工操作

只读评估：

```powershell
.\.venv\Scripts\python.exe scripts\rdp_schedule_workflows.py --dry-run --json
```

实际触发使用 `POST /rdp/tasks/trigger`，查询使用 `GET /rdp/tasks/status`。不要：

- 用 cron/Task Scheduler 直接执行同一个 scheduled workflow，造成双调度；
- 手工改 task status；
- 删除 active task 绕过唯一约束；
- 直接执行 frozen `release_cycle`；
- 在 rdp-daemon 之外再启动第二个长期 scheduler，除非经过并发演练和批准。

## 8. 监控与告警

至少监控：

- rdp-daemon heartbeat age/status；
- pending/running age；
- exit `-3` orphan recovery；
- per-workflow success/failure/latency；
- missed/catch-up slot；
- DB scheduler state write failure；
- enabled workflow 未在合理窗口完成；
- disabled/frozen workflow 意外入队。

详细日历见 [rdp_workflow_calendar.md](rdp_workflow_calendar.md)，故障恢复见 [workflow_failure_recovery.md](workflow_failure_recovery.md)。
