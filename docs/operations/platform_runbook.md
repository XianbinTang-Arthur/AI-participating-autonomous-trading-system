# RDP 平台运行手册

> 文档状态：现行操作说明
> 最后核对：2026-08-27（起始 HEAD `9c4112c6`，含当前控制面收口候选；以本文档所在 HEAD 为准）
> 核对范围：task-daemon、数据库队列、10 个 workflow、schema/API 与标准模拟部署静态契约；不证明现场覆盖或容器状态

## 1. 运行边界

- RDP 写 research/governance 数据库，对主交易数据库保持只读。
- 主交易 runtime active parameter 只从 `governance.active_parameter_sets` 加载。
- 参数前向变更只能通过认证后的 release API 和 gate；`skip_apply=false` 的两个组合入口都要求短时 `action=apply` token。direct apply 已停用并固定无写入返回 `release_required`。Operator rollback 要求独立的 `action=rollback` token；内部 observation 风险收敛走精确数据库证明而非浏览器 token。旧直写脚本已禁用。
- `release_cycle` 当前禁用且禁止入队；不要通过手工改任务表绕过。
- 标准 `aats-rdp-daemon` 随部署启动，不需要在宿主机另起 nohup 进程。
- 不在终端、文档、工单或聊天中显示 `.env.*` 内容、连接串、token 或交易所凭证。

## 2. 每日/每次部署后检查

### 2.1 主系统与 RDP 健康

1. derivatives 模拟 profile 的部署报告必须同时证明 gateway、market、decision、execution、rdp-daemon、liquidations-daemon、microstructure-collector 七个应用容器健康。
2. live profile 仍在副作用前被拒绝；模拟 collector 健康不能外推为 live 可部署或历史覆盖完整。
3. 登录 Operator UI，检查：
   - `/system/health` 无 critical blocker；
   - `/system/recovery` 无未处理 stuck/ambiguous submit；
   - `/reconciliation/latest` 无 unresolved high/critical finding；
   - `/rdp/health` 的数据库、artifact、workflow 和 daemon 状态；
   - `/rdp/tasks/status` 无异常长期 running/pending；
   - `/rdp/parameters/active` 的 version、combo、actor 和缺失情况。

`/healthz` 只表示 Gateway 进程存活，不能代替上述检查。

### 2.2 只读 CLI 检查

```powershell
# 到期 slot 评估：不写队列/状态
.\.venv\Scripts\python.exe scripts\rdp_schedule_workflows.py --dry-run --json

# 可靠性和质量
.\.venv\Scripts\python.exe scripts\rdp_run_reliability_check.py
.\.venv\Scripts\python.exe scripts\rdp_run_quality_monitor.py

# Active rounds
.\.venv\Scripts\python.exe scripts\rdp_list_active_rounds.py
```

先确认命令使用的环境和数据库，再执行任何非 dry-run 操作。

## 3. Workflow 日历

所有时间为 UTC：

| Workflow | 周期 | Enabled | 备注 |
| --- | --- | --- | --- |
| `candles_rolling_15m` | 每 15 分钟 | 是 | rolling candles |
| `microstructure_silver_15m` | 每 15 分钟 | 是 | Silver 聚合 |
| `reliability_cycle` | 每小时 :15 | 是 | 可靠性 |
| `okx_rest_history_rolling_1h` | 每小时 :20 | 是 | OI/mark/long-short |
| `observation_cycle` | 每小时 :30 | 是 | release observation + 受控 pending-risk 收敛 |
| `data_maintenance` | 每日 04:00 | 是 | ingest/index/retention |
| `governance_cycle` | 每日 07:00 | 是 | quality/validation/candidate |
| `research_cycle` | 周日 08:00 | 是 | refresh/full pipeline |
| `decision_cycle` | 周日 10:00 | 否 | 仅保留定义 |
| `release_cycle` | 每小时 :00 | 否 | 冻结，禁止入队 |

首次调度 bootstrap 固定为 `data_maintenance → research_cycle`，之后才进入常规 slot 评估。当前各 workflow 使用滚动窗口/水位推进，不接受历史 slot 参数；因此多个漏执行 slot 会合并为一次“截至最新到期 slot”的任务。若同 workflow 已有 active task，本轮不会推进 scheduler 水位，待下轮再次评估。

## 4. 手工触发 Workflow

