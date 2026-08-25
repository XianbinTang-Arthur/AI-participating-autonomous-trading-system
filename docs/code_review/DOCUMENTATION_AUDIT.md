# AATS 文档纠错与治理审计报告

> 初始审计日期：2026-08-22 至 2026-08-23
> 初始代码基线：`be9179ead5be6aba22fbe94e3baf72b9f46eedc3`（`main`）
> 整改覆盖层最后核对：2026-08-25（起始 HEAD `00b6df0f8a8d2665d6cae3e88996843767cd1f56`；Phase 3A–3W 整改提交候选）
> 初始审计对象：基线内 699 个 Git 跟踪 Markdown 文档、`docs/task/` 下两份 legacy Word 文档，以及与文档语义直接相关的代码注释和基础设施注释
> 审计原则：当前可执行代码、迁移、配置生成器、Compose 声明和部署脚本优先；数据库/运行时现场状态未读取，不把静态检查冒充实盘验证

## 1. 结论

本轮已建立“现行规范—专题参考—历史证据”三层文档体系，并对仍承担项目入口、配置、部署、运维、RDP、Operator 和模块参考职责的文档进行逐项代码对照。发现的错误主要集中在：profile 语义与端口、配置优先级、运行时参数真源、进程拓扑、JetStream 数量与保留期、RDP 表/工作流/API 数量、已经禁用的治理脚本、TLS/HTTP 边界以及历史 runbook 被误当作现行操作手册。

处理结果：

1. 新增当前文档地图，统一冲突裁决顺序和历史材料边界；
2. 重写或校正当前入口、架构、部署、配置、RDP 与运维文档；
3. 对容易被误操作的历史 runbook、roadmap、观察记录和 knowledge graph 增加醒目的历史声明；
4. 删除现行文档中的危险性删除/reset 建议、失效 memory 引用、过期路由和已禁用 CLI；
5. 修正代码/基础设施中的过期注释，使其不再与实际 JetStream 配置冲突；
6. 保留历史文件的原始事件和当时结论，不把今天的实现倒写进历史记录。
7. 将带空格的 `docs/code review/` 规范化为 `docs/code_review/`，并修复仓库内全部已发现引用；
8. 新增文档治理规范、上线前本地测试指南、归档政策，以及缺失的主要目录入口；
9. 将 `docs/` 根层的既有任务/SOW 明确为路径兼容保留区，停止继续堆积，同时避免破坏历史审计与外部链接。
10. 对两份 2026-03 legacy Word 蓝图完成结构化只读检查，并在 `docs/task/README.md` 标出已漂移内容与当前替代入口；原件保持历史不变。
11. Phase 3R 重写 `parameter_mapping_reference.md`，移除已失效的 18 项计数、
    signal/stability“未映射”和 directional placeholder 表述，并记录 FS-015 short-bias
    replay gate、21/3 个当前映射及运行时未知边界。
12. Phase 3S 新增仓库级最小权限 CI 与当前测试说明，明确区分 workflow 代码、远端
    required-check 状态和未覆盖的 integration/security/供应链门禁；不把本地静态契约
    写成 GitHub 已运行成功。
13. Phase 3T 新增目标平台 Python lock、hash 消费规则和外部镜像 digest 文档；同步纠正
    当前基础设施说明中的 Grafana `10.4.4` 为 Compose 实际 `12.4.3`，并明确 APT、SBOM、
    扫描、clean build 与远端 CI 仍未验证。
14. Phase 3U 新增数据库连接预算单一真源、engine inventory 和容量边界说明，不把静态
    47 个名义余量冒充为目标负载已验证。
15. Phase 3V 同步 Research Factory v2 的 train/valid 双门、test 内容 seal 与
    `sealed_not_evaluated` lineage；明确最终 OOS、历史 v1 artifact 污染、walk-forward、
    多重检验和独立复核仍未完成。
