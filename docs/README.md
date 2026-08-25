# AATS 文档地图与适用边界

最后核对：2026-08-25（起始 HEAD `00b6df0f8a8d2665d6cae3e88996843767cd1f56`；包含 Phase 3A–3W 与收益证据/模拟漏斗整改，以本文档所在 HEAD 为准）

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
| [真实收益差距评估与落地路线](code_review/profitability_gap_assessment_2026_08_25.md) | 现行代码审查与收益判断 | 当前候选、模拟执行、统计/成交/前向/live 门禁的实际差距与实施顺序 |
| [收益证据与模拟交易就绪运行手册](operations/profit_readiness_runbook.md) | 现行操作说明 | 公共微观结构、v2 候选、L2、holdout、参数代次、故障矩阵和 readiness |
| [收益可信度整改验收矩阵](testing/profit_readiness_acceptance.md) | 现行测试说明 | 静态、单元、WSL2、模拟运行及明确 NO-GO 边界 |

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
- [`operations/parameter_mapping_reference.md`](operations/parameter_mapping_reference.md)

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

- 主交易是 4 个交易 slice；derivatives-live Compose 定义另有 rdp-daemon 和两个采集 daemon，共 7 个应用容器，但该 live profile 当前不可部署。
- profile 模板端口：spot 8000、derivatives 8001、spot-live 8010、derivatives-live 8011。
- 本地 `start_api.py` 是 HTTP，只接受模拟 profile 与 loopback host；live TLS 配置仍保留，但当前 deploy/prewarm/wrapper/local launcher 都禁止 live。
- `scripts/run_local.py` 现为明确迁移失败入口：不加载 profile/runtime，输出指引并 exit `2`；不是可用 paper loop，仓库外旧调用方仍需迁移。
- JetStream 是 3 条 stream，全部 1 天上限/兜底；总声明容量 6.5 GiB，server 8 GiB。
- RDP ORM 是 81 张表，不是历史材料中的 48/78 张。
- RDP workflow 是 10 个定义、8 个 enabled；decision/release disabled，release 还禁止入队。
- runtime active parameter 是 Postgres DB-only；JSON 文件不是 fallback。
- `apply_active_parameter_set.py`、`approve_recommendation_and_apply.py`、`rdp_rollback_active_parameter_set.py`、`rdp_freeze_parameter_set.py`、`rdp_run_release_cycle.py` 已禁用。
- `deploy.sh` 没有默认 profile，只允许 `spot`/`derivatives` 模拟部署；future derivatives-live required list 已包含两个采集器，但当前 live 禁用且没有运行结论。
- 2026-08-24 未提交整改工作区已把 replay/backtest 固定为
  `next_bar_event_v2`；旧 same-bar-close 回测结果全部失效，只有带模型版本和
  `execution_timeline.json` 的新产物才具备时间因果审计基础，但仍不能证明真实
  盘口成交。详见 [`task/fs_003_backtest_causal_timing_sow_2026_08_24.md`](task/fs_003_backtest_causal_timing_sow_2026_08_24.md)。
- 2026-08-24 未提交整改工作区已增加 FS-006 显式关键 task 监督：daemon 的
  critical task 非预期结束会停止 heartbeat 并非零退出，FastAPI `/healthz`
  对已失败关键 task 返回 `503`。Phase 3K 又为账户刷新、执行同步、对账、
  outbox、command flow、Phase 1 shadow 与 trial guard 七条固定周期任务加入成功
  进度 deadline；永久 await 或连续无成功周期会分类为 `stalled` 并走同一失败
  路径。事件驱动任务、event-loop 整体阻塞、真实容器 restart/告警与依赖故障
  注入仍未验证，不能据此宣称 trading-ready。详见
  [`task/fs_006_critical_task_supervision_sow_2026_08_24.md`](task/fs_006_critical_task_supervision_sow_2026_08_24.md)
  和 [`task/fs_006_critical_task_progress_watchdog_sow_2026_08_24.md`](task/fs_006_critical_task_progress_watchdog_sow_2026_08_24.md)。
- 2026-08-24 未提交整改工作区已收紧 FS-009 schema 所有权：
  managed 应用启动只读校验 root/RDP ledger 和 schema contract，部署在
  app up 前以一次性 job 执行 root migrations + ORM baseline + 全 Batch B，
  RDP stage 用 version/checksum/advisory lock 记账且 DDL 与 ledger 原子提交。
  Gateway 在任何 readiness/后台 task 前校验，失败不对外 ready。
  空库/历史克隆/部分失败 manifest 和 app+schema rollback 仍未运行，
  所以不得声称生产 schema 已一致。详见
  [`task/fs_009_schema_single_truth_sow_2026_08_24.md`](task/fs_009_schema_single_truth_sow_2026_08_24.md)。
