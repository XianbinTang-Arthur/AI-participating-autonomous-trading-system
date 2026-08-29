# AATS Operations 文档索引

> 文档状态：现行索引
> 最后核对：2026-08-29（起始 HEAD `d34b01c38f31` + 持续采集周期保活工作区；以本文档所在 HEAD 为准）
> 核对范围：目录状态、当前静态入口与安全边界；不证明现场服务或数据状态

本目录同时保存当前操作手册、专题参考和历史运行记录。文件名包含 `runbook`、`checklist` 或 `release`，不代表它仍可用于当前实盘。执行任何状态变更前，先按本页状态选择入口，并按 [`../README.md`](../README.md) 的真实性与时效性规则复核当前 `HEAD`。

## 1. 现行操作说明

下列文档已在所列基线与当前代码对照。操作前仍必须读取实时健康、权限和安全门；超过 30 天或相关代码变化后自动视为待复核。

| 文档 | 范围 | 关键边界 |
| --- | --- | --- |
| [`operator_checklist.md`](operator_checklist.md) | Operator 日常检查 | 不替代实时 trading-ready/reconciliation 证据 |
| [`platform_runbook.md`](platform_runbook.md) | RDP daemon、队列、调度与恢复 | RDP 操作不能替代主交易恢复 |
| [`rdp_operator_workflow.md`](rdp_operator_workflow.md) | RDP Operator 标准流程 | 禁止使用已硬禁用的旧 CLI |
| [`parameter_governance.md`](parameter_governance.md) | 参数治理状态与证据 | active parameter 真源是 Postgres |
| [`parameter_apply_and_rollback.md`](parameter_apply_and_rollback.md) | release/apply/rollback、安全门和审计 | direct apply 已停用；Operator rollback 用 token，内部风险收敛用精确 DB 证明 |
| [`production_parameter_change_runbook.md`](production_parameter_change_runbook.md) | 生产参数变更全流程 | 当前 live 部署禁用；只可准备/审阅证据，不得执行 runtime 发布 |
| [`rdp_scheduling_strategy.md`](rdp_scheduling_strategy.md) | scheduler 语义 | 以 workflow JSON、allowlist 和 daemon timeout 为真源 |
| [`rdp_workflow_calendar.md`](rdp_workflow_calendar.md) | 当前 workflow 日历 | decision/release disabled；release 禁止入队 |
| [`workflow_failure_recovery.md`](workflow_failure_recovery.md) | task queue 失败恢复 | 旧 `/rdp/operations/failures*` 路由不存在 |
| [`rdp_reliability_runbook.md`](rdp_reliability_runbook.md) | RDP 可靠性检查 | 明确 checker 覆盖不足与 legacy artifact 边界 |
| [`rdp_staging_rehearsal_checklist.md`](rdp_staging_rehearsal_checklist.md) | staging 演练 | 不等于实盘放行或 runtime 验证 |
| [`rdp_ui_quickstart.md`](rdp_ui_quickstart.md) | RDP UI 使用入口 | local HTTP 与 live TLS 必须区分 |
| [`wsl2_sync_workflow.md`](wsl2_sync_workflow.md) | Windows/WSL2 Git 同步 | 只同步 committed state；部署仍走 `scripts/deploy.sh` |
| [`wsl2_startup_prewarm.md`](wsl2_startup_prewarm.md) | 登录后及周期性 WSL2/采集栈健康检查 | repair 复用标准部署包装器；协调停止不自动撤销，不形成第二套部署方式 |
| [`profit_readiness_runbook.md`](profit_readiness_runbook.md) | 收益证据、v2 研究、L2、holdout、参数代次、故障矩阵与 readiness | 格式 v1 只能证明模拟就绪，不能放行 live |
| [`rdp_historical_data_recovery_runbook.md`](rdp_historical_data_recovery_runbook.md) | 历史覆盖审计、官方导入、raw/archive、retention、连续性与历史 bundle 重建 | 只允许 research/governance 与 derivatives 模拟栈；不授权 live、真实订单或参数 apply |

部署、TLS、profile、端口和安全停机的最高层操作入口是根目录 [`../../DEPLOYMENT.md`](../../DEPLOYMENT.md)。

## 2. 现行专题参考

这些文档解释某一子域，但不应单独作为生产状态变更步骤。使用前需按右侧真源复核。

