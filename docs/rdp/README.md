# AATS RDP 文档索引

> 文档状态：现行索引  
> 最后核对：2026-08-25（起始 HEAD `00b6df0f8a8d2665d6cae3e88996843767cd1f56`；Phase 3A–3W 整改提交候选）

## 当前入口

- [`../../aats/data_platform/README.md`](../../aats/data_platform/README.md)：RDP 总览、模块边界和安全原则；
- [`module_reference.md`](module_reference.md)：当前代码模块、API 与数据层导航；
- [`../task/rdp_2_run_observability_sow_2026_08_25.md`](../task/rdp_2_run_observability_sow_2026_08_25.md)：RDP 2.0 Run/Attempt/Step/Event 纵向切片、验收与兼容边界；
- [`../operations/platform_runbook.md`](../operations/platform_runbook.md)：daemon、队列、调度和恢复；
- [`../operations/rdp_operator_workflow.md`](../operations/rdp_operator_workflow.md)：Operator 标准流程；
- [`../operations/rdp_workflow_calendar.md`](../operations/rdp_workflow_calendar.md)：当前 workflow 日历。
- [`../task/fs_004_research_selection_holdout_sow_2026_08_25.md`](../task/fs_004_research_selection_holdout_sow_2026_08_25.md)：Research Factory v2 的 train/valid 选择、test 内容封存与仍开放的最终 OOS 边界。

## 深入参考

- [`phase2_parameter_research_details.md`](phase2_parameter_research_details.md)：Phase 2 参数研究细节；
- [`phase3_4_attribution_execution_details.md`](phase3_4_attribution_execution_details.md)：Phase 3/4 归因与执行细节。

深入参考保留阶段语义，不自动升级为当前操作说明。workflow 集合、enabled 状态、timeout 和 enqueue block 以 `configs/rdp_workflows/*.json`、`rdp_task_db.py`、`workflow_scheduler.py` 和 `rdp_task_daemon.py` 为真源。