- 2026-08-24 Phase 3F 已把 FS-007 确认风险改为代码层 containment：profile
  必填，live 无 override 且在副作用前失败，Compose 关键步骤非零即停止，模拟部署
  只生成 `production_ready=false` 的脱敏证据包。完整 runtime readiness 与
  app+schema+parameter 一致回滚仍 OPEN，详见
  [`task/fs_007_deployment_fail_closed_sow_2026_08_24.md`](task/fs_007_deployment_fail_closed_sow_2026_08_24.md)。
- 2026-08-24 Phase 3G 已把 Gateway Compose 宿主映射固定为 `127.0.0.1`，
  本地启动器拒绝 live/非 loopback，模拟 evidence 校验实际 Docker HostIp。
  现有容器、目标防火墙、VPN/NAT、证书与外部不可达性仍 UNKNOWN，详见
  [`task/fs_005_gateway_loopback_containment_sow_2026_08_24.md`](task/fs_005_gateway_loopback_containment_sow_2026_08_24.md)。
- 2026-08-24 Phase 3H 已为 Gateway 加入固定 Host allowlist 和统一浏览器
  安全响应头；CSP 不依赖 `unsafe-inline`/`unsafe-eval`，HSTS 只对实际 HTTPS
  scope 输出。真实 TLS terminator、proxy、目标浏览器与未捕获 500 响应边界仍
  需运行验证，详见
  [`task/fs_020_browser_security_headers_sow_2026_08_24.md`](task/fs_020_browser_security_headers_sow_2026_08_24.md)。
- 2026-08-24 Phase 3I 已把 Operator 登录的同步 DB/PBKDF2/账户状态/审计链
  完整移出 event loop，以每进程有界 worker、排队超时、global/client/identity
  滑动窗口、dummy KDF 和输入上限失败关闭。多进程集中限流、trusted proxy、
  真实数据库和目标负载仍 OPEN，详见
  [`task/fs_019_operator_login_async_isolation_sow_2026_08_24.md`](task/fs_019_operator_login_async_isolation_sow_2026_08_24.md)。
- 2026-08-24 Phase 3J 已将四主进程 NATS/hybrid peer readiness 收紧为
  generation-scoped 失败关闭；旧 key、Redis 异常、peer timeout 或缺代次都
  不能启动 publisher。标准模拟 deploy 生成/注入同一代次并记入证据包；
  真 Redis/NATS/Compose 启动、重启和断连矩阵仍 OPEN，详见
  [`task/fs_016_nats_peer_readiness_fail_closed_sow_2026_08_24.md`](task/fs_016_nats_peer_readiness_fail_closed_sow_2026_08_24.md)。
- 2026-08-24 Phase 3L 已把 Kill Switch 长期恢复状态与在线增险许可拆分：
  Gateway/monolith 维护同 generation 的 15 秒 Redis permission，execution 只在最终
  submission fence 读取且不能续租。旧 RUNNING authority 不能替代过期 permission；
  真 Redis/NATS 四进程单向分区、crash/restart、目标告警和独立复核仍 OPEN，详见
  [`task/fs_002_short_lived_trading_permission_lease_sow_2026_08_24.md`](task/fs_002_short_lived_trading_permission_lease_sow_2026_08_24.md)。
- 2026-08-24 Phase 3M 已把 profile recommendation apply 与 rollback 都收紧为
  授权/状态/双签校验后的无写入 `501`。approve/release 不代表 runtime 生效，历史
  apply Saga 不再由 route 调用；真实 generation、worker ack/readback、反向 Saga 与
  历史漂移对账仍 OPEN。详见
  [`task/fs_001_profile_apply_fail_closed_sow_2026_08_24.md`](task/fs_001_profile_apply_fail_closed_sow_2026_08_24.md)
  和 [`../audit/full_system_2026_08_24/33-fs-001-profile-apply-fail-closed.md`](../audit/full_system_2026_08_24/33-fs-001-profile-apply-fail-closed.md)。
