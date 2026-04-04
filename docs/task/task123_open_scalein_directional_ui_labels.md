# Task 123 - open_* / scale_in_* 合约方向文案补齐

## Business objectives and boundaries
- 目标：让合约 UI 在展示 `open_long/open_short`、`scale_in_long/scale_in_short` 时保留方向性文案。
- 边界：仅改 `trade-display` 展示优先级，不改撮合逻辑、不改存储、不改审计 schema。

## Module responsibilities and domain model
- `trade-display.js` 负责订单/成交表格和抽屉中的动作文案。
- `terms.js` 已经有 `开多 / 开空 / 加多 / 加空` 文案，本任务只修使用优先级。

## Input/output interfaces
- 输入：`execution_action`、`position_intent`
- 输出：UI 文案应优先显示：
  - `open_long -> 开多`
  - `open_short -> 开空`
  - `scale_in_long -> 加多`
  - `scale_in_short -> 加空`

## Database schema / tables / indexes / constraints
- 无 schema 变更

## Transactions, Consistency, Concurrency
- 不涉及事务或并发修改

## Authorization, Authentication, Data Security
- 不涉及

## Error Handling and Idempotency
- 不涉及下单幂等，仅前端展示

## State Transition and Lifecycle
- 仅调整展示层如何解释已有 `position_intent`

## Caching and Performance
- 常量级条件判断，无可观性能影响

## Logging, Monitoring, Auditing
- 改善 operator 页面与事后复盘可解释性

## Testing Strategy
- 添加最窄 integration test，验证 trade-display 对 `open_* / scale_in_*` 输出方向化文案

## Migration, Rollback, Compatibility
- 兼容现有数据
- 回滚只需恢复 `trade-display.js` 的优先级规则

## Configuration and Environment Isolation
- 无配置变更

## Code Organization and Dependencies
- 最小修改：
  - `aats/api/static/modules/trade-display.js`
  - `tests/integration/test_dashboard_ui.py`

## Documentation and Operations Manual
- 用本任务文档记录变更范围与验收

## Deployment and Acceptance Criteria
- UI 中：
  - `open_long/open_short` 显示为 `开多/开空`
  - `scale_in_long/scale_in_short` 显示为 `加多/加空`
- lint 与最窄 integration test 通过