| 文档 | 主题 | 复核真源 |
| --- | --- | --- |
| [`artifact_conventions.md`](artifact_conventions.md) | RDP artifact 命名与目录 | artifact writer/reader 与数据库 registry |
| [`event_store_archive_runbook.md`](event_store_archive_runbook.md) | event store 归档 | 当前 schema、archive script、备份/恢复验证 |
| [`improvement_backlog_process.md`](improvement_backlog_process.md) | 改进 backlog 治理 | 当前治理流程与负责人决策 |
| [`live_schema_contract_for_rdp.md`](live_schema_contract_for_rdp.md) | RDP/live schema 边界 | ORM、migration、只读连接权限 |
| [`operator_rdp_integration.md`](operator_rdp_integration.md) | 最初 8 个 RDP 只读端点 | 当前为 57 个 method/path operation、56 个唯一 URL path；完整集合读 OpenAPI |
| [`parameter_mapping_reference.md`](parameter_mapping_reference.md) | 参数映射 | `active_parameters.py` 与 settings 字段 |
| [`periodic_review_workflow.md`](periodic_review_workflow.md) | 周期复核框架 | 当前 workflow/scheduler 与治理策略 |
| [`rdp_environment_matrix.md`](rdp_environment_matrix.md) | 环境能力矩阵 | environment guard、managed profile、DB 配置 |
| [`rdp_metrics_framework.md`](rdp_metrics_framework.md) | RDP 指标口径 | metrics 生产代码、dashboard/query |
| [`reliability_alerting.md`](reliability_alerting.md) | 可靠性告警设计 | checker、当前 alert artifact、Grafana provisioning |
| [`release_effectiveness_evaluation.md`](release_effectiveness_evaluation.md) | release 效果评估 | observation/release 状态机与当前数据 |
| [`round_lifecycle.md`](round_lifecycle.md) | round 生命周期 | 当前模型、状态机和 API |

## 3. 历史证据

下列文件保留当时设计、演练或观察结果，不承担当前操作职责。旧命令只用于解释历史，不得直接执行。

| 文档 | 历史语义 | 当前替代入口 |
| --- | --- | --- |
| [`grafana_alerts.md`](grafana_alerts.md) | 早期告警建议与路径快照 | 当前 `deploy/wsl2-dev/grafana/provisioning/alerting/` 与标准部署 |
| [`multiprocess_refactor_roadmap.md`](multiprocess_refactor_roadmap.md) | 多进程改造 roadmap | [`../../ARCHITECTURE.md`](../../ARCHITECTURE.md)、[`../../DEPLOYMENT.md`](../../DEPLOYMENT.md) |
| [`p1d_phase1a_predeploy_checklist.md`](p1d_phase1a_predeploy_checklist.md) | P1-D 阶段预部署检查 | 当前部署/Operator/RDP runbook |
| [`p1d_phase1a_48h_stability_runbook.md`](p1d_phase1a_48h_stability_runbook.md) | 一次性 48 小时观察 | 当前指标、告警和 Operator 检查 |
| [`release_notes_p2-8_signal_edge_alignment.md`](release_notes_p2-8_signal_edge_alignment.md) | P2-8 发布记录 | 当前代码和项目全景说明 |
| [`route_a_observation_window_daily_check.md`](route_a_observation_window_daily_check.md) | Route A 固定观察窗口 | 当前 RDP 日历与可靠性 runbook |
| [`safe_shutdown_design_2026_04_20.md`](safe_shutdown_design_2026_04_20.md) | 安全停机实现设计 | [`../../DEPLOYMENT.md`](../../DEPLOYMENT.md) 与 `scripts/ops/safe_shutdown.sh` |
| [`stage7_wsl2_realrun_runbook.md`](stage7_wsl2_realrun_runbook.md) | Stage 7 实跑记录 | [`../../DEPLOYMENT.md`](../../DEPLOYMENT.md) |

## 4. 维护要求

1. 新增 operations 文档时必须在本页登记状态、适用范围和真源。
2. 现行操作文档必须带最后核对日期和 Git 基线；没有这些元数据不得作为实盘依据。
3. 历史文档可以修复链接和增加历史声明，但不能把后来的实现倒写为当时事实。
4. 同一操作只保留一个最高层入口；专题文档链接入口，不复制整套命令。
5. 静态文档不能声称当前服务健康、账户一致、资金安全或已经部署成功。