- 2026-08-24 Phase 3N 已将回测 fill 固定为 `ohlcv_participation_cap_v2`：
  三类订单都要求正 volume 并受默认 1% cap，IOC/bounded 只使用下单前已知的
  observation volume，bounded 按 taker fee + fixed slippage，成本和 scorecard
  明示 fee/slippage、OHLCV 粒度与 L2/queue/impact 限制。它只收敛 bar proxy，
  不构成 live 容量/收益证明；FS-014/G3 仍 OPEN。详见
  [`task/fs_014_ohlcv_fill_realism_containment_sow_2026_08_24.md`](task/fs_014_ohlcv_fill_realism_containment_sow_2026_08_24.md)
  和 [`../audit/full_system_2026_08_24/34-fs-014-ohlcv-fill-realism-containment.md`](../audit/full_system_2026_08_24/34-fs-014-ohlcv-fill-realism-containment.md)。
- 2026-08-25 Phase 3O 已将 Dashboard 详情抽屉改为原生 modal dialog，补齐
  accessible name/description、初始/返回焦点、Escape/backdrop/按钮统一关闭；
  reduced-motion 同时覆盖 CSS 动画/过渡/滚动和 JavaScript smooth scroll。
  目标浏览器、keyboard-only、NVDA/VoiceOver、axe、缩放和动效人工验证仍 OPEN。
  详见 [`task/fs_017_fs_018_dashboard_accessibility_sow_2026_08_25.md`](task/fs_017_fs_018_dashboard_accessibility_sow_2026_08_25.md)
  和 [`../audit/full_system_2026_08_24/35-fs-017-fs-018-dashboard-accessibility.md`](../audit/full_system_2026_08_24/35-fs-017-fs-018-dashboard-accessibility.md)。
- 2026-08-25 Phase 3P 已删除四个 managed profile 中无 Settings 字段/消费者的伪
  auto-rollback key，并让 strategy YAML 非 mapping 或未知 key 在 managed loader
  失败关闭；配置 reference 与 generator 一致，生成器不再覆盖人工治理 README。
  committed candidate 目标启动、仓库外 overlay、generator clean-run 与独立复核仍 OPEN。
  详见 [`task/fs_010_managed_profile_unknown_key_fail_closed_sow_2026_08_25.md`](task/fs_010_managed_profile_unknown_key_fail_closed_sow_2026_08_25.md)
  和 [`../audit/full_system_2026_08_24/36-fs-010-managed-profile-unknown-key-fail-closed.md`](../audit/full_system_2026_08_24/36-fs-010-managed-profile-unknown-key-fail-closed.md)。
- 2026-08-25 Phase 3Q 已把失效的 `scripts/run_local.py` 改为无配置副作用的迁移
  失败入口：识别旧参数、输出当前 API/UI 与 integration 指引并 exit `2`，不加载
  `.env.*` 或 runtime。committed candidate 独立复核与仓库外调用方迁移仍 OPEN。
  详见 [`task/fs_011_legacy_run_local_fail_closed_sow_2026_08_25.md`](task/fs_011_legacy_run_local_fail_closed_sow_2026_08_25.md)
  和 [`../audit/full_system_2026_08_24/37-fs-011-legacy-run-local-fail-closed.md`](../audit/full_system_2026_08_24/37-fs-011-legacy-run-local-fail-closed.md)。
- 2026-08-25 Phase 3R 已把 independent replay 的 short-bias gate 与生产收口：
  `strategy_short_bias_enabled=false` 时在 score history/dominant-leg 之前把 short score
  固定为 `0.0`，同名布尔值进入 replay artifact；它是目标 profile 上下文而不是
  active-parameter 调优项。历史回测尚未按显式 gate 值重跑，独立复核仍 OPEN。
  详见 [`task/fs_015_replay_short_bias_parity_sow_2026_08_25.md`](task/fs_015_replay_short_bias_parity_sow_2026_08_25.md)
  和 [`../audit/full_system_2026_08_24/38-fs-015-replay-short-bias-parity.md`](../audit/full_system_2026_08_24/38-fs-015-replay-short-bias-parity.md)。
- 2026-08-25 Phase 3S 已新增最小权限 GitHub Actions 基础门禁：Python 3.12
  全仓 Ruff、完整 unit、strict markers 与新增 warning 失败；Long/Short 错误 AsyncMock
  已修正。Phase 3T 又为 CI/运行时加入目标平台完整 hash lock，并固定 Python 基础镜像与
  九个外部 Compose image digest。远端运行/required check、integration、安全扫描、APT、
  SBOM 与 clean build 仍 OPEN。详见
  [`task/fs_021_ci_quality_gate_sow_2026_08_25.md`](task/fs_021_ci_quality_gate_sow_2026_08_25.md)、
  [`task/fs_022_reproducible_dependencies_sow_2026_08_25.md`](task/fs_022_reproducible_dependencies_sow_2026_08_25.md)
  和 [`../audit/full_system_2026_08_24/40-fs-022-reproducible-dependencies.md`](../audit/full_system_2026_08_24/40-fs-022-reproducible-dependencies.md)。
