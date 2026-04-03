# Task 142 - Exit Execution Truth Pending 可见性与恢复契约收口
## 业务目标与边界
- 修复 `truth_pending` 的父退出任务在 startup / recovery / operator 视图里可能不可见的问题。
- 修复 `refresh_exit_execution_intents()` 对无 child refs 的非终态 parent 静默跳过的问题。
- 收口 `_execute_serial_exit_split()` 的返回契约，避免类型标注与运行时返回值不一致。
- 在不破坏现有下游兼容性的前提下，显式标注 `max_drawdown_bps` 的旧兼容语义，避免继续误导新调用方。

## 模块职责与领域模型
- `aats/services/execution_engine/exit_intent_aggregator.py`
  - 统一 parent-exit 的 structural review item 生成。
  - 将 `truth_pending` 和 `missing_child_refs_for_parent` 纳入显式可见的 parent blocker。
- `aats/services/recovery_control/startup_recovery.py`
  - 启动恢复 overlay 不再只把 parent item 当作人工 review，而是区分 `blocks_resume` 与 `review_required`。
- `aats/services/governance_engine/recovery_posture.py`
  - 将新的 parent-exit blocker 视为持久 resume blocker。
- `aats/services/execution_engine/order_manager.py`
  - 收口 serial exit split 的返回契约，确保 submit 主路径始终返回 `OrderState`。
- `aats/services/strategy_engines/independent/*`
  - 保留 `max_drawdown_bps` 兼容字段，但显式补充其旧语义来源，推动下游优先使用新字段。

## 输入 / 输出接口
- 不新增外部 API。
- `exit_execution_review_items()` 会新增两类 item：
  - `exit_execution_truth_pending`
  - `exit_execution_missing_child_refs_for_parent`
- startup recovery / reconciliation / operator 读侧会透出上述新 kind。

## 数据库 / 表 / 事务 / 一致性
- 本任务不新增表结构。
- 仅复用已有 `ExitExecutionIntent.metadata.resume_issue`、`ReconciliationStateSnapshot.details_json`、`RecoveryStatus.resume_blocked_reasons`。

## 错误处理与幂等
- child refs 无法重建时，不再静默跳过，而是写入幂等的 structural resume issue。
- 如果后续 child refs 被重建，`missing_child_refs_for_parent` issue 会在 refresh 中自动清除。

## 状态迁移与生命周期
- `truth_pending` parent：
  - 不一定升级为 `operator_review_required`
  - 但必须以 structural item 形式可见，并阻断 resume
- `childless parent`：
  - 视为 structural issue
  - 阻断 resume
  - 在 child refs 恢复后自动消失

## 日志、监控、审计
- 沿用现有 startup snapshot / recovery view / operator review item 透传链路。
- 不新增独立审计表。

## 测试策略
- 单测：
  - `tests/unit/test_exit_execution_aggregator.py`
  - `tests/unit/test_startup_recovery.py`
  - `tests/unit/test_reconciliation_repair.py`
  - `tests/unit/test_order_manager_exit_execution.py`
  - `tests/unit/test_independent_scoring.py`
  - `tests/unit/test_independent_family.py`
- 窄集成：
  - `tests/integration/test_recovery.py`

## 迁移、回滚、兼容
- 不移除旧字段或旧路由。
- `max_drawdown_bps` 保持兼容输出，但新增显式语义说明字段，便于下游逐步迁移。

## 配置与环境隔离
- 本任务不新增 `.env.*` 配置项。

## 部署与验收标准
- startup / recovery / operator 视图能看见 `truth_pending` parent。
- childless parent 不再被 refresh 静默吞掉。
- serial exit split 的 submit 主路径不再存在 `OrderState` / `None` 契约不一致。
- lint、相关单测、最窄集成测试通过。
