# AATS RDP 文档索引

> 文档状态：现行索引  
> 最后核对：2026-08-27（起始 HEAD `9c4112c6`，含当前 RDP 控制面收口候选；以本文档所在 HEAD 为准）
> 核对范围：当前入口、任务状态和静态模块边界；历史运行证据不证明现场状态

## 当前入口

- [`../../aats/data_platform/README.md`](../../aats/data_platform/README.md)：RDP 总览、模块边界和安全原则；
- [`module_reference.md`](module_reference.md)：当前代码模块、API 与数据层导航；
- [`../design/rdp_platform_v3_architecture_2026_08_25.md`](../design/rdp_platform_v3_architecture_2026_08_25.md)：当前统一工作台、单一读模型、队列语义和发布资格设计；
- [`../task/rdp_platform_v3_refactor_sow_2026_08_25.md`](../task/rdp_platform_v3_refactor_sow_2026_08_25.md)：V3 实施、测试、发布和模拟验收范围；
- [`../review/rdp_platform_v3_code_review_2026_08_25.md`](../review/rdp_platform_v3_code_review_2026_08_25.md)：V3 单一读模型、队列、发布资格、前端证据与安全动作复核；
- [`../review/rdp_full_business_logic_audit_2026_08_25.md`](../review/rdp_full_business_logic_audit_2026_08_25.md)：全链路业务逻辑审查发现、修复和验证证据；
- [`../task/rdp_2_run_observability_sow_2026_08_25.md`](../task/rdp_2_run_observability_sow_2026_08_25.md)：RDP 2.0 Run/Attempt/Step/Event 纵向切片、验收与兼容边界；
- [`../operations/platform_runbook.md`](../operations/platform_runbook.md)：daemon、队列、调度和恢复；
- [`../operations/rdp_operator_workflow.md`](../operations/rdp_operator_workflow.md)：Operator 标准流程；
- [`../operations/rdp_ui_quickstart.md`](../operations/rdp_ui_quickstart.md)：Workspace V3 页面结构与操作速查；
- [`../operations/rdp_workflow_calendar.md`](../operations/rdp_workflow_calendar.md)：当前 workflow 日历。
- [`../task/fs_004_research_selection_holdout_sow_2026_08_25.md`](../task/fs_004_research_selection_holdout_sow_2026_08_25.md)：Research Factory v2 的 train/valid 选择、test 内容封存与仍开放的最终 OOS 边界。
- [`../task/rdp_live_attribution_lineage_fix_sow_2026_08_26.md`](../task/rdp_live_attribution_lineage_fix_sow_2026_08_26.md)：完整 RDP 默认 live attribution、精确 lineage、schema migration 与 readiness fail-closed 边界。
- [`../operations/rdp_historical_data_recovery_runbook.md`](../operations/rdp_historical_data_recovery_runbook.md)：当前历史数据审计、raw archive、官方导入、归档、持续采集与 bundle 重建唯一操作入口；
- [`../task/rdp_historical_data_recovery_and_collection_hardening_sow_2026_08_26.md`](../task/rdp_historical_data_recovery_and_collection_hardening_sow_2026_08_26.md)：历史数据恢复与持续采集加固范围、验收门和未完成现场事项。
- [`../task/rdp_legacy_recommendation_forward_isolation_p0_sow_2026_08_27.md`](../task/rdp_legacy_recommendation_forward_isolation_p0_sow_2026_08_27.md)：旧 recommendation 精确 evidence-round 前向隔离；当前为 LF-A 实施/验收范围，不改写历史证据。
- [`../task/rdp_control_plane_truth_finalization_sow_2026_08_27.md`](../task/rdp_control_plane_truth_finalization_sow_2026_08_27.md)：控制面 canonical 真值、DB/mirror、post-apply provenance 与风险收敛 action proof；完成状态以任务书头和当前验证为准。

## 深入参考

- [`phase2_parameter_research_details.md`](phase2_parameter_research_details.md)：Phase 2 参数研究细节；
- [`phase3_4_attribution_execution_details.md`](phase3_4_attribution_execution_details.md)：Phase 3/4 归因与执行细节。

深入参考保留阶段语义，不自动升级为当前操作说明。workflow 集合、enabled 状态、timeout 和 enqueue block 以 `configs/rdp_workflows/*.json`、`rdp_task_db.py`、`workflow_scheduler.py` 和 `rdp_task_daemon.py` 为真源。