推荐通过 Operator UI 或 `POST /rdp/tasks/trigger` 触发，状态通过 `GET /rdp/tasks/status` 查询。请求必须经过 Operator 认证；workflow 名必须属于当前 10 项 allowlist，且不能是冻结的 `release_cycle`。

触发前：

1. 检查同一 workflow 是否已有 pending/running；
2. 确认目标环境允许该 workflow；
3. 确认数据库和 OKX REST（若使用）可达；
4. 记录 actor 和原因。

任务队列保证：

- 同一 workflow 最多一个 active task；
- 并发触发通过数据库 partial unique index 和原子 `INSERT ... ON CONFLICT` 收敛；
- daemon 通过 `FOR UPDATE SKIP LOCKED` 领取；
- 只有分类为 `transient_infrastructure` 的失败才自动重试一次，且必须等待 `earliest_start_at`；确定性代码错误、数据/业务门禁和未知错误保持终态；
- orphan recovery 只接管心跳超过 30 秒未更新的 running 任务，以 exit `-3` 标记 failed；并行 daemon 启动不会把仍有心跳的任务误判为 orphan。

## 5. 研究脚本

### 5.1 数据采集与 schema

```powershell
.\.venv\Scripts\python.exe scripts\rdp_init_db.py
.\.venv\Scripts\python.exe scripts\rdp_run_daily_ingest.py --ensure-schema
.\.venv\Scripts\python.exe scripts\rdp_detect_gaps.py
```

`rdp_init_db.py` 是受控显式迁移入口，执行 ORM baseline、完整 Batch B ledger/checksum chain 和最终只读校验；当前 ORM 为 102 张表。Batch B 仍为 18 个有序 stage，末项名称是 `batch_b_19_historical_research_artifacts`，并额外所有 7 张非 ORM 治理表与 `rdp_schema_migrations` 账本，所以标准部署物理库当前为 110 张。不要按旧的 48/78/81/84/98/101 表清单验收，也不要把 ORM 数量 102 当成物理库 exact count。`--ensure-schema` 为旧 CLI 名，在 daily ingest/replay 等业务 runner 中已收紧为只读 validate-only，不再执行 DDL。Live 部署不手工运行本节命令，只通过根 `scripts/deploy.sh` 的一次性综合 schema job。

### 5.2 历史数据治理

历史覆盖审计、不可变归档、官方 trade/L2/mark 导入、archive-before-delete、collector continuity、历史资格和 bundle 重建统一使用 [`rdp_historical_data_recovery_runbook.md`](rdp_historical_data_recovery_runbook.md)。覆盖 artifact 只证明审计时点的数据库事实；UI 中“治理快照可用”不等于覆盖完整、候选合格或可以实盘。

### 5.3 Replay 与研究

```powershell
.\.venv\Scripts\python.exe scripts\rdp_run_replay.py --help
.\.venv\Scripts\python.exe scripts\rdp_run_parameter_scan.py --help
.\.venv\Scripts\python.exe scripts\rdp_run_full_pipeline.py --help
.\.venv\Scripts\python.exe scripts\rdp_run_research_factory_experiment.py --help
```

先用 `--help` 核对当前参数，不复制历史任务文档中的固定日期/批次命令。Research Factory 结论只有 `reject`、`keep_observing`、`positive_executable_edge` 三类，且只生成证据，不自动 apply。

## 6. 参数审批、发布与回滚

### 6.1 发布前条件

- recommendation 已批准且未 supersede；
- evidence、execution realism、live attribution 可追踪；
- pre-apply gate 允许；
- 主交易 health/recovery/reconciliation 可接受；
- actor、release id、observation plan、rollback target 齐全；
- 当前 Operator session 签发的短时 `X-Rdp-Apply-Token`；`skip_apply=false` 的组合 release 入口要求 `action=apply` token。

### 6.2 当前写入口

| 动作 | API |
| --- | --- |
| 批准 recommendation | `POST /rdp/recommendations/{id}/approve` |
| 拒绝 recommendation | `POST /rdp/recommendations/{id}/reject` |
| 替代 recommendation | `POST /rdp/recommendations/{id}/supersede` |
| Gate | `POST /rdp/gates/run` |
| 获取 Operator token | `POST /rdp/operator-tokens` |
| 创建 release + apply | `POST /rdp/releases/create` + `action=apply` token |
| Direct apply 迁移失败入口 | `POST /rdp/parameters/apply`（固定 `release_required`，无写入） |
| 观察 | `POST /rdp/observations/run` |
| 回滚评估 | `POST /rdp/rollback-recommendation/evaluate` |
| 执行回滚 | `POST /rdp/parameters/rollback` |

