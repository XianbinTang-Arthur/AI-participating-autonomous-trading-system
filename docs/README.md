# AATS 文档地图与适用边界

最后核对：2026-08-23（代码基线 `be9179e`）

本页解决一个长期问题：仓库同时保存当前规范、专题参考、历史设计、审查报告、任务书和一次性观察记录。文件仍在仓库中，不代表它描述当前行为。

## 1. 真实性与时效性判定

“最后核对”只证明文档已在所列 Git 基线按静态代码、迁移、配置和测试复核，不证明当前容器、数据库、交易所账户或实盘链路健康。

| 文档状态 | 必须具备 | 何时视为待复核 |
| --- | --- | --- |
| 现行操作说明 | `最后核对` 日期、Git 基线、实际入口、失败/停止边界 | 部署/配置/API/安全路径变化，或距最后核对超过 30 天 |
| 现行架构/模块说明 | `最后核对` 日期、Git 基线、代码真源链接 | 模块边界、数据模型或控制流变化，或距最后核对超过 90 天 |
| 现行约束 | 明确约束对象和变更触发条件 | 安全、测试、数据库或部署纪律发生变化 |
| 专题参考 | 说明适用子域，并链接当前真源 | 引用的 schema、指标、artifact 或实现发生变化 |
| 历史证据 | 日期/commit/阶段语义；醒目历史声明或由本页目录级声明覆盖 | 不升级为现行；只允许补断链、补历史边界，不改写当时事实 |

当当前 `HEAD` 与文档基线不同，不能仅凭 commit 不同断言文档错误；必须先检查差异是否触及其真源。但任何实盘操作前，操作类文档都必须在当前 `HEAD` 重新核对。

运行时事实——账户余额、仓位、订单、active parameter version、kill switch、reconciliation、容器健康、告警与交易所模式——禁止以静态文档快照作为结论，必须通过当前受控 UI/API、只读数据库查询或标准健康检查取得。

## 2. 冲突裁决顺序

文档出现冲突时，按以下顺序判断：

1. 当前代码、数据库迁移、Compose 声明与 `scripts/deploy.sh`；
2. 本页标为“现行”的根入口/运行手册；
3. 专题参考；
4. 带日期的 audit/review；
5. design/task/roadmap/release notes/观察窗口等历史材料。

真实资金操作不能仅凭历史文档执行。无法从现行代码确认的步骤应停止并重新审查。

## 3. 现行入口

| 文档 | 状态 | 用途 |
| --- | --- | --- |
| [`README.md`](../README.md) | 现行 | 项目入口、profile、快速开始、文档索引 |
| [`ARCHITECTURE.md`](../ARCHITECTURE.md) | 现行 | 主交易切片、状态/事件/资金/账务/RDP 边界 |
| [`DEPLOYMENT.md`](../DEPLOYMENT.md) | 现行 | 唯一部署入口、profile/端口、TLS、停机、trading-ready |
| [`CLAUDE.md`](../CLAUDE.md) / [`AGENTS.md`](../AGENTS.md) | 现行约束 | 开发、测试、凭证、部署和数据库纪律 |
| [`CONTRIBUTING.md`](../CONTRIBUTING.md) | 现行约束 | 所有贡献者的资金、凭证、数据、代码审查与验证纪律 |
| [`project_positioning.md`](project_positioning.md) | 现行原则 | 项目目标与术语解释口径；不承担运行状态说明 |
| [项目代码审查与系统说明](code_review/README.md) | 现行代码基线 | 从入口到模块的完整说明、API/表/Topic/测试清单、文档漂移 |
| [上线前本地测试指南](testing/README.md) | 现行操作说明 | 本地静态、单元、场景、WSL2 集成、模拟运行与现场门 |
| [文档治理规范](DOCUMENTATION_GOVERNANCE.md) | 现行约束 | 放置、命名、状态、复核、迁移和验收规则 |
| [文档纠错审计报告](code_review/DOCUMENTATION_AUDIT.md) | 现行审计记录 | 本轮纠错范围、代码事实、验证方法与未改代码风险 |

