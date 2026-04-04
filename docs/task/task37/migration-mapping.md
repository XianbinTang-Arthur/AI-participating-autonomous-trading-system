# Task37 现有文件到新模块的一对一迁移表

## 1. 说明

本表用于指导后续重构中的模块迁移。它不是简单的重命名表，而是“职责迁移表”。

字段说明：

- `现有文件`：当前仓库正在承担职责的文件。
- `新模块 / 文件`：重构后建议承担该职责的位置。
- `迁移方式`：
  - `保留`
  - `保留并重写内部实现`
  - `拆分重写`
  - `逐步下线`

## 2. 迁移表

| 现有文件 | 新模块 / 文件 | 迁移方式 | 说明 |
|---|---|---|---|
| `aats/bootstrap/config.py` | `aats/bootstrap/config.py` | 保留并重写内部实现 | 继续作为 runtime 装配入口，但 wiring 逐步切到新 execution / ledger / projection。 |
| `aats/storage/base.py` | `aats/storage/base.py` | 保留并重写内部实现 | 扩展 Protocol，加入新 repo 接口。 |
| `aats/storage/sqlalchemy_models.py` | `aats/storage/sqlalchemy_models.py` | 保留并重写内部实现 | 新增 execution / ledger / inbox / outbox 表模型。 |
| `aats/services/execution_engine/order_manager.py` | `aats/services/execution_control/order_service.py` | 拆分重写 | 拆成 order create、state advance、terminal handling。 |
| `aats/services/execution_engine/state_machine.py` | `aats/services/execution_control/order_state_machine.py` | 保留并强化 | 迁移状态流转规则和严格校验。 |
| `aats/services/execution_engine/outbox.py` | `aats/services/execution_control/command_service.py` | 拆分重写 | 从当前事件 outbox 升级为命令 + 事件统一边界。 |
| `aats/services/execution_engine/obligations.py` | `aats/services/ledger/reservation_posting.py` | 逐步下线 | 保留金不再作为独立真相，而是账本 posting 的派生对象。 |
| `aats/services/execution_engine/recovery.py` | `aats/services/recovery_control/startup_recovery.py` | 拆分重写 | 启动恢复、resume gate、差异分类拆开。 |
| `aats/services/execution_engine/okx_adapter.py` | `aats/services/execution_engine/okx_adapter.py` + `aats/services/execution_control/fill_ingestion.py` | 保留并拆责 | venue translation 保留，状态推进和入账迁出。 |
| `aats/services/execution_engine/okx_rest.py` | `aats/services/execution_engine/okx_rest.py` | 保留 | 继续作为 exchange REST client。 |
| `aats/services/execution_engine/okx_private_websocket.py` | `aats/services/execution_engine/okx_private_websocket.py` | 保留 | 继续作为 venue private websocket client。 |
| `aats/services/accounting.py` | `aats/services/ledger/settlement_posting.py` | 拆分重写 | 把结算、费用、margin 相关 posting 迁出。 |
| `aats/services/portfolio_service/positions.py` | `aats/services/projections/balance_projection.py` + `aats/services/projections/position_projection.py` | 拆分重写 | 当前可变组合状态降级为 projection builder。 |
| `aats/services/portfolio_service/snapshots.py` | `aats/services/projections/equity_projection.py` | 保留思路，重做输入源 | snapshot 只保留为读模型。 |
| `aats/services/portfolio_service/reconstruction.py` | `aats/services/recovery_control/projection_rebuild.py` | 保留并改输入源 | 改为基于 ledger + execution 重建 projection。 |
| `aats/services/reconciliation_service/repair.py` | `aats/services/recovery_control/reconciliation_classifier.py` | 拆分重写 | 从 repair-oriented 改为 classify-oriented。 |
| `aats/services/reconciliation_service/comparator.py` | `aats/services/recovery_control/reconciliation_classifier.py` | 保留并并入 | 差异比对逻辑保留。 |
| `aats/services/governance_engine/recovery_posture.py` | `aats/services/recovery_control/resume_gate.py` | 保留并重写实现 | 上层接口可保留，底层依据改成 execution + ledger。 |
| `aats/services/governance_engine/health.py` | `aats/services/governance_engine/health.py` | 保留并重写数据源 | 继续作为系统健康聚合层。 |
| `aats/services/operator/query_service.py` | `aats/services/operator/query_service.py` | 保留并重写数据源 | 改查新 projections / execution views / ledger views。 |
| `aats/api/auth.py` | `aats/api/auth.py` | 保留并强化 | 强化生产鉴权、审批和 session revocation。 |
| `aats/api/auth_routes.py` | `aats/api/auth_routes.py` | 保留并强化 | 继续承载 operator control plane。 |
| `aats/api/session_auth.py` | `aats/api/session_auth.py` | 保留并强化 | 后续可升级为更强 session 管理。 |
| `aats/storage/execution_repo.py` | `aats/storage/execution_order_repo.py` | 逐步下线 | 兼容期双写，最终由新 repo 替代。 |
| `aats/storage/execution_repo_postgres.py` | `aats/storage/execution_order_repo_postgres.py` | 逐步下线 | 与上同。 |
| `aats/storage/obligation_repo.py` | `aats/storage/reservation_repo.py` | 逐步下线 | reservation 真相迁入新模型。 |
| `aats/storage/obligation_repo_postgres.py` | `aats/storage/reservation_repo_postgres.py` | 逐步下线 | 与上同。 |
| `aats/storage/portfolio_repo.py` | `aats/storage/projection_repo.py` | 逐步下线 | 只保留 projection 存储，不再承载真相。 |
| `aats/storage/portfolio_repo_postgres.py` | `aats/storage/projection_repo_postgres.py` | 逐步下线 | 与上同。 |
| `aats/storage/event_store.py` | `aats/storage/event_store.py` | 保留，职责降级 | 继续用于审计和回放辅助。 |
| `aats/storage/event_store_postgres.py` | `aats/storage/event_store_postgres.py` | 保留，职责降级 | 不再作为资金恢复真相源。 |
| `aats/storage/fill_outcome_repo.py` | `aats/storage/projection_repo.py` 或兼容层 | 逐步下线 | fill outcome 成为 projection 的一部分。 |
| `aats/storage/fill_outcome_repo_postgres.py` | `aats/storage/projection_repo_postgres.py` 或兼容层 | 逐步下线 | 与上同。 |

