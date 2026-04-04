# Task38 Phase 1 Scaffold 落地说明

## 1. 目标

Task38 的目标是把 Task37 中定义的 Phase 1 设计落到代码里，但仍然保持“骨架落地、主链路未切换”的边界。

本次落地包括：

- `migrations/0001_postgres_latest_schema.sql`
- Phase 1 新表对应的 SQLAlchemy model
- 新 storage repo protocol 与 Postgres skeleton
- 后续 Phase 2 到 Phase 4 目标 package 的目录骨架

本次不包括：

- 主业务链路切到新 repo
- execution state machine 切主
- ledger 成为唯一资金真相
- recovery / reconciliation 重写

## 2. 当前状态

当前仓库现在具备：

- Phase 1 新 schema 的落库入口
- 对应 repo 的类型边界与 Postgres persistence skeleton
- 后续分阶段实施所需的 package 目录骨架

当前仓库仍然没有完成：

- 双写接入
- 读路径切换
- operator / recovery / reconciliation 切换
- 老旧 repo 的兼容期治理

## 3. 后续直接接续点

完成 Task38 后，下一步应优先推进：

1. 在 `aats/bootstrap/config.py` 中装配新 repo 实例。
2. 在 `aats/services/execution_engine/order_manager.py` 中接入 execution order / fill 的 shadow write。
3. 在 fill 持久化路径上生成 ledger journal mirror write。
4. 为新 schema 和新 repo 增加最小 PostgreSQL 集成测试。