## 4. 现行专题文档

### 测试

- [`testing/README.md`](testing/README.md)
- [`../CONTRIBUTING.md`](../CONTRIBUTING.md)
- [`../CLAUDE.md`](../CLAUDE.md) 的测试命令与环境约束

### 配置

- [`configuration/README.md`](configuration/README.md)
- [`../configs/README.md`](../configs/README.md)

### RDP

- [`rdp/README.md`](rdp/README.md)
- [`../aats/data_platform/README.md`](../aats/data_platform/README.md)
- [`operations/platform_runbook.md`](operations/platform_runbook.md)
- [`operations/rdp_scheduling_strategy.md`](operations/rdp_scheduling_strategy.md)
- [`operations/rdp_workflow_calendar.md`](operations/rdp_workflow_calendar.md)
- [`operations/workflow_failure_recovery.md`](operations/workflow_failure_recovery.md)

### 参数与 Operator

- [`operations/README.md`](operations/README.md)（Operations 全量索引与状态）
- [`operations/operator_checklist.md`](operations/operator_checklist.md)
- [`operations/rdp_operator_workflow.md`](operations/rdp_operator_workflow.md)
- [`operations/parameter_governance.md`](operations/parameter_governance.md)
- [`operations/parameter_apply_and_rollback.md`](operations/parameter_apply_and_rollback.md)
- [`operations/production_parameter_change_runbook.md`](operations/production_parameter_change_runbook.md)

### WSL2 与基础设施

- [`../deploy/wsl2-dev/README.md`](../deploy/wsl2-dev/README.md)
- [`operations/wsl2_sync_workflow.md`](operations/wsl2_sync_workflow.md)
- [`operations/wsl2_startup_prewarm.md`](operations/wsl2_startup_prewarm.md)
- [`operations/safe_shutdown_design_2026_04_20.md`](operations/safe_shutdown_design_2026_04_20.md) 是实现设计；实际停机命令以 `DEPLOYMENT.md` 为准。

## 5. 专题参考

下列材料可以解释某个子域，但使用前要与当前代码核对：

- `docs/operations/` 中没有 Stage/Phase/固定日期语义的指标、artifact、schema、alert、round、periodic review 文档；
- `docs/rdp/phase2_parameter_research_details.md`、`phase3_4_attribution_execution_details.md`；
- `docs/governance/`、`docs/research/`；
- `docs/audit/` 与 `docs/review/` 的结论，用于了解当时发现，不是永久“已修复/未修复”状态。

## 6. 历史材料

以下目录或命名默认是历史证据，不承担当前操作职责：

| 范围 | 语义 |
| --- | --- |
| `docs/task/` | 任务书、SOW、实施记录和阶段性交付 |
| `docs/design/` | 设计提案；可能未实施、部分实施或已被替代 |
| `docs/review/`、`docs/audit/` | 某个 commit/日期的审查快照 |
| `docs/autonomous_sessions/`、`docs/weekly_review/` | 会话/周报历史 |
| `docs/knowledge_graph/` | 2026-04-21、HEAD `0ef6f1c` 的系统快照，已整体标为历史 |
| 文件名含 `stage`、`phase`、`roadmap`、`release_notes`、固定日期/窗口 | 一次性阶段材料，除非本页另行列为现行 |
| `deploy/wsl2-dev/RUNBOOK.md` | 早期 Stage 1-9 实跑记录，已明确标为历史，禁止作为当前部署入口 |
| `docs/` 根层既有 SOW/任务文件 | 为提交、Issue、审计和外部链接兼容而保留；禁止新增同类文件 |

历史文档中的命令、端口、stream 数量、表数量、workflow 数量、配置优先级和完成状态都可能过期。

## 7. 当前必须牢记的漂移修正