- 2026-08-25 Phase 3U 已把主交易和 RDP 相关 SQLAlchemy pool 上限收敛到单一真源：
  四进程声明 topology ceiling=150、Compose 普通容量=197、名义余量=47；AST verifier
  归类当前 13 个 `create_engine` 调用并接入 CI。该结果不是目标负载或全局 runtime cap；
  transient/CLI/迁移/恢复/admin、故障重连、告警和联合内存仍 OPEN。详见
  [`task/fs_008_database_connection_budget_sow_2026_08_25.md`](task/fs_008_database_connection_budget_sow_2026_08_25.md)
  和 [`../audit/full_system_2026_08_24/41-fs-008-database-connection-budget.md`](../audit/full_system_2026_08_24/41-fs-008-database-connection-budget.md)。
- 2026-08-25 Phase 3V 已把 Research Factory real-data v2 的 candidate selection 与 test
  隔离；本次收益可信度整改又增加历史候选不可用审计、确定性 v2 计划、purged
  walk-forward、block bootstrap、Holm、deflated Sharpe、一次性 holdout 账本、L2 event
  replay 和 paper calibration。代码与单元契约不等于候选已完成最终 OOS；当前尚无候选专用
  holdout 运行结果、worker 参数读回或完整故障矩阵，因此生产仍 NO-GO。详见
  [`task/fs_004_research_selection_holdout_sow_2026_08_25.md`](task/fs_004_research_selection_holdout_sow_2026_08_25.md)
  、[`task/profit_readiness_full_delivery_sow_2026_08_25.md`](task/profit_readiness_full_delivery_sow_2026_08_25.md)
  和 [`operations/profit_readiness_runbook.md`](operations/profit_readiness_runbook.md)。
- 2026-08-25 Phase 3W 完成起始基线以来全量候选变更复审，补齐登录/Kill Switch/
  回测成交的非有限值边界、本地 monolith 入口、CI warning filter 和 SQLite 测试资源释放，
  并删除当前 Compose 注释中的手工运维误导。Windows 严格全量单测为
  `4423 passed, 30 skipped, 94 subtests passed`；WSL2 集成和模拟栈运行事实仍须现场验证。
  详见 [`../audit/full_system_2026_08_24/43-phase3w-post-audit-full-change-review.md`](../audit/full_system_2026_08_24/43-phase3w-post-audit-full-change-review.md)。
- 2026-08-25 收益证据整改已把 development return series、完整 campaign、全试验计数、
  重复假设、bootstrap/Holm/DSR 串成不可覆盖证据链。实际 10 个计划中仅 3 个代表候选
  具备 return series，三者全部负收益且统计失败，holdout 正确保持封存。模拟漏斗另修复
  allocator 预算只缩金额不缩 qty 的问题；代码与部署已通过，但部署后首批 25 个自然目标均为
  flat/0，订单/成交运行证据仍为 `UNKNOWN`。正式 NO-GO 和后续硬门见
  [`code_review/profitability_gap_assessment_2026_08_25.md`](code_review/profitability_gap_assessment_2026_08_25.md)。

## 8. 易漂移事实与代码真源

| 事实 | 代码/配置真源 | 文档维护触发器 |
| --- | --- | --- |
| profile 身份、模拟/实盘、保证金/持仓模式 | `aats/bootstrap/managed_profiles.py` | managed definition 变化 |
| profile 端口模板 | `scripts/generate_managed_config_artifacts.py`、`configs/templates/` | generator/template 变化 |
| 配置优先级与 active parameter | `aats/bootstrap/config.py`、`aats/bootstrap/active_parameters.py` | loader/build runtime 变化 |
| 应用进程与容器 | `apps/`、`deploy/wsl2-dev/docker-compose*.yml` | service/overlay 变化 |
| 部署与停机 | `scripts/deploy.sh`、`scripts/ops/safe_shutdown.sh` | 阶段、profile、health gate 变化 |
| JetStream 数量、subject、容量、保留 | `aats/bus/nats_bus.py`、`deploy/wsl2-dev/nats/nats-server.conf` | `StreamSpec` 或 server budget 变化 |
| RDP 表和 schema | `aats/data_platform/rdp_models.py`、`aats/data_platform/migrations/_batch_b.py`、`scripts/apply_schema_migrations.py` | ORM/migration/ledger 变化 |
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
