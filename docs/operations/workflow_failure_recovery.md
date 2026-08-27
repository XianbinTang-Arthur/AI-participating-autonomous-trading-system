# RDP Workflow 失败恢复指南

> 文档状态：现行操作说明
> 最后核对：2026-08-27（起始 HEAD `9c4112c6`，含当前控制面收口候选；以本文档所在 HEAD 为准）
> 核对范围：Gateway/daemon queue、失败分类与 observation 风险收敛静态契约；不证明当前任务或资本状态

## 1. 先区分故障域

- RDP workflow 失败：影响采集、研究、治理、发布证据；按本页处理。
- 主交易 execution/OKX/reconciliation 故障：按根目录 `DEPLOYMENT.md` 和 Operator trading-ready/recovery 流程处理，不能用 RDP retry 代替。

RDP 失败通常不直接改变订单状态，但 gate/apply/observation 相关失败可能造成生产参数证据或恢复链断裂，需升级处理。

## 2. 当前状态入口

- `GET /rdp/tasks/status`：队列任务、状态、时间、exit code、error/log tail。
- `GET /rdp/health`：daemon、数据库、workflow、artifact 健康。
- `GET /rdp/control-summary`：Operator 汇总与可用动作。
- `artifacts/operations/workflow_failures.json`：legacy failure registry/审计输入，仍被部分 reliability 工具读取。
- `governance.rdp_task_queue`：Gateway/daemon 执行状态真源。

当前没有 `/rdp/operations/failures`、`/open` 或 `/retry` 路由。

## 3. 状态语义

| 状态 | 含义 |
| --- | --- |
| `pending` | 已入队，等待 `earliest_start_at` 和 daemon claim |
| `running` | daemon 已领取 |
| `done` | 退出 0，完成 |
| `failed` | 非零退出、异常或孤儿恢复 |

孤儿恢复 exit code `-3` 表示 daemon 在任务完成前重启/终止，旧 running 被回收为 failed，不是任务脚本自身返回 -3。

## 4. 标准调查

记录以下证据后再操作：

- task id、workflow、requested_by、requested/started/finished 时间；
- `earliest_start_at`；
- exit code、error message、log tail；
- daemon heartbeat 和部署版本；
- workflow JSON 版本与输入数据窗口；
- 相关 DB/artifact/OKX 可达性；
- 同 workflow 是否还有 active task。

常见分类：

| 类别 | 例子 | 处置 |
| --- | --- | --- |
| 输入质量 | gap、schema/manifest、数据不足 | 修复/补齐输入，再补跑 |
| 数据库 | 连接、迁移、约束、锁 | 恢复 DB 和 schema，核对事务结果 |
| 外部数据源 | OKX timeout/rate limit | 等待恢复并保留失败记录 |
| 代码/配置 | 参数错误、workflow/allowlist 漂移 | 修复代码与测试后部署 |
| 超时 | task 超出 daemon timeout | 先查慢因，不盲目调大 |
| daemon 生命周期 | exit `-3` | 确认旧进程死亡和新 heartbeat，再 retry |

## 5. 补跑

修复根因后，通过 Operator UI 或 `POST /rdp/tasks/trigger` 创建新任务。数据库会阻止同 workflow 同时存在两条 pending/running。

- 不直接把 failed 改回 pending/running；
- 不删除 active task 绕过 partial unique index；
- 不绕开队列直接执行 scheduled workflow 来伪造成功状态；
- 自动重试任务必须尊重 `earliest_start_at`；
- `release_cycle` 当前禁止入队，不能通过 retry 绕过冻结。

旧 artifact-based retry 工具只用于它们明确支持的历史 round/failure registry；使用前先运行 `--help` 并确认不会与 DB task queue 产生双重执行。

## 6. 高风险失败

| 失败 | 风险 | 处理 |
| --- | --- | --- |
| Gate 缺失/失败 | 未验证参数可能前向发布 | 阻止 apply，修复 evidence/gate |
| Apply 返回失败或 history 不完整 | active/release 审计可能不一致 | 停止发布，核对 DB active/history/release |
| Active parameter DB 不可用 | runtime 退化到 profile 参数 | 停止发布，恢复 DB；没有 JSON fallback |
| Observation 任一阶段失败 | 异常参数可能继续生效；已有 canonical risk 仍会尝试 effectiveness 物化 | 核对每个 stage error、主交易事实与已提交 evidence，不用最后一个成功阶段覆盖前序失败 |
| Rollback 无 target | 内部 enforcer 只能在证明充分时写 combo soft pause | 核对 pause 与 action proof；失败则保持 `reconciliation_required` |
| Rollback `in_progress`/结果不确定 | 资本动作可能已发生，禁止自动重放 | 保持同 combo apply veto，人工对齐 active/history/release/attempt/proof |
| Release cycle 被触发 | 违反冻结策略 | 立即审计触发源、任务表和 active history |

高风险失败同时检查：`/system/health`、kill switch、recovery、reconciliation、active version、近期 decision/order intent、fee/slippage/PnL。

## 7. 恢复完成标准

- 原 task 保留为 failed，原因和处置可追踪；
- 新 task 是独立记录且成功；
- daemon heartbeat 正常，无 orphan running；
- scheduler state/slot 不重复、不漏跑；
- 相关 artifact 与 DB snapshot 一致；
- 涉及生产参数时，active/history/release/provenance 完整；
- 没有通过直接 DB/JSON 编辑掩盖失败。
- 所有 rollback risk 已有 exact terminal proof，或明确保留为阻断中的 `reconciliation_required`；不得只凭 legacy boolean 宣告恢复。
