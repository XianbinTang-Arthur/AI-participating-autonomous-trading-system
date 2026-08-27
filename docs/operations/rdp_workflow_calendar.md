# RDP Workflow 调度日历

> 文档状态：现行操作说明
> 最后核对：2026-08-27（起始 HEAD `9c4112c6`，含当前控制面收口候选；以本文档所在 HEAD 为准）
> 核对范围：workflow JSON、scheduler/queue 与 observation dispatch 静态契约；不证明当前 task 已运行

## 当前 10 个 Workflow

| Workflow | Schedule | Enabled | 任务摘要 |
| --- | --- | --- | --- |
| `candles_rolling_15m` | 每 15 分钟 | 是 | 15m candle rolling ingest |
| `microstructure_silver_15m` | 每 15 分钟 | 是 | microstructure Silver 构建 |
| `reliability_cycle` | 每小时 :15 | 是 | reliability check |
| `okx_rest_history_rolling_1h` | 每小时 :20 | 是 | OI/mark/long-short REST history |
| `observation_cycle` | 每小时 :30 | 是 | release observation；持久化运行结束时执行受控 pending-risk 收敛 |
| `data_maintenance` | 每日 04:00 | 是 | daily ingest、artifact index、retention |
| `governance_cycle` | 每日 07:00 | 是 | quality、artifact validation、round/candidate |
| `research_cycle` | 周日 08:00 | 是 | refresh data、full pipeline |
| `decision_cycle` | 周日 10:00 | **否** | 定义保留，不自动调度 |
| `release_cycle` | 每小时 :00 | **否** | 定义保留；任务队列额外阻止入队 |

## 每日视图

```text
每 15 分钟  candles_rolling_15m + microstructure_silver_15m
每小时 :15  reliability_cycle
每小时 :20  okx_rest_history_rolling_1h
每小时 :30  observation_cycle
每日 04:00   data_maintenance
每日 07:00   governance_cycle
周日 08:00   research_cycle
```

`decision_cycle` 和 `release_cycle` 不在执行日历中。不得因 JSON 中保留 schedule 字段就把它们视为会自动运行。

`release_cycle` 禁用只表示“不自动创建/应用新 release”。它不禁用
`observation_cycle` 的安全收敛职责：后者会对已有 release 生成 observation/effectiveness，
并仅在精确 provenance、clean attempt、combo lock 和数据库终态证明都成立时处理 pending
rollback；不满足时进入 `reconciliation_required`，不会用 legacy 证据执行资本动作。

## 冷启动与补偿

- 首次 scheduler bootstrap 固定排队 `data_maintenance → research_cycle`。
- 其他 workflow 首次只记录最新 slot，避免从 Unix epoch 全量回补。
- daemon 停机后，scheduler 会计算 `last_processed` 之后的全部到期 slot，但只为最新滚动窗口创建一次任务，并在报告的 `coalesced` 中记录合并范围。现行命令不接收历史 slot，逐 slot 入队不能形成历史回放。
- 同 workflow 的 partial unique index 阻止并行 pending/running。
- 已有 active task 时不会推进 `last_processed_slot`，避免把尚未覆盖的最新窗口误记为已处理。
- 自动 retry 只用于明确分类为 `transient_infrastructure` 的失败，复用同一 `run_id` 并设置 `earliest_start_at`；代码异常、业务门禁、数据不足和未知失败保持终态等待人工处理。

## 时区

代码统一用 UTC 计算 slot。展示到 America/New_York、Asia/Shanghai 等本地时区时需考虑夏令时；运维记录始终保留原始 UTC 时间。

## 核对方式

```powershell
.\.venv\Scripts\python.exe scripts\rdp_schedule_workflows.py --dry-run --json
```

该命令只评估，不写队列或调度状态。实际任务通过 `POST /rdp/tasks/trigger` 或 daemon scheduler 入队；状态查看 `GET /rdp/tasks/status`。
