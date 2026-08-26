# AATS Task 与 SOW 历史索引

> 文档状态：历史交付目录索引  
> 最后核对：2026-08-26（目录与替代入口；RDP 数据治理任务状态已回填）

本目录保存任务书、SOW、阶段设计、实施记录和交付报告。它是工程可追溯性材料，不是当前系统说明；“完成”“通过”“上线”等措辞只对文件记录的基线和验证范围成立。

## 使用规则

- 查当前行为：使用 [`../code_review/README.md`](../code_review/README.md) 与代码真源；
- 查当前操作：使用 [`../operations/README.md`](../operations/README.md) 与 [`../../DEPLOYMENT.md`](../../DEPLOYMENT.md)；
- 查当前测试：使用 [`../testing/README.md`](../testing/README.md)；
- 引用历史任务时保留文件名、日期、commit 和验证范围；
- 新任务材料继续写入本目录，不得写入 `docs/` 根层兼容区；
- 同一任务有子目录时，以其 `README.md` 作为该任务入口。

## 2026-08-26 RDP 数据治理实施记录

- [`rdp_historical_data_recovery_and_collection_hardening_sow_2026_08_26.md`](rdp_historical_data_recovery_and_collection_hardening_sow_2026_08_26.md)：历史数据恢复、持续采集、不可变归档、双准入与完整模拟 RDP 的实施台账。工程实现、标准 derivatives 部署和 NO-GO 运行验证已回填；官方 1 日样本、随机归档恢复、故障注入、跨日采集与签名 UI 仍明确未完成。

现行操作入口是 [`../operations/rdp_historical_data_recovery_runbook.md`](../operations/rdp_historical_data_recovery_runbook.md)，当前收益/上线门结论是 [`../testing/profit_readiness_acceptance.md`](../testing/profit_readiness_acceptance.md)。任务书不替代下一次运行前的新覆盖审计和健康检查。

## 2026-08-24 全系统审计整改 SOW

以下文件记录 2026-08-24 至 2026-08-25 整改工作的实施边界与验证契约；其“完成”状态只对应各文件记录的当时基线，不是当前生产已生效声明：

