# Research Data Platform（RDP）

> 项目定位声明：RDP 只在严格风控、可审计、可恢复、可治理的边界内为主交易系统提供研究证据和受控参数。完整定位见 [项目定位声明](../../docs/project_positioning.md)。

> 文档状态：现行模块说明
> 最后核对：2026-08-27（起始 HEAD `9c4112c6`，含当前 RDP 控制面收口候选；以本文档所在 HEAD 为准）
> 核对范围：当前静态代码、迁移、配置和测试契约；既有 2026-08-26 运行证据仅作带日期历史快照，数据库覆盖、容器在线和 collector 连续性必须现场重验

适用范围：`aats/data_platform/`、`scripts/rdp_*.py`、`configs/rdp_workflows/`、RDP API 与任务守护进程。

本文是 RDP 当前入口。逐文件模块清单见 [RDP 代码模块参考](../../docs/rdp/module_reference.md)，日常操作见 [平台运行手册](../../docs/operations/platform_runbook.md)，完整系统边界见 [项目代码审查与系统说明](../../docs/code_review/README.md)。

## 1. 安全边界

RDP 是研究和治理子系统，不是实时交易执行器。

- Bronze/Silver/Gold 只服务离线研究，主交易行情来自 OKX market gateway。
- RDP 对 live 交易库只读，写入集中在独立 research/governance 数据库。
- recommendation、candidate、verdict 和 research artifact 都不会自行改变交易行为。
- runtime active parameter 的唯一真源是 Postgres `governance.active_parameter_sets`。
- 参数前向写入必须经受保护的 release API、权限、资格、gate、actor 和 history；direct apply 已停用。`skip_apply=false` 的组合 release 入口要求 `apply` token，Operator rollback 要求 `rollback` token。内部 observation 风险收敛不使用浏览器 token，但必须由精确数据库事实、combo lock、attempt 和 action proof 授权。旧直写 CLI 已禁用并返回退出码 2。
- Research Factory 只产出证据、结论和人工应用设计，不写 runtime、active parameters、managed config 或 OKX。
- 当前不允许自动 release：`release_cycle` 配置禁用，且任务队列显式阻止它入队。
- Phase 3V 的 real-data runner 使用 train stability + valid selection 双门；test 只参与
  dataset quality/source integrity 与内容 seal，不产生 factor/label/绩效 metrics/selection
  gate。execution summary 必须精确绑定 valid 窗口且只合并到
  valid。recommendation 的 ready-for-review 不代表最终 OOS 已通过。

## 2. 数据架构

`aats/data_platform/rdp_models.py::RdpBase` 当前声明 102 张 ORM 表：

| Schema | 表数 | 主要职责 |
| --- | ---: | --- |
| `staging` | 13 | 原始文件/API 落地、官方成交/L2 历史、待校验输入 |
| `bronze` | 21 | 标准化原始事实、实时采集与隔离的历史 L2/mark proxy |
| `silver` | 16 | 去重、质量门控，以及 bundle-scoped 历史派生数据 |
| `gold` | 9 | replay、对齐和研究消费数据集 |
| `meta` | 14 | ingest run、checkpoint、来源、归档、缺口、bundle、重建与 collector continuity |
| `research` | 3 | experiment 与研究结果 |
| `governance` | 26 | 参数、推荐、发布、观察、应用层 action proof、任务队列、逻辑 Run/Step/Event、holdout、参数代次和运行状态 |
| **合计** | **102** | — |

标准部署后的 `aats_research` 物理库当前是 110 张表：上述 102 张 ORM 表，另加 Batch B SQL 所有的 7 张治理表（`apply_saga_operations`、`cost_calibration_runs`、`profile_research_runs`、`profile_type_review_streak`、`rdp_daemon_heartbeat`、`system_config`、`system_config_history`）和迁移账本 `rdp_schema_migrations`。覆盖审计扫描 102 张 ORM 表；部署验收同时要求这 8 张 migration-owned 表存在，不得把“ORM 数量 102”误写成“物理库恰好 102 张”。