16. Phase 3W 复审全部整改候选后，纠正本地单进程入口实际只构建 Gateway slice、Compose
    内嵌手工启动/销毁命令与唯一部署入口冲突、CI warning allowlist 语义错误；同步记录
    非有限值边界与测试数据库确定性释放。目标 WSL2/Compose 运行事实仍不由静态文档代替。

## 2. 文档适用性模型

| 等级 | 定义 | 使用规则 |
| --- | --- | --- |
| 现行规范 | 项目入口、配置、部署、运维和模块总览 | 应与当前代码同步，可用于维护和只读排查 |
| 专题参考 | schema、指标、artifact、研究或子模块说明 | 使用前必须与当前代码/迁移核对 |
| 历史证据 | task、design、audit、review、stage/phase、release note、观察窗口 | 仅解释当时背景，不得直接用于当前实盘操作 |

完整入口和具体文件清单见 [`../README.md`](../README.md)。冲突时按“代码与迁移 → 现行规范 → 专题参考 → 历史材料”裁决。

## 3. 已纠正的核心事实

| 主题 | 过期/错误表述 | 当前代码事实 |
| --- | --- | --- |
| 运行拓扑 | 单体 API 或仅 4 个容器代表整个 derivatives | 4 个交易 slice；`derivatives` 模拟另有 `rdp-daemon`、`liquidations-daemon`、`microstructure-collector`，共 7 个应用进程；live 仍禁用 |
| profile 端口 | spot/derivatives/live 端口混用 | spot 8000、derivatives 8001、spot-live 8010、derivatives-live 8011 |
| profile 语义 | derivatives 使用 hedge，或 live 使用 net | derivatives 是 simulated/cross/net；derivatives-live 是 real/cross/hedge |
| 本地 API | `start_api.py` 使用 HTTPS | 本地入口是 HTTP；标准 live 部署由部署脚本准备 TLS 并使用 HTTPS |
| 本地 paper loop | `scripts/run_local.py` 可直接运行 | Phase 3Q 后该脚本只输出迁移指引并 exit `2`，不加载 profile/runtime；不是受支持 paper loop |
| 配置优先级 | 环境变量可任意覆盖 managed identity，active JSON 可兜底 | defaults → managed defaults → profile YAML → allowlisted env → runtime 构建；managed identity 环境变量被忽略；active parameter 为 Postgres DB-only |
| 参数映射 | independent 只有 18 项；signal/stability 未映射；directional trend weight 占位映射到 alpha 门槛 | independent 21 个 required 映射；directional 3 个实际映射且仅 min-hold required；trend weight 无生产映射；short-bias 是 replay 上下文快照 |
| JetStream | 2 条 stream、7 天保留 | 3 条 stream，1 天上限/兜底；声明容量 2 GiB + 4 GiB + 512 MiB = 6.5 GiB，server 为 8 GiB |
| 审计事件 | `audit.records` 是 JetStream topic | 它是 persist-only 路径，落 Postgres，不属于 3 条 stream |
| RDP 表 | 48/78 张表 | 当前 ORM metadata 为 81 张表 |
| 工作流 | 少于 10 个，decision/release 按计划运行 | 10 个定义、8 个 enabled；decision/release disabled，release 禁止入队 |
| 参数变更 | 旧脚本可 freeze/apply/rollback/release | 多个旧脚本已硬禁用；当前生产变更必须走受控 API/UI、权限、完整性检查、安全门和审计 |
| apply token | 所有 release/apply 组合路由都强制 token | 直接 apply/rollback 路由强制 `X-Rdp-Apply-Token`；当前 create-release 与 approve-and-release 组合路由依赖写权限和 Step 2 gate，但未绑定该 token dependency |
| API 规模 | 手工枚举的旧路由表等于当前 API | 当前 FastAPI 共 193 个路由；排除 4 个框架文档路由和 20 个 UI shell/静态入口后为 169 个 API 路由；`/rdp/*` 为 50 个。具体集合以 OpenAPI/代码为准 |
| 部署健康门 | derivatives-live 的 7 个应用进程都被部署脚本强制检查 | future required list 已含 7 个应用进程，但所有 live profile 当前在副作用前禁用，目标 health/freshness 尚未验证 |
| 安全停机 | 手工 `docker compose down`/restart | 当前受支持入口是 `scripts/ops/safe_shutdown.sh`，默认 dry-run；部署仍只走 `scripts/deploy.sh` |

