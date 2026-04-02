# Task 142 - Protective Close Action And Independent UI Config Visibility

## Business objectives and boundaries

- 修复 `protective family` 在 residual inventory 收口阶段的 family-level action 语义，让 operator / audit / UI 不再把“关闭保护腿”误记为“调整保护腿”。
- 补齐 `independent` 新显式参数在 operator UI 配置卡中的展示，避免运行时已生效但前端摘要缺失。
- 不改 allocator、风控或执行器的真实撮合行为。

## Module responsibilities and domain model

- `protective_family.py`
  - 负责 protective overlay 的状态机与 family-level action 归类。
- `coordinator.py`
  - 负责将 selected family action 映射成 top-level `final_action`。
- `strategy-view.js`
  - 负责把 runtime/operator 配置摘要渲染成前端卡片。
- `terms.js`
  - 负责 family action 的中文文案。

## Input/output interfaces

- 输入：
  - `StrategyLegIntent.action`
  - `HedgeOverlayDecision`
  - runtime `configured_parameters.directional`
- 输出：
  - `StrategyFamilyAction.close_protection_leg`
  - coordinator `DecisionOutcome.final_action="exit"`
  - 前端配置卡新增 independent 参数行

## Database schema / tables / indexes / constraints

- 无数据库变更。

## Transactions, Consistency, Concurrency

- 仅策略内存对象和前端摘要变更，无事务变更。

## Authorization, Authentication, Data Security

- 无新增鉴权与数据权限面。

## Error Handling and Idempotency

- family action 新值保持纯函数映射；重复评估结果稳定。
- 前端缺失参数时仍显示 `待确认`，保持兼容。

## State Transition and Lifecycle

- `protective` 在 `hedge_leg.action == "close"` 时：
  - family action 从 `rebalance_protection` 修正为 `close_protection_leg`
  - top-level `final_action` 对齐为 `exit`

## Caching and Performance

- 仅增加少量静态摘要拼接，无性能敏感影响。

## Logging, Monitoring, Auditing

- operator/runtime payload 中的 family action 语义更准确，便于审计。
- 前端配置卡对 independent 新参数可直接核对 live profile。

## Testing Strategy

- unit:
  - protective close action 语义
  - coordinator top-level action 映射
- integration:
  - dashboard UI 渲染新的 independent 参数

## Migration, Rollback, Compatibility

- 对 `rebalance_protection` 保持兼容，仅在 close 路径新增 `close_protection_leg`。
- UI 新增字段展示，不移除旧字段。

## Configuration and Environment Isolation

- 不修改环境隔离策略。

## Code Organization and Dependencies

- 最小变更，局部修改 family/coordinator/UI/terms/tests。

## Documentation and Operations Manual

- 本文档作为本轮 SOW/交付说明。

## Deployment and Acceptance Criteria

- protective close 路径不再输出 `rebalance_protection`
- independent 新显式参数在 runtime/operator UI 配置卡可见
- lint、unit、最窄 integration 通过