迁移定义位于 `aats/data_platform/migrations/`。显式前向入口是 `scripts/apply_schema_migrations.py`（部署综合作业）或兼容初始化入口 `scripts/rdp_init_db.py`；它们均执行 ORM baseline + 全部 18 个有序 Batch B stage（末项名称 `batch_b_19_historical_research_artifacts`），并在 `governance.rdp_schema_migrations` 保存 version/checksum。应用、daemon 和研究 job 不在启动期执行 DDL，只读校验 ORM table/column surface 与迁移账本。不能用旧文档中的“48/78/81/84/98/101 张表”或单纯“表存在”判断 schema 完整。

### 数据流

```text
OKX REST / 历史 ZIP / live 只读事实
  -> staging
  -> validate + normalize
  -> bronze
  -> silver
  -> gold replay datasets
  -> replay / attribution / execution realism
  -> governance evidence / recommendation / verdict
  -> 人工 gate + release/apply API
  -> governance.active_parameter_sets
  -> 主交易 build_runtime() 读取
```

## 3. 当前运行组件

| 组件 | 入口 | 职责 |
| --- | --- | --- |
| RDP task daemon | `scripts/rdp_task_daemon.py` | 在容器内启用 scheduler、领取 attempt、同步 Run heartbeat、执行/取消 workflow、写终态与重试关系 |
| Workflow scheduler | `scripts/rdp_schedule_workflows.py` / `operations/workflow_scheduler.py` | 从数据库调度状态和 JSON 定义计算到期 slot，原子入队 |
| Workflow dispatcher | `operations/workflow_dispatcher.py` | 校验 workflow 和任务、按顺序执行、写运行报告并上报结构化 Run Step/Event |
| Gateway RDP API | `aats/api/rdp_routes.py`、`rdp_v2.py`、`rdp_workspace.py`、`rdp_workspace_routes.py`、`rdp_profile_routes.py` | Run 创建/详情/取消/重试、Workspace V3，以及查询、审批、gate、release/apply/rollback、兼容 workbench、profile/sleeve 治理 |
| Rolling collectors | `collectors/rolling/`、相关 `scripts/rdp_*` | Candle、funding、OI、mark、long/short 等增量采集 |
| Public WS collectors | `collectors/microstructure_ws_collector.py`、`liquidations_ws_collector.py` | `trades`、BBO、books5、OI/funding/mark 与公共强平；保存连接代次、采样/接收时间、drop/flush/gap 证据 |
| Data governance | `data_governance/`、`scripts/rdp_{audit,archive,import,rebuild}_*.py` | 只读覆盖审计、不可变 Parquet 归档、官方历史来源、双资格门与确定性历史重建 |
| Research Factory | `research_factory/`、`research/` | 证据输入、实验、verdict、治理审查与人工应用设计 |

标准 Compose 中 `aats-rdp-daemon` 随应用栈启动，命令为：

```text
python scripts/rdp_task_daemon.py --poll-interval 10 --enable-scheduler
```

它不是“宿主机手工维护的后台进程”。

## 4. Workflow 与调度真相

`configs/rdp_workflows/` 当前有 10 个有效 JSON。调度时间均为 UTC。

| Workflow | 当前调度 | Enabled | 主要任务 |
| --- | --- | --- | --- |
| `candles_rolling_15m` | 每 15 分钟 | 是 | 15m rolling candles |
| `microstructure_silver_15m` | 每 15 分钟 | 是 | microstructure Silver 聚合 |
| `okx_rest_history_rolling_1h` | 每小时第 20 分钟 | 是 | OI/mark/long-short 历史窗口 |
| `reliability_cycle` | 每小时第 15 分钟 | 是 | 可靠性检查 |
| `observation_cycle` | 每小时第 30 分钟 | 是 | release observation 推进 |
| `data_maintenance` | 每日 04:00 | 是 | daily ingest、artifact index、retention |
| `governance_cycle` | 每日 07:00 | 是 | quality、artifact validation、round/candidate 治理 |
| `research_cycle` | 每周日 08:00 | 是 | 数据刷新和 full pipeline；Phase 3 默认 live attribution，缺 live DB 时失败关闭 |
| `decision_cycle` | 每周日 10:00 | **否** | 保留定义，不自动调度 |
| `release_cycle` | 每小时整点 | **否** | 定义保留；任务队列还显式冻结入队 |

冷启动 bootstrap 顺序是 `data_maintenance → research_cycle`。数据库调度状态是正常路径真源；只有数据库不可达时才退化读取 artifact 状态文件，并记录 stale 风险。

