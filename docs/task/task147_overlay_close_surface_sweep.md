# Task 147 - Overlay Close Surface Sweep

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## Business objectives and boundaries

- 对 `protective / opportunistic` residual close 的剩余展示面做最后一轮排查。
- 修复仍把 `route_action=override_target` 直接渲染成通用文案的最后一处 UI：`recent sleeve intents` 表格。
- 不改执行逻辑，不改 route_action 结构字段，只修展示层与测试。

## Module responsibilities and domain model

- `strategy-view.js`
  - 负责策略页中 `recent sleeve intents` 的 route 文案渲染。
- `test_dashboard_ui.py`
  - 负责验证 residual close 在策略页多个展示位都显示 closing 语义。

## Input/output interfaces

- 输入：
  - `recent_sleeve_intents[].route_action`
  - `recent_sleeve_intents[].family_action`
- 输出：
  - `recent sleeve intents` 表格中的 route 文案

## Database schema / tables / indexes / constraints

- 无数据库变更。

## Transactions, Consistency, Concurrency

- 仅 UI 展示逻辑变更，无事务与并发影响。

## Authorization, Authentication, Data Security

- 无新增权限或敏感数据处理。

## Error Handling and Idempotency

- 继续复用统一的 `strategyRouteActionLabel(...)`，避免多处 route/family 文案漂移。

## State Transition and Lifecycle

- `override_target + close_protection_leg` -> `收回保护腿`
- `override_target + close_opportunity_leg` -> `收回机会腿`

## Caching and Performance

- 无可感知性能影响。

## Logging, Monitoring, Auditing

- 策略页多个摘要位对 residual close 的解释保持一致，便于 live 值班和复盘。

## Testing Strategy

- integration：
  - dashboard UI 断言 residual close 在：
    - 调度结论
    - 当前路由 / 候选处理
    - recent sleeve intents
    - allocator 结论
    都呈现 closing 语义

## Migration, Rollback, Compatibility

- 非 residual-close 场景保持原有 route 标签不变。

## Configuration and Environment Isolation

- 无配置变更。

## Code Organization and Dependencies

- 最小修改 `strategy-view.js` 和现有 dashboard 集成测试。

## Documentation and Operations Manual

- 本文档作为本轮 final surface sweep 的 SOW 与交付说明。

## Deployment and Acceptance Criteria

- `recent sleeve intents` 不再把 residual close 渲染成通用 `override_target`。
- dashboard 最窄集成测试通过。
