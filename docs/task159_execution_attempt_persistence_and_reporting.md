# Task 159 - execution_attempt_id 显式持久化与 attempt 级外部报表

## 业务目标与边界

- 目标：
  - 把 `execution_attempt_id` 从仅靠 payload 留存，升级成执行主真相表里的显式持久化字段。
  - 让外部系统可以直接通过 operator 报表读取 attempt 级执行诊断。
  - 保持 `independent` 的 EVR 主口径仍然以 `execution_chain_id` 为准，不把 attempt 级诊断误当成主归因样本。
- 边界：
  - 不重写 EVR 主逻辑。
  - 不引入新的 telemetry 基础设施。
  - 不修改现有交易执行决策与下单行为。

## 模块职责与领域模型

- `execution_attempt_id`
  - 表示一次明确的 submit / retry 尝试身份。
  - 一个 `execution_chain_id` 可以对应多个 `execution_attempt_id`。
- `execution_chain_id`
  - 仍表示一本书/一条执行链的主样本身份。
  - 仍用于 EVR 主口径。
- `attempt diagnostics`
  - 用于补充观察 retry、stray attempt、多 attempt per chain。
  - 不替代 chain-level EVR。

## 输入/输出接口

- 输入：
  - `OrderIntent.execution_attempt_id`
  - `OrderState.execution_attempt_id`
  - `FillEvent.execution_attempt_id`
  - `FillOutcomeRecord.execution_attempt_id`
- 输出：
  - `execution_orders.execution_attempt_id`
  - `execution_fills.execution_attempt_id`
  - `fill_outcomes.execution_attempt_id`
  - `/reports/execution-quality.summary.attempt_metrics`
  - `/reports/execution-attempts`

## 数据库 Schema / 表 / 索引 / 约束

- 新增显式列：
  - `execution_orders.execution_attempt_id`
  - `execution_fills.execution_attempt_id`
  - `fill_outcomes.execution_attempt_id`
- 新增索引：
  - `ix_execution_orders_execution_attempt_id`
  - `ix_execution_fills_execution_attempt_id`
  - `ix_fill_outcomes_execution_attempt_id`
- 迁移策略：
  - 通过 `0003_postgres_execution_attempt_id_columns.sql` 增量加列
  - 从现有 `raw_payload/payload` 回填旧数据

## 事务、一致性、并发

- 继续沿现有 repo 的事务边界，不新增跨表事务模式。
- 写入 order/fill/fill_outcome 时同步写显式列与 payload，避免新旧读取面分叉。

## 鉴权、认证、数据安全

- 只修改内部持久化与 operator 查询，不新增越权读写接口。
- 新增报表沿现有 operator API 鉴权链。

## 错误处理与幂等

- 老 payload 缺失 `execution_attempt_id` 时继续用 helper 回退，不打坏兼容。
- attempt 级报表对缺失 attempt id 的历史行采用现有派生规则归一化。

## 状态流转与生命周期

- `execution_attempt_id` 生命周期：
  - intent/planner 生成或 submit 时派生
  - order state 固化
  - fill / fill outcome 继承
  - replay / operator / report 查询消费

## 缓存与性能

- 新报表优先复用现有 `execution_quality` 行数据，不额外引入重查询。
- 通过显式列与索引减少长期对 JSON payload 的依赖。

## 日志、监控、审计

- operator 外部报表新增 attempt 级 summary/rows，作为轻量 metrics sink。
- replay 仍保持 chain/attempt mismatch 校验。

## 测试策略

- repo / hydrate / payload 回归
- migration 版本与列存在性校验
- operator API 报表接口集成测试
- 保留最窄 replay/runtime 回归

## 迁移、回滚、兼容

- 新增 migration，不改旧 migration。
- 旧数据可通过回填 + helper fallback 兼容读取。
- 回滚时只需忽略新列，不影响旧 payload。

## 配置与环境隔离

- 无新增配置。
- PostgreSQL runtime 自动应用新 migration。

## 代码组织与依赖

- 最小变更集中在：
  - `aats/storage/*`
  - `aats/services/operator/*`
  - `aats/api/routes.py`
  - 相关 tests

## 文档与运维

- 需要在交付说明里明确：
  - EVR 主口径仍是 `execution_chain_id`
  - attempt 报表是补充执行诊断

## 部署与验收标准

- 新 migration 可在 legacy schema 上顺序执行。
- `execution_orders / execution_fills / fill_outcomes` 均存在 `execution_attempt_id` 显式列。
- `/reports/execution-quality` 能返回 `attempt_metrics`
- `/reports/execution-attempts` 能返回 attempt 级 rows 与 summary