## 5. 任务队列并发与恢复

Gateway 和 scheduler 都向 `governance.rdp_task_queue` 写任务，daemon 负责执行。

- `VALID_WORKFLOWS` 必须覆盖全部 10 个 JSON 定义。
- 同一 workflow 只允许一条 `pending`/`running`：数据库 partial unique index 是最终约束。
- `db_create_task_if_idle()` 使用 `INSERT ... ON CONFLICT DO NOTHING RETURNING` 原子吸收并发竞争。
- daemon 用 `FOR UPDATE SKIP LOCKED` 领取最早且 `earliest_start_at <= now()` 的任务。
- 延迟重试通过 `earliest_start_at` 生效，不会立即重新领取。
- daemon 重启会把孤儿 `running` 任务收敛为 `failed`，特殊退出码为 `-3`。
- `release_cycle` 位于 `ENQUEUE_BLOCKED_WORKFLOWS`；scheduler、API 和 daemon 都不得绕过冻结。

## 6. 配置与数据真源

| 配置 | 当前真源 | 说明 |
| --- | --- | --- |
| RDP 数据库 | `RDP_DATABASE_URL` | 本地 `.env.research`；容器未显式设置时可复用 `AATS_ACTIVE_PARAMETER_DB_URL` |
| Live 只读库 | `RDP_LIVE_DATABASE_URL` | 只读健康、归因和 gate 输入 |
| Workflow 定义 | `configs/rdp_workflows/*.json` | 任务序列和 schedule 声明 |
| Scheduler state | governance DB | 文件只作 DB 故障降级快照 |
| Runtime active parameters | `governance.active_parameter_sets` | 主交易 loader DB-only；无 JSON fallback |
| 参数/推荐等治理 registry | governance DB-first | 部分模块仍保留文件审计副本/降级读取；不能与 runtime active parameter 真源混为一谈 |
| Research artifacts | `artifacts/` | 证据和报告，不是 live 配置 |

不要在文档、日志或命令输出中打印数据库连接串、API key、session secret 或 token。

## 7. Active parameter 受控变更

当前可执行前向写路径在 Operator release API：

1. 审阅并批准 recommendation。
2. 运行 pre-apply gate。
3. 通过 `POST /rdp/operator-tokens` 获取 `action=apply` 的短时 token，再调用 `POST /rdp/releases/create` 或 `POST /rdp/recommendations/{id}/approve-and-release` 建立 canonical release。
4. `POST /rdp/parameters/apply` 已停用并固定返回 `release_required`，不得作为前向或重试入口。
5. 核对 `GET /rdp/parameters/active`、apply history、release history 和主交易 `/system/health`。
6. 观察失败时可通过携带 `action=rollback` token 的 `POST /rdp/parameters/rollback` 人工回滚；启用的 observation cycle 也会在精确证明下收敛 pending rollback risk，不确定状态转人工 reconciliation。

以下 CLI 已禁用，不能写进现行 runbook：

- `scripts/apply_active_parameter_set.py`
- `scripts/approve_recommendation_and_apply.py`
- `scripts/rdp_rollback_active_parameter_set.py`
- `scripts/rdp_run_release_cycle.py`

它们只保留为明确报错的兼容桩。

## 8. 常用只读/研究入口

使用项目 Python 运行；涉及数据库写入前先确认目标环境。

```powershell
# 显式初始化/迁移 RDP schema（仅对已明确的非 live/受控目标）
.\.venv\Scripts\python.exe scripts\rdp_init_db.py

# 单次日批采集
.\.venv\Scripts\python.exe scripts\rdp_run_daily_ingest.py --ensure-schema

# 完整研究管线
.\.venv\Scripts\python.exe scripts\rdp_run_full_pipeline.py --start 2026-03-31 --end 2026-04-02 --ensure-schema

# 只评估到期 workflow，不入队
.\.venv\Scripts\python.exe scripts\rdp_schedule_workflows.py --dry-run --json

# 可靠性与质量检查
.\.venv\Scripts\python.exe scripts\rdp_run_reliability_check.py
.\.venv\Scripts\python.exe scripts\rdp_run_quality_monitor.py

# 只读覆盖审计（生成不可变 JSON/Markdown 快照）
.\.venv\Scripts\python.exe scripts\rdp_audit_data_coverage.py --window-days 90
```

