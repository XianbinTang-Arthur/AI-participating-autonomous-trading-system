# Task37 真资管无人值守交易系统重构需求包

## 1. 文档定位

本目录是面向当前仓库的系统级重构需求包，用于把现有项目从：

- 单进程、事件驱动的交易 runtime
- 以 `fills + portfolio snapshots + reconciliation` 为核心的状态恢复模式
- 依赖投影状态近似表达资金真相的执行系统

重构为：

- 账本驱动的资金系统
- 可恢复、可审计、可长期无人值守的真钱交易控制系统
- 在不确定状态下默认停机而不是继续交易的生产级运行内核

本需求包覆盖：

- 五阶段施工顺序
- 第一阶段最小可落地的数据库表结构
- 新 repo 接口定义
- 现有文件到新模块的一对一迁移表

本需求包不覆盖：

- 具体 Python 实现
- 生产部署拓扑
- 迁移脚本落库
- 详细工时估算
- 人员拆分与排期管理

这些内容应在后续任务中继续展开。

## 2. 目标

Task37 的目标不是直接完成重构，而是形成一套足以指导后续工程实施的正式文档，明确：

1. 什么必须先做，什么必须后做。
2. 哪些模块保留，哪些模块降级，哪些模块重写。
3. 第一阶段需要引入哪些表和哪些存储接口。
4. 现有执行、资金、恢复、对账、控制面如何按阶段迁移。

## 3. 设计原则

后续实现必须默认遵守以下原则：

1. 资金真相只能来自账本分录，不能来自可变内存态或快照。
2. 订单推进必须基于持久化状态机，不能依赖一次函数调用走完整链路。
3. 所有外部副作用必须有明确的幂等边界，包括 submit、cancel、poll、WebSocket 事件消费。
4. 恢复必须是确定性重放和状态分类，不能依赖隐式修补。
5. 对账的职责是发现差异、分类差异、驱动动作，不再承担资金真相职责。
6. 所有投影都必须允许重建，因此投影永远不是资金真相。
7. 无法确认状态时，系统必须进入 `halt`、`review_required` 或 `resume_blocked` 之一。

## 4. 文档索引

- [五阶段施工顺序总览](./phase-plan.md)
- [第一阶段表结构 SQL](./phase1_schema.sql)
- [新 Repo 接口定义](./repo-interfaces.md)
- [现有文件到新模块迁移表](./migration-mapping.md)

## 5. 当前仓库中的保留区与重构区

适合作为重构后上层业务层继续保留的区域：

- `aats/services/decision_engine`
- `aats/services/ai_service`
- `aats/services/feature_engine`
- `aats/services/market_gateway`
- `aats/services/governance_engine`
- `aats/services/operator`
- `aats/api`

不适合作为最终真钱内核、需要重构或逐步下线的区域：

- `aats/services/execution_engine/order_manager.py`
- `aats/services/execution_engine/obligations.py`
- `aats/services/execution_engine/recovery.py`
- `aats/services/portfolio_service/positions.py`
- `aats/services/portfolio_service/snapshots.py`
- `aats/storage/execution_repo*.py`
- `aats/storage/portfolio_repo*.py`
- `aats/storage/obligation_repo*.py`

## 6. 交付边界

Task37 的交付应视为“实施前设计包”。后续任务应以此为约束，逐步补齐：

- 分阶段迁移脚本
- 新 repo 的具体实现
- wiring 切换计划
- 兼容期双写与比对计划
- 验收测试与回滚策略
