# Task55 - Phase 5 Operator / Control Plane 切换

本阶段把 operator/control plane 的主读路径切到新的执行表与账本余额，并把 Phase 5 启用时的危险控制面行为收紧。

开关：

- `operator_control_plane_execution_ledger_enabled`

启用前提：

- `storage_mode=postgres`
- `portfolio_ledger_truth_enabled=True`
- `recovery_reconciliation_execution_ledger_enabled=True`

启用后行为：

- `orders/*` 主读路径优先使用 `execution_order_repo`
- `fills/*` 主读路径优先使用 `execution_fill_repo_v2`
- `balances` 主读路径优先使用 `ledger_accounts + ledger_entries`
- `portfolio/latest` 和 `positions` 继续读取 Phase 3 的 ledger-backed snapshot
- `system/runtime` 会暴露 `control_plane.phase5_enabled` 和各 truth source

控制面加固：

- 禁止 `operator_unsafe_write_without_auth=True`
- exchange-coupled runtime 必须启用 `operator_auth_enabled`
- 即使在本地 paper 模式下，Phase 5 开启后未鉴权写操作也会被拒绝

旧层降级后的角色：

- `execution_repo / portfolio_repo / fill_outcome_repo` 继续保留兼容与历史辅助用途
- 在 Phase 5 control plane 中，它们不再作为订单、成交、余额视图的权威来源