## 4. 本轮修改范围

### 4.1 新增

- `docs/README.md`：当前文档地图、冲突裁决顺序、历史边界和维护规则；
- `docs/operations/README.md`：逐文件区分现行操作、专题参考和历史证据，并给出当前替代入口；
- `docs/code_review/README.md`：从入口到交易、状态、资金、RDP、API、存储和部署的代码库全景说明；
- 本报告：记录纠错范围、证据和未冒充完成的运行时验证。

### 4.2 重写或系统校正

- 根入口：`README.md`、`ARCHITECTURE.md`、`DEPLOYMENT.md`、`CLAUDE.md`、`CONTRIBUTING.md`；
- 配置：`configs/README.md`、`docs/configuration/managed-config-reference.md`；
- RDP 总览：`aats/data_platform/README.md`、`docs/rdp/module_reference.md`；
- RDP/Operator 运行手册：`platform_runbook.md`、`parameter_governance.md`、`parameter_apply_and_rollback.md`、`production_parameter_change_runbook.md`、`rdp_operator_workflow.md`、`workflow_failure_recovery.md`、`rdp_scheduling_strategy.md`、`rdp_workflow_calendar.md`；
- 基础设施：`deploy/wsl2-dev/README.md`、`wsl2_sync_workflow.md`、Grafana/可靠性/指标/artifact 等现行专题说明。

### 4.3 明确降级为历史证据

- `docs/knowledge_graph/README.md` 与 10 个分册；
- `deploy/wsl2-dev/RUNBOOK.md`；
- multiprocess roadmap、Stage 7、P1-D、Route A observation、release notes、safe-shutdown 原始设计等固定阶段材料。

这些文件没有被删除，因为它们仍有审计和演进价值；顶部声明明确其基线、替代入口和禁止直接执行的边界。

### 4.4 同步修正的非 Markdown 注释

- `aats/bus/nats_bus.py`：将“2 条/7 天”等过期说明改为当前 3 条/1 天/6.5 GiB；
- `aats/bootstrap/config.py`：同步当前三条 stream 的名称、容量和 1 天边界；
- `apps/api_gateway/main.py`：移除已经失效的 RDP 表数量注释；
- `aats/services/execution_engine/{fill_event_cache,obligation_cache,order_state_cache}.py`：区分 7 天 Redis 热缓存窗口与 1 天 JetStream hot buffer；
- `scripts/rdp_task_daemon.py`：明确标准运行形态是 Compose 管理的 daemon 容器，直接前台运行仅用于受控诊断；
- `deploy/wsl2-dev/nats/nats-server.conf`：同步当前容量预算和保留语义。

上述 7 个 Python 文件和 1 个 NATS 配置文件仅改注释/docstring，不改变可执行逻辑或配置值。

## 5. 静态验证方法

| 检查 | 判定标准 |
| --- | --- |
| 相对链接 | 现行文档中的本地相对链接均能解析到文件或目录 |
| RDP 路由 | 文档中出现的 `/rdp/...` 路径必须与 FastAPI 当前注册路由匹配；参数占位符规范化后比较 |
| profile/端口 | 对照 profile generator、YAML 和 Compose overlay |
| 配置优先级 | 对照 managed config loader、runtime builder 和 active parameter registry |
| stream | 对照 `nats_bus.py` 的 stream specs 和 NATS server 容量 |
| workflow | 对照 10 个 workflow JSON、allowlist、scheduler 和 daemon timeout |
| ORM 表 | 由当前 `Base.metadata.tables` 计算，不依赖旧文档计数 |
| API 数量 | 从当前 FastAPI app 的注册 routes 计算 |
| UTF-8 | 修改/新增文本必须可按 UTF-8 解码 |
| Git diff | `git diff --check` 不允许新增尾随空白或冲突标记 |

