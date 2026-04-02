# Task 158 - execution_attempt_id 与 attempt-level diagnostics

## Business objectives and boundaries

- 为执行主链新增显式 `execution_attempt_id`，区分：
  - `execution_chain_id`: 同一条策略执行链
  - `execution_attempt_id`: 该执行链下的一次明确提交尝试
- 将该字段贯穿到：
  - `execution.py`
  - `order_manager.py`
  - fill / fill outcome
  - replay
  - operator diagnostics
- 保留现有 chain-aware EVR 作为主口径，不直接改成 attempt 聚合。
- 新增 attempt-level diagnostics，用于回答：
  - 同一条 chain 发生了多少次提交尝试
  - 有多少次尝试真的产生 fill
  - 是否存在 stray attempt / unmatched attempt

## Module responsibilities and domain model

- `execution.py`
  - 为 `OrderIntent / LegOrderIntent / ExecutionPlan / LegExecutionPlan / OrderState / FillEvent` 新增 `execution_attempt_id`
- `order_manager.py`
  - 在确定 `client_order_id` 后生成稳定 `execution_attempt_id`
  - 保证 order state / fill / hydrator 都能保留该字段
- execution adapters
  - paper / OKX 适配器透传 attempt id
- `portfolio.py`
  - `FillOutcomeRecord` 持有 attempt id
- `replay.py`
  - 校验 order state / fill / outcome 的 attempt 一致性
- `query_service.py`
  - 继续输出 chain-level EVR
  - 新增 attempt-level diagnostics summary

## Input/output interfaces

- Inputs
  - order intent
  - client order id
  - order state
  - fill event
  - fill outcome record
- Outputs
  - `execution_attempt_id`
  - `independent_expected_vs_realized_summary.attempt_diagnostics`

## Database schema / tables / indexes / constraints

- 本任务不新增 SQL 列或索引。
- 继续沿用现有 JSON payload/raw payload 持久化 attempt id。

## Transactions, Consistency, Concurrency

- 同一条实际提交尝试的：
  - `OrderState`
  - `FillEvent`
  - `FillOutcomeRecord`
  必须持有相同 `execution_attempt_id`
- 同一条 `execution_chain_id` 可以有 0..N 个 `execution_attempt_id`

## Authorization, Authentication, Data Security

- 本任务不修改权限或认证边界。
- `execution_attempt_id` 仅作内部归因标识，不承载敏感用户信息。

## Error Handling and Idempotency

- 若旧 payload 缺少 `execution_attempt_id`：
  - attempt diagnostics 回退到 `client_order_id`
  - replay 不直接失败，但若显式字段冲突则产出 issue
- submit command 幂等不改变，仍以现有 idempotency key 为准。

## State Transition and Lifecycle

- `execution_attempt_id` 在确定 `client_order_id` 的时刻生成。
- 同一条 attempt 下，允许经历：
  - `CREATED`
  - `SUBMITTING`
  - `SUBMITTED`
  - `PARTIALLY_FILLED`
  - `FILLED`
  - `CANCELED`
  - `FAILED`
  等状态更新。

## Caching and Performance

- 不新增数据库查询。
- attempt diagnostics 基于现有 fill outcome 遍历按 attempt key 聚合，复杂度与当前 EVR 同阶。

## Logging, Monitoring, Auditing

- replay 增加 attempt mismatch issue code。
- operator diagnostics 增加 attempt-level 摘要，但不扩大到实时告警系统。

## Testing Strategy

- unit
  - order manager 生成并透传 attempt id
  - adapters / fill outcome 保留 attempt id
  - replay 检测 attempt mismatch
  - operator summary 输出 attempt diagnostics
- integration
  - independent runtime/operator payload 继续兼容
  - replay / EVR / attempt diagnostics 一起回归

## Migration, Rollback, Compatibility

- 完全向后兼容：
  - 老数据无 attempt id 时仍可工作
  - attempt diagnostics 自动回退到 `client_order_id`
- 若回滚，只需停止消费 `execution_attempt_id` 字段。

## Configuration and Environment Isolation

- 本任务不新增配置项。
- 行为由 payload 是否带 `execution_attempt_id` 决定。

## Code Organization and Dependencies

- 仅修改现有 execution / operator / replay / portfolio 相关模块。
- 不引入新依赖。

## Documentation and Operations Manual

- 本文档作为 Task 158 的 SOW 与交付说明起点。
- 最终交付需说明：
  - attempt id 生成规则
  - chain-level EVR 与 attempt diagnostics 的职责边界
  - 旧数据 fallback 行为

## Deployment and Acceptance Criteria

- `execution_attempt_id` 进入：
  - `OrderIntent`
  - `OrderState`
  - `FillEvent`
  - `FillOutcomeRecord`
  - replay 校验
  - operator diagnostics
- chain-level EVR 保持不退化
- attempt diagnostics 能显式比较：
  - chain sample 数
  - attempt 数
  - multi-attempt chain 数
  - unmatched attempt 数
