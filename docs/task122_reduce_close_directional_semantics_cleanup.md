# Task 122 - reduce_* / close_* 方向语义清理

## Business objectives and boundaries
- 目标：修复 `reduce_* / close_*` 在兼容恢复链里的买卖方向错误，并让合约 audit / UI 保留 `减多 / 减空 / 平多 / 平空` 的方向性文案。
- 边界：不改撮合策略、不改公开 API、不重构执行模型；仅修复方向恢复和展示优先级。

## Module responsibilities and domain model
- `aats.schemas.execution` 负责 `position_intent -> side` 的标准语义。
- `execution_control` / `storage` 负责兼容从 `OrderState` 恢复 `OrderIntent` 时使用正确方向。
- `api/static/modules` 负责优先显示方向性合约仓位动作，而不是抽象 `reduce/exit`。

## Input/output interfaces
- 输入：`OrderState.position_intent`、`execution_action`、`position_intent`
- 输出：
  - `OrderIntent.side`
  - UI 文案：`减多 / 减空 / 平多 / 平空`

## Database schema / tables / indexes / constraints
- 无 schema 变更

## Transactions, Consistency, Concurrency
- 保持现有事务边界
- 修复后同一 `position_intent` 在 order service、shadow、converged repo 中的 `side` 必须一致

## Authorization, Authentication, Data Security
- 不涉及

## Error Handling and Idempotency
- 不更改命令流或幂等键
- 仅修复兼容恢复语义

## State Transition and Lifecycle
- 正确方向应为：
  - `reduce_long` / `close_long` -> `sell`
  - `reduce_short` / `close_short` -> `buy`

## Caching and Performance
- 仅增加轻量 helper，不引入额外缓存或可观性能开销

## Logging, Monitoring, Auditing
- 审计链和 UI 应保留方向细粒度表达，不再退化成通用 `减仓 / 退出`

## Testing Strategy
- 单测：
  - `ExecutionOrderService`
  - `Phase1ExecutionShadowService`
  - `ConvergedPostgresExecutionRepository`
- 集成测试：
  - `trade-display` 对 `reduce_* / close_*` 展示方向化文案

## Migration, Rollback, Compatibility
- 兼容现有存储字段
- 回滚只需恢复 helper 和 UI 优先级规则

## Configuration and Environment Isolation
- 无配置变更

## Code Organization and Dependencies
- 最小修改：
  - `aats/schemas/execution.py`
  - `aats/services/execution_control/order_service.py`
  - `aats/services/execution_control/shadow.py`
  - `aats/storage/execution_repo_converged_postgres.py`
  - `aats/api/static/modules/trade-display.js`
  - 对应测试文件

## Documentation and Operations Manual
- 交付后在任务文档中说明修复范围和验证结果

## Deployment and Acceptance Criteria
- 三条兼容恢复链方向恢复正确
- `trade-display` 对合约 `reduce_* / close_*` 显示方向化文案
- lint、单测、最窄 integration test 通过