## 3. Phase 1 直接涉及的文件

第一阶段优先改这些文件：

- `aats/bootstrap/config.py`
- `aats/storage/base.py`
- `aats/storage/sqlalchemy_models.py`
- `aats/services/execution_engine/order_manager.py`
- `aats/storage/execution_repo_postgres.py`
- 新增：
  - `aats/storage/execution_order_repo.py`
  - `aats/storage/execution_order_repo_postgres.py`
  - `aats/storage/execution_command_repo.py`
  - `aats/storage/execution_command_repo_postgres.py`
  - `aats/storage/ledger_repo.py`
  - `aats/storage/ledger_repo_postgres.py`
  - `aats/storage/inbox_repo.py`
  - `aats/storage/inbox_repo_postgres.py`

## 4. Phase 1 后仍需保留的旧文件

这些文件在 Phase 1 后仍继续承担兼容职责：

- `aats/services/portfolio_service/positions.py`
- `aats/services/portfolio_service/snapshots.py`
- `aats/storage/portfolio_repo.py`
- `aats/storage/portfolio_repo_postgres.py`
- `aats/storage/execution_repo.py`
- `aats/storage/execution_repo_postgres.py`
- `aats/storage/obligation_repo.py`
- `aats/storage/obligation_repo_postgres.py`

原因：

- 新账本和新投影尚未切成主真相。
- 旧链路仍承担当前业务路径。
- 需要双写和比对周期来验证一致性。
