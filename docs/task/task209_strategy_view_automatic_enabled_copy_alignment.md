# Task 209 - 策略页 `automatic_enabled` 展示语义收口

## Business objectives and boundaries

- 目标：把前端对 `automatic_enabled` 的解释统一成“当前是否允许自动进入执行链”。
- 边界：只改展示文案，不改后端字段、控制逻辑、数据库结构或 API 语义。

## Module responsibilities and domain model

- `strategy-view.js` 负责把 runtime summary 和 automation decision 摘要翻译成面向操作员的中文文案。
- `automatic_enabled` 在当前模型中表示“这条 sleeve 当前是否满足自动进入执行链的前置条件”，不再表示“配置层面是否打开自动模式”。

## Input/output interfaces

- 输入：现有 `strategyRuntime.summary`、`entry_execution_guard`、`configured_parameters`。
- 输出：策略页自动控制卡片中的标题、状态值、解释性文案。

## Database schema / tables / indexes / constraints

- 无改动。

## Transactions, Consistency, Concurrency

- 无事务或并发改动。

## Authorization, Authentication, Data Security

- 无权限或安全模型改动。

## Error Handling and Idempotency

- 无新增错误处理路径。
- 前端渲染对缺失字段仍保持原有 fallback。

## State Transition and Lifecycle

- 无状态机改动。
- 仅修正文案对现有执行控制状态的解释。

## Caching and Performance

- 无额外请求、缓存或性能开销。

## Logging, Monitoring, Auditing

- 无日志或审计改动。

## Testing Strategy

- 更新前端 integration smoke test，验证：
  - 新文案出现
  - 旧的“自动执行主开关”解释不再出现

## Migration, Rollback, Compatibility

- 向后兼容：仅 UI copy 变化。
- 回滚成本低，只需回退前端文案和测试。

## Configuration and Environment Isolation

- 无配置改动。

## Code Organization and Dependencies

- 仅改 `strategy-view.js` 和对应测试。

## Documentation and Operations Manual

- 本 SOW 即本轮变更说明。

## Deployment and Acceptance Criteria

- 策略页把 `automatic_enabled` 解读为“当前是否允许自动进入执行链”。
- 测试通过 lint、unit、最窄 integration。
