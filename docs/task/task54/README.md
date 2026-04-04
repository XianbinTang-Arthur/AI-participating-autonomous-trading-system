# Task54 - Phase 4 恢复与对账切换

本阶段把恢复与对账主链切到 `execution + ledger` 视角，但仍然保持显式开关启用：

- 配置开关：`recovery_reconciliation_execution_ledger_enabled`
- 依赖前提：
  - `storage_mode=postgres`
  - `portfolio_ledger_truth_enabled=True`

本阶段落地内容：

- 新增 `RecoveryReconciliationClassifier`
  - 对对账结果做恢复语义分类
  - 区分：
    - `clean`
    - `projection_rebuild_required`
    - `manual_review_required`
    - `investigate_state_divergence`
    - `halt_required`
- 新增 `ExecutionLedgerRecoveryService`
  - 在旧恢复服务之上叠加 Phase 4 规则
  - 使用 Phase 2 `execution_orders / execution_commands` 修正恢复视角
  - 当存在未完成执行命令时，启动直接进入安全阻断
- `ReconciliationService` 在 Phase 4 下会持久化带恢复分类的 `ReconciliationReport`
- `RecoveryPostureEvaluator` 会把 `status.resume_blocked_reasons` 一并纳入恢复门禁计算

当前边界：

- 这一步没有改成全新的对账存储模型，仍复用现有 `reconciliation_reports` 表
- Phase 4 重点是恢复门禁和对账分类，不是新的 lot-based 持仓引擎
- 自动修复仍然仅限“本地投影缺口”这类可判定情形