apply/release/rollback 的 token 属于敏感短期凭证，不写入 shell history、文档或工单。完整 payload 契约以 `/openapi.json` 为准。

启用的 `observation_cycle` 会在持久化评估后处理 pending rollback risk。它只在 exact
release/post-apply provenance、clean attempt、combo lock 与应用层 insert-once action proof
全部成立时回滚、取消或 soft pause；其他记录进入 `reconciliation_required` 并继续阻断前向
apply。不得用 legacy boolean、JSON 镜像或 release 状态单独宣告回滚完成。

以下脚本已禁用，运行会退出 2：

- `scripts/apply_active_parameter_set.py`
- `scripts/approve_recommendation_and_apply.py`
- `scripts/rdp_rollback_active_parameter_set.py`
- `scripts/rdp_run_release_cycle.py`

### 6.3 发布后核对

1. `GET /rdp/parameters/active` 显示预期 combo/version；
2. `GET /rdp/parameters/apply-history` 有 actor/action/gate/release；
3. `GET /rdp/releases/latest` 与 history 一致；
4. 重建 runtime 后 Settings Provenance 显示 active parameter 注入；
5. `/system/health`、reconciliation、order intent、fee/slippage 和风险指标无退化；
6. observation 到期后记录 keep/review/rollback 结论。

数据库加载失败时 runtime 会 fail-soft 到 profile 参数并记录 error，不会从 JSON 文件恢复。该状态必须按配置漂移处理。

## 7. 故障处理

### 7.1 Task 长期 pending

- 检查 rdp-daemon heartbeat/health；
- 检查 `earliest_start_at` 是否尚未到；
- 检查是否已有同 workflow running；
- 检查数据库连接和队列约束；
- 不要直接改成 running，也不要删除 active task 绕过唯一约束。

### 7.2 Task 长期 running

- 查看 task status 的 started_at/log tail；
- 检查 daemon 是否仍有 heartbeat；
- daemon 已重启时，确认超过 30 秒无心跳的 orphan 是否被标为 failed/-3；仍有新鲜心跳的任务不得被恢复逻辑覆盖；
- 只通过标准 retry 路径补跑可重试故障，保留原失败记录。

### 7.3 Workflow 失败

1. 保存 workflow、task id、exit code、log tail 和输入版本；
2. 判断是输入质量、数据库、OKX、timeout、schema 还是代码错误；
3. 临时基础设施故障可通过标准 retry 路径补跑；确定性代码错误、数据质量或业务门禁失败必须先修复根因，再由 Operator 明确重新触发；
4. 验证新任务与旧失败记录可关联；
5. 不通过重复直接执行脚本掩盖队列状态。

### 7.4 Active parameter 数据库失败

- 停止任何新发布动作；
- 检查 `/rdp/health` 和 active parameter loader error；
- 以数据库为真源恢复，不把 `configs/active_parameter_sets/*.json` 人工灌回 runtime；
- 恢复后核对 active set、history、gate、release 与 Settings Provenance。

## 8. 停机、备份与恢复

系统级停机使用：

```bash
# 默认只读预检
bash scripts/ops/safe_shutdown.sh --reason "planned_maintenance"

# 人工确认后执行
bash scripts/ops/safe_shutdown.sh --apply --confirm --reason "planned_maintenance"
```

数据库备份/恢复使用 `deploy/wsl2-dev/scripts/backup_postgres.sh` 和 `restore_postgres.sh`。恢复前记录 commit/profile、先备份当前库并停止应用；恢复后先做 RDP schema、task queue、active parameters 和主交易一致性核对。

## 9. 相关文档

- [RDP 总览](../../aats/data_platform/README.md)
- [RDP 模块参考](../rdp/module_reference.md)
- [参数应用与回滚](parameter_apply_and_rollback.md)
- [生产参数变更](production_parameter_change_runbook.md)
- [Operator 检查清单](operator_checklist.md)
- [完整代码审查与系统说明](../code_review/README.md)