- [`phase3_post_audit_full_change_review_sow_2026_08_25.md`](phase3_post_audit_full_change_review_sow_2026_08_25.md)：Phase 3 全量变更复审、问题修复、完整提交与 derivatives 模拟栈运行观测；
- [`fs_001_profile_apply_fail_closed_sow_2026_08_24.md`](fs_001_profile_apply_fail_closed_sow_2026_08_24.md)：profile 参数应用错误成功失败关闭；
- [`fs_001_profile_rollback_fail_closed_sow_2026_08_24.md`](fs_001_profile_rollback_fail_closed_sow_2026_08_24.md)：profile 参数回滚错误成功失败关闭；
- [`fs_002_kill_switch_p0_remediation_sow_2026_08_24.md`](fs_002_kill_switch_p0_remediation_sow_2026_08_24.md)：Kill Switch P0；
- [`fs_002_short_lived_trading_permission_lease_sow_2026_08_24.md`](fs_002_short_lived_trading_permission_lease_sow_2026_08_24.md)：Kill Switch 全分区短时交易许可租约；
- [`fs_003_backtest_causal_timing_sow_2026_08_24.md`](fs_003_backtest_causal_timing_sow_2026_08_24.md)：回测因果时序；
- [`fs_014_ohlcv_fill_realism_containment_sow_2026_08_24.md`](fs_014_ohlcv_fill_realism_containment_sow_2026_08_24.md)：OHLCV 成交量参与上限、partial fill 与成本证据边界；
- [`fs_005_gateway_loopback_containment_sow_2026_08_24.md`](fs_005_gateway_loopback_containment_sow_2026_08_24.md)：Gateway 本机绑定与本地入口收口；
- [`fs_006_critical_task_supervision_sow_2026_08_24.md`](fs_006_critical_task_supervision_sow_2026_08_24.md)：关键任务监督；
- [`fs_006_critical_task_progress_watchdog_sow_2026_08_24.md`](fs_006_critical_task_progress_watchdog_sow_2026_08_24.md)：固定周期关键任务成功进度看门狗；
- [`fs_007_deployment_fail_closed_sow_2026_08_24.md`](fs_007_deployment_fail_closed_sow_2026_08_24.md)：部署失败关闭与 live 禁用；
- [`fs_009_schema_single_truth_sow_2026_08_24.md`](fs_009_schema_single_truth_sow_2026_08_24.md)：schema 单一迁移真源；
- [`fs_010_managed_profile_unknown_key_fail_closed_sow_2026_08_25.md`](fs_010_managed_profile_unknown_key_fail_closed_sow_2026_08_25.md)：managed 伪配置删除、unknown-key 失败关闭与生成文档防回退；
- [`fs_011_legacy_run_local_fail_closed_sow_2026_08_25.md`](fs_011_legacy_run_local_fail_closed_sow_2026_08_25.md)：失效本地 paper-loop 入口的无配置副作用迁移失败关闭；
- [`fs_015_replay_short_bias_parity_sow_2026_08_25.md`](fs_015_replay_short_bias_parity_sow_2026_08_25.md)：independent replay 与生产 short-bias gate 一致性；
- [`fs_016_nats_peer_readiness_fail_closed_sow_2026_08_24.md`](fs_016_nats_peer_readiness_fail_closed_sow_2026_08_24.md)：NATS/hybrid peer readiness 失败关闭与部署代次隔离；
- [`fs_019_operator_login_async_isolation_sow_2026_08_24.md`](fs_019_operator_login_async_isolation_sow_2026_08_24.md)：Operator 登录异步隔离、有界 worker、每进程限流与 dummy KDF；
- [`fs_020_browser_security_headers_sow_2026_08_24.md`](fs_020_browser_security_headers_sow_2026_08_24.md)：Gateway Host 失败关闭与浏览器安全响应头；
- [`fs_017_fs_018_dashboard_accessibility_sow_2026_08_25.md`](fs_017_fs_018_dashboard_accessibility_sow_2026_08_25.md)：Dashboard 原生 modal/focus contract 与 reduced-motion 收敛；
- [`fs_021_ci_quality_gate_sow_2026_08_25.md`](fs_021_ci_quality_gate_sow_2026_08_25.md)：仓库级最小权限 CI、strict marker 与 warning 预算；远端 required check 和 integration/security gate 仍开放。
- [`fs_022_reproducible_dependencies_sow_2026_08_25.md`](fs_022_reproducible_dependencies_sow_2026_08_25.md)：Python 3.12/Linux hashed lock、基础/外部镜像 digest 与防回退契约；APT、SBOM/扫描、clean build 和远端治理仍开放。
- [`fs_008_database_connection_budget_sow_2026_08_25.md`](fs_008_database_connection_budget_sow_2026_08_25.md)：PostgreSQL 角色化连接池、声明拓扑预算和 engine inventory；目标负载、瞬时路径与联合内存预算仍开放。
- [`fs_004_research_selection_holdout_sow_2026_08_25.md`](fs_004_research_selection_holdout_sow_2026_08_25.md)：Research Factory train/valid 双门与 test 内容封存；最终 OOS、walk-forward、历史产物审计和独立复核仍开放。

现行结论、残余风险和运行时未知项以 [`../../audit/full_system_2026_08_24/README.md`](../../audit/full_system_2026_08_24/README.md) 为入口；这些 SOW 只记录获准范围、设计和静态/隔离验证。

截至 2026-08-23，本目录含数百份历史材料，另有 184 份早期 SOW/任务文件保留在 `docs/` 根层以维持路径兼容。批量迁移必须先做引用和外部审计影响评估。

## Legacy Word 文档

下列 `.docx` 是 2026-03 的初始蓝图，保留原文件名和路径以维持历史追溯；它们不是当前说明：

- [`Ai Autonomous Trading System Reference Implementation Skeleton.docx`](<Ai Autonomous Trading System Reference Implementation Skeleton.docx>)：早期 reference skeleton，包含已经漂移的目录、Kafka/Redpanda 候选、旧事件名、旧服务边界和 `run_local.py` 假设；
- [`Ai Autonomous Trading System Whitepaper V1.docx`](<Ai Autonomous Trading System Whitepaper V1.docx>)：概念白皮书 v1，包含 Kubernetes、Kafka/Redpanda/NATS 候选和早期状态机，不代表当前 WSL2 Compose、NATS、Postgres、Redis 或交易实现。

两份文件只可用于了解项目起点。当前替代入口是 [`../../ARCHITECTURE.md`](../../ARCHITECTURE.md)、[`../code_review/README.md`](../code_review/README.md) 和 [`../../DEPLOYMENT.md`](../../DEPLOYMENT.md)。本轮只读结构检查确认其历史性质；由于文档 OOXML 缺少明确页尺寸且当前环境没有 LibreOffice，未完成视觉渲染检查，因此不对原件版式作“已优化”声明。
