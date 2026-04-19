# Task 121 - reverse_to_* 审计 / 存储 / UI 语义清理

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## Business objectives and boundaries
- 目标：修复 `reverse_to_long` / `reverse_to_short` 在审计、存储回读、影子执行和前端展示中的语义丢失问题。
- 边界：不改撮合逻辑、不改仓位决策逻辑、不改公开 API，只修正兼容恢复和展示语义。

## Module responsibilities and domain model
- `decision_engine` 负责产出 `position_intent`
- `execution_control` / `storage` 负责在 `OrderState <-> OrderIntent` 兼容恢复时保留正确方向
- `api/static/modules` 负责向操作面展示“反手做多 / 反手做空”，不能把它压扁成通用“反手”

## Input/output interfaces
- 输入：`OrderState.position_intent` / `FillEvent.position_intent`
- 输出：
  - `OrderIntent.side`
  - UI 文案 `反手做多` / `反手做空`

## Database schema / tables / indexes / constraints
- 不改 schema
- 不改索引和约束

## Transactions, Consistency, Concurrency
- 保持现有存储路径
- 修复后 `reverse_to_short` 在各兼容恢复链上的 `side` 必须一致为 `sell`

## Authorization, Authentication, Data Security
- 不涉及鉴权
- 不引入新的敏感配置

## Error Handling and Idempotency
- 不改变提交、撤单或幂等键逻辑
- 仅修复重建语义和展示优先级

## State Transition and Lifecycle
- `reverse_to_*` 保持方向性：
  - `reverse_to_long` -> `buy`
  - `reverse_to_short` -> `sell`
- UI 展示优先保留方向性 `position_intent`

## Caching and Performance
- 无额外缓存
- 仅局部条件判断，不引入可观性能开销

## Logging, Monitoring, Auditing
- 审计链需要保留 `reverse_to_long` / `reverse_to_short`
- 不再被通用 `reverse` 覆盖

## Testing Strategy
- 单测：
  - `OrderState -> OrderIntent` 对 `reverse_to_short` 的 side 推导
  - shadow 服务对 `reverse_to_short` 的 side 推导
- 集成测试：
  - trade-display 对 `reverse_to_long` / `reverse_to_short` 展示为 `反手做多` / `反手做空`

## Migration, Rollback, Compatibility
- 兼容旧数据
- 不破坏旧 `execution_action=reverse`
- 回滚只需恢复相关条件判断和 UI 优先级

## Configuration and Environment Isolation
- 不改运行配置
- 不依赖新环境变量

## Code Organization and Dependencies
- 最小修改：
  - `aats/services/execution_control/order_service.py`
  - `aats/services/execution_control/shadow.py`
  - `aats/storage/execution_repo_converged_postgres.py`
  - `aats/api/static/modules/trade-display.js`
  - 对应测试文件

## Documentation and Operations Manual
- 交付后用任务文档记录修复范围和验证结果

## Deployment and Acceptance Criteria
- `reverse_to_short` 在三条兼容恢复链里都恢复成 `sell`
- UI 展示 `reverse_to_long` / `reverse_to_short` 时保留方向性
- lint、单测、最窄 integration test 通过