- 主交易是 4 个交易 slice；标准 derivatives-live 另有 rdp-daemon 和两个采集 daemon，共 7 个应用容器。
- profile 模板端口：spot 8000、derivatives 8001、spot-live 8010、derivatives-live 8011。
- 本地 `start_api.py` 是 HTTP；标准 live 部署由 deploy script 配置 HTTPS。
- `scripts/run_local.py` 当前签名已漂移，不能作为可用 paper loop。
- JetStream 是 3 条 stream，全部 1 天上限/兜底；总声明容量 6.5 GiB，server 8 GiB。
- RDP ORM 是 78 张表，不是 48 张。
- RDP workflow 是 10 个定义、8 个 enabled；decision/release disabled，release 还禁止入队。
- runtime active parameter 是 Postgres DB-only；JSON 文件不是 fallback。
- `apply_active_parameter_set.py`、`approve_recommendation_and_apply.py`、`rdp_rollback_active_parameter_set.py`、`rdp_freeze_parameter_set.py`、`rdp_run_release_cycle.py` 已禁用。
- `deploy.sh` 自动健康门尚未覆盖 derivatives-live 的两个采集器，需单独验证。

## 8. 易漂移事实与代码真源

| 事实 | 代码/配置真源 | 文档维护触发器 |
| --- | --- | --- |
| profile 身份、模拟/实盘、保证金/持仓模式 | `aats/bootstrap/managed_profiles.py` | managed definition 变化 |
| profile 端口模板 | `scripts/generate_managed_config_artifacts.py`、`configs/templates/` | generator/template 变化 |
| 配置优先级与 active parameter | `aats/bootstrap/config.py`、`aats/bootstrap/active_parameters.py` | loader/build runtime 变化 |
| 应用进程与容器 | `apps/`、`deploy/wsl2-dev/docker-compose*.yml` | service/overlay 变化 |
| 部署与停机 | `scripts/deploy.sh`、`scripts/ops/safe_shutdown.sh` | 阶段、profile、health gate 变化 |
| JetStream 数量、subject、容量、保留 | `aats/bus/nats_bus.py`、`deploy/wsl2-dev/nats/nats-server.conf` | `StreamSpec` 或 server budget 变化 |
| RDP 表和 schema | `aats/data_platform/rdp_models.py`、migrations | ORM/migration 变化 |
| RDP workflow、schedule、timeout、enqueue block | `configs/rdp_workflows/*.json`、`rdp_task_db.py`、`workflow_scheduler.py`、`rdp_task_daemon.py` | 任一集合/调度语义变化 |
| HTTP API 与认证 | FastAPI route registry、`aats/api/auth*.py` | route/dependency/middleware 变化 |
| 真实账户与运行健康 | 当前受控 UI/API、只读数据库和标准 health checks | 每次操作都重新读取，禁止缓存到长期文档 |

## 9. 维护规则

1. 当前行为变更必须同步更新对应现行入口。
2. 不在 README 保存“最近测试通过 N 个”这类快速失效快照；测试结果写入当次交付说明。
3. 新 workflow 同步 JSON、allowlist、daemon timeout、日历和测试。
4. 新 ORM 表同步 migration、模型计数和 RDP 总览。
5. 新 RDP route 同步 OpenAPI/系统说明；不要手工维护不完整的“全部 API”表。
6. 历史文档保留原始事实；若可能被误用，在顶部增加醒目的历史/替代说明，而不是篡改当时记录。
7. 现行文档超过本页时效阈值后，即使内容尚未证明错误，也必须标为“待复核”或完成当前基线复核后更新日期。
8. 文档不能用“服务正常”“账户一致”“已部署”等无现场证据的现在时；静态验证、运行时验证和未知项必须分开陈述。
9. 新目录和两份以上/混合生命周期的文档目录必须提供 `README.md`；目录使用小写 `snake_case`，禁止新增带空格目录。
10. 详细放置、命名、迁移和验收规则见 [`DOCUMENTATION_GOVERNANCE.md`](DOCUMENTATION_GOVERNANCE.md)。