`--ensure-schema` 是为了 CLI 兼容保留的旧名；在上述 ingest/replay/pipeline 运行器中它现在**只读校验** schema contract，不会创建表或执行 ALTER。如需迁移，必须先单独运行显式迁移入口；live/WSL2 仅通过 `scripts/deploy.sh` 执行综合 schema job。

`scripts/rdp_start.py`、`scripts/rdp_realtime_daemon.py` 是兼容入口；`rdp_start.py` 会转发到 daily ingest 和 historical `--once`。新自动化应直接调用目标脚本或任务队列，不应依赖这些 legacy shim。

### 8.1 历史恢复与持续采集

所有历史执行都以 dry-run/计划输出为默认行为；写入必须使用绝对 raw/archive 目录和显式确认。当前支持：

- confirmed OHLCV 与 funding REST 回填，逐页保存不可变原始响应；
- 官方成交 REST/文件、官方 L2 文件、mark-price bar proxy 导入；
- `historical_research` 与 `live_capture` 资格分离；
- 仅从 `ELIGIBLE` bundle 重建隔离的历史 Silver；
- live 热表按 UTC 日先归档、验证 SHA-256/Parquet 行数，再允许 retention 删除；
- `liquidation-orders` 没有可信公共历史回填路径，过去窗口保持不可恢复/等待未来采集；零事件只有在入站连接帧连续且无 drop/disconnect 时才是有效零。

正式步骤、停止条件和故障恢复见 [RDP 历史数据恢复与持续采集手册](../../docs/operations/rdp_historical_data_recovery_runbook.md)。

## 9. API 与 Operator UI

Gateway 当前注册 57 个 method/path operation、56 个唯一 `/rdp/*` URL path，其中包括 5 个 Run V2 operation 和 2 个 Workspace V3 operation，覆盖：

- health、active parameters、apply history；
- attribution、execution、decision、readiness；
- recommendation 审批/拒绝/替代/批准并发布；
- token、gate、release、apply、rollback、observation；
- task trigger/status、control summary；
- workbench、tuning proposal；
- profile recommendation/type review；
- sleeve advice。

当前 UI 只读取 `GET /rdp/v3/workspace` 返回的单一版本化业务快照；数据治理部分由服务端读取最新不可变覆盖 artifact 和有界 meta 聚合。`GET /rdp/v3/data-governance` 提供同一治理快照的只读端点，不扫描 raw tick 表、不返回 DSN/source locator，也不提供 live 或参数 apply 动作。旧 control/workbench/tuning 读接口继续保留给兼容脚本，不再由页面异步拼接。Managed recommendation/effectiveness 数据库读取失败时禁止回退到陈旧 JSON；DB CAS 已提交但审计镜像刷新失败时必须单独报告 degraded，不能把 canonical 成功误报为业务失败。

完整方法与路径以运行时 `/openapi.json` 和 [项目代码审查与系统说明](../../docs/code_review/README.md) 为准。`/healthz` 只表示 Gateway 存活，不代表 RDP、交易或参数发布已 ready。

## 10. 测试

```powershell
# RDP 相关单元测试
.\.venv\Scripts\python.exe -m pytest tests\unit -k "rdp or workflow or active_parameter" -x -q

# 全部单元测试
.\.venv\Scripts\python.exe -m pytest tests\unit -x -q
```

需要真实 Postgres/NATS/Redis 的集成测试在 WSL2 环境运行。每次交付报告实际命令和结果，不在本页保存会失效的通过数。

## 11. 延伸阅读

- [RDP 代码模块参考](../../docs/rdp/module_reference.md)
- [平台运行手册](../../docs/operations/platform_runbook.md)
- [RDP 历史数据恢复与持续采集手册](../../docs/operations/rdp_historical_data_recovery_runbook.md)
- [参数应用与回滚](../../docs/operations/parameter_apply_and_rollback.md)
- [生产参数变更 Runbook](../../docs/operations/production_parameter_change_runbook.md)
- [Managed Profile 配置说明](../../docs/configuration/managed-config-reference.md)
- [项目代码审查与系统说明](../../docs/code_review/README.md)
