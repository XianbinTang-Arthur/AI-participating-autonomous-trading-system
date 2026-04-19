# Task58 最终收敛线

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../../docs/project_positioning.md)。


## 目标

本轮推进的是“最终金融级收敛”里最后三件最硬的事：

1. 去掉 `legacy execution_repo` 作为主真相写入路径
2. 把 lots 持久化成真实的 `position_lots / lot_events`
3. 补真实 PostgreSQL 下的双实例冲突与失败注入演练

## 当前实现

### 1. Execution 真相

- `financial_convergence_mode_enabled=true` 时，runtime 的 `execution_repo` 会切到 [execution_repo_converged_postgres.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/storage/execution_repo_converged_postgres.py)
- 主真相表：
  - `execution_orders`
  - `execution_order_state_history`
  - `execution_fills`
- `legacy order_states / fill_events` 不再承载主写路径
- [outbox.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/execution_engine/outbox.py) 的事务边界已经跟随切到新 execution truth

### 2. Lots 真相

- 当前 PostgreSQL 基线 migration：[0001_postgres_latest_schema.sql](D:/文件/project/AIParticipatingAutonomousTradingSystem/migrations/0001_postgres_latest_schema.sql)
- 新增持久化 repo：
  - [lot_repo.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/storage/lot_repo.py)
  - [lot_repo_postgres.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/storage/lot_repo_postgres.py)
- 新增持久化服务：
  - [persistent_lot_book.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/ledger/persistent_lot_book.py)
- [ledger_portfolio.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/projections/ledger_portfolio.py) 现在会在账本驱动的组合快照旁，同步重建并落库 `position_lots / lot_events`
- [lot_projection.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/ledger/lot_projection.py) 现在同时负责：
  - 组合投影
  - FIFO lot book 重建
  - lot events 生成

### 3. PostgreSQL 演练

- 新增真实 PostgreSQL 集成测试：
  - [test_task58_financial_convergence_postgres.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/tests/integration/test_task58_financial_convergence_postgres.py)
- 覆盖场景：
  - advisory lock 双实例冲突
  - subscriber failure 注入下，新 execution truth 已提交、outbox 仍 pending
  - migrations 下 `position_lots / lot_events` 可工作

## 最新全链路流程

### 下单与执行

1. `ORDER_INTENTS`
2. `OrderManager`
3. `ExecutionOrderService` 入队 `execution_commands`
4. `ExecutionCommandProcessor` 执行 submit/cancel
5. `PostgresExecutionOutboxPublisher`
6. `ConvergedPostgresExecutionRepository`
7. `execution_orders / execution_fills`
8. `event_store + outbox`
9. bus publish `ORDER_UPDATES / FILL_EVENTS`

### 资金与 lot

1. `FILL_EVENTS`
2. `LedgerBackedPortfolioService`
3. `LedgerSettlementPostingService`
4. `ledger_journals / ledger_entries`
5. `PersistentLotBookService`
6. `position_lots / lot_events`
7. `portfolio_snapshots / fill_outcomes`

### 恢复与控制面

1. `ExecutionLedgerRecoveryService`
2. `RecoveryReconciliationClassifier`
3. `operator control plane`
4. `execution_order_repo / execution_fill_repo_v2 / ledger_* / position_lots`

## 当前边界

这轮之后，系统已经正式进入“execution + ledger + lots”主真相形态，但仍有两点要明确：

- `legacy execution_repo` 仍然作为兼容代码存在，未从仓库删除；只是 `financial_convergence_mode_enabled=true` 时不再作为主写真相
- 真实 PostgreSQL 演练测试已经落库，但是否真的跑过，取决于环境里是否提供 `AATS_DATABASE_URL`

## 验证

- 本地可运行回归：
  - `tests/unit/test_task58_converged_execution_truth.py`
  - `tests/unit/test_task58_persistent_lot_book.py`
  - `tests/unit/test_task57_lot_projection_and_convergence.py`
  - `tests/unit/test_task52_execution_command_flow.py`
  - `tests/integration/test_phase4_recovery_reconciliation_runtime.py`
  - `tests/integration/test_recovery.py`
- 真实 PostgreSQL 测试：
  - `tests/integration/test_task58_financial_convergence_postgres.py`
  - 若缺少 `AATS_DATABASE_URL`，会被显式 skip