## 6. 本次验证结果

| 验证 | 结果 |
| --- | --- |
| 全仓 Markdown 本地链接与图片 | 699 个跟踪文档 + 4 个本次新增文档，共 703 个文件；失效目标 0 |
| 现行文档元数据 | 30 个现行入口、约束、操作和模块文档均带 2026-08-22 核对日期及 `be9179e` 基线 |
| 现行文档 RDP method/path 引用 | 166 个带 HTTP method 的引用；与 50 个当前 RDP 路由比较，未匹配 0 |
| 静态事实断言 | profile/端口、3 stream/6.5 GiB、81 表、10/8 workflow、release enqueue block、193/169/50 route 全部通过 |
| Python lint | 仅检查本轮改到注释/docstring 的 7 个 Python 文件；Ruff 通过 |
| 针对性单元测试 | NATS、managed/runtime profile、DB-only active parameter、scheduler/task queue、API、apply token、approve/release；215 passed，6 subtests passed |
| Diff hygiene | `git diff --check` 通过；仅有仓库既有 Windows line-ending 提示，无 whitespace error |

没有运行 WSL2 integration、容器、数据库 migration、部署、交易所调用或实盘动作；它们不属于文档静态纠错的成功证明。

## 7. 未改代码的已知风险

以下问题已经在现行文档中明确，但本轮按“修正文档”范围没有修改交易系统代码：

1. `scripts/run_local.py` 的旧调用已收口为无配置副作用的迁移失败入口；仓库外调用方迁移仍需复核；
2. `deploy.sh` 的 future required list 已包含 derivatives-live 两个采集器，但 live 当前禁用，目标 Compose health/freshness 与告警仍未验证；
3. create-release 与 approve-and-release 组合路由未绑定 direct apply/rollback 使用的短期 token dependency；
4. reliability config checker 当前只覆盖部分 workflow 配置，并仍检查一个 legacy active artifact；
5. 静态一致性不能证明 WSL2、容器、数据库、交易所、真实账户或实盘资金链当前健康。
6. FS-015 代码已收口 short-bias 关闭语义，但历史 replay artifact 尚未按显式目标 gate
   重跑，committed candidate 独立复核仍未完成。
7. FS-021 已新增基础 lint/unit/warning workflow，FS-022 已加入 Python hash lock 和镜像
   digest；但远端 required check、integration、APT snapshot、clean build、SBOM、
   security/license/provenance scan 仍未完成。本机 Python 3.14 结果不能替代目标
   Python 3.12 workflow 与 Linux/Docker 实跑。
8. FS-008 已建立角色化数据库连接预算、声明 topology ceiling=150 和 engine inventory；
   但目标全拓扑负载、慢查询/故障重连、transient/CLI/迁移/恢复/admin 路径、告警和
   `work_mem` 联合内存预算仍未验证，静态 47 个名义余量不能表述为现场容量已通过。
9. FS-004 当前 v2 runner 已把 test 从 candidate selection 隔离并写内容 seal；但历史 v1
   artifact/人工查看未审计，最终一次性 OOS、holdout access ledger、walk-forward、
   multiple-testing correction 与独立复核未完成，不能写成研究证据已可用于实盘。

这些风险需要独立的设计、审批、实现与测试任务，不能通过修改文档被视为已经修复。

## 8. 后续维护纪律

1. 改 profile、端口、topology、stream、workflow、ORM、route 或参数治理路径时，同一提交更新现行文档；
2. 不在长期 README 固化一次性测试数量、余额、容器状态或账户快照；
3. 不把 design/task 中的目标态写成当前能力；
4. 历史 runbook 保留证据，但必须链接当前替代入口；
5. 任何涉及实盘变更的命令都必须重新从当前代码、权限和安全门验证，不根据历史记录直接执行。
