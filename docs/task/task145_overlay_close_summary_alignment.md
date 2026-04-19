# Task 145 - Overlay Close Summary Alignment

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## Business objectives and boundaries

- 修复 `protective / opportunistic` 在 residual inventory 收口场景下的 operator/策略页摘要文案，让它们明确表达“收回保护腿 / 收回机会腿”。
- 补齐 `opportunistic` 的 coordinator/apply 级 residual-close 回归，避免只验证 `protective`。
- 不改 allocator、family 状态机和执行器主链，只修摘要链与测试覆盖。

## Module responsibilities and domain model

- `query_service.py`
  - 负责把 `selected_route_action + selected_family_action` 翻译为 operator runtime 摘要。
- `strategy-view.js`
  - 负责原样展示 runtime summary 的“调度结论”。
- `test_strategy_coordinator.py`
  - 负责验证 family cutover 后 applied target / decision outcome 的 closing 语义。
- `test_dashboard_ui.py`
  - 负责验证策略页会展示新的 closing 摘要文案。

## Input/output interfaces

- 输入：
  - `latest_snapshot.selected_route_action`
  - `latest_snapshot.selected_family_action`
- 输出：
  - `summary.operator_summary`
  - 策略页“调度结论”

## Database schema / tables / indexes / constraints

- 无数据库变更。

## Transactions, Consistency, Concurrency

- 仅内存摘要与前端渲染变更，无事务影响。

## Authorization, Authentication, Data Security

- 无新增权限或敏感数据处理。

## Error Handling and Idempotency

- route/family action 到中文摘要的映射保持纯函数化，重复计算结果稳定。

## State Transition and Lifecycle

- `override_target + close_protection_leg` => `收回保护腿`
- `override_target + close_opportunity_leg` => `收回机会腿`

## Caching and Performance

- 仅增加常量分支判断，无实质性能影响。

## Logging, Monitoring, Auditing

- operator/runtime/UI 对 closing 行为的解释与腿级语义保持一致，便于审计和复盘。

## Testing Strategy

- unit：
  - operator summary 文案分支
  - opportunistic residual-close coordinator/apply 语义
- integration：
  - 策略页展示 closing 摘要

## Migration, Rollback, Compatibility

- 保持旧 route_action 分支兼容，仅对新增 family close action 提供更细粒度文案。

## Configuration and Environment Isolation

- 无配置变更。

## Code Organization and Dependencies

- 最小修改 `query_service` 与相关测试，不做无关重构。

## Documentation and Operations Manual

- 本文档同时作为本次修复的 SOW 和交付说明。

## Deployment and Acceptance Criteria

- operator runtime summary 在 overlay residual close 场景下输出 closing 文案。
- opportunistic 拥有对等的 coordinator/apply residual-close 回归。
- 策略页能显示新的 closing 文案。
