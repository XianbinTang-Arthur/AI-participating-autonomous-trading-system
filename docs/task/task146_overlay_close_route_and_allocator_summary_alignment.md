# Task 146 - Overlay Close Route And Allocator Summary Alignment

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## Business objectives and boundaries

- 修复 `protective / opportunistic` 在 residual-inventory close 场景下，策略页仍显示通用 `override_target` 路由文案的问题。
- 修复 allocator 在同场景下仍输出通用“生成账户级执行目标”文案的问题。
- 不改 coordinator、allocator 的执行决策规则，只修摘要解释层和测试覆盖。

## Module responsibilities and domain model

- `allocator.py`
  - 负责生成组合级 `PortfolioAllocationDecision.operator_summary`。
- `strategy-view.js`
  - 负责将 `route_action / family_action / operator_summary` 渲染到策略页。
- `test_strategy_coordinator.py`
  - 负责验证 residual close 场景下 allocator summary 与 applied target 语义一致。
- `test_dashboard_ui.py`
  - 负责验证策略页的路由、调度结论、allocator 结论都显示 closing 语义。

## Input/output interfaces

- 输入：
  - `StrategySleeveIntent.family_action`
  - `summary.latest_selected_route_action`
  - `summary.latest_selected_family_action`
  - `latest_allocation_decision.operator_summary`
- 输出：
  - `PortfolioAllocationDecision.operator_summary`
  - 策略页 `当前路由`
  - 候选策略表 `如何处理`
  - 策略页 `Allocator 结论`

## Database schema / tables / indexes / constraints

- 无数据库变更。

## Transactions, Consistency, Concurrency

- 仅摘要文案与前端渲染变更，无事务和并发影响。

## Authorization, Authentication, Data Security

- 无新增认证或敏感数据处理。

## Error Handling and Idempotency

- 文案映射保持纯函数化，同一输入重复渲染结果稳定。

## State Transition and Lifecycle

- `override_target + close_protection_leg`
  - runtime route label -> `收回保护腿`
  - allocator summary -> `已批准收回保护腿的账户级执行目标`
- `override_target + close_opportunity_leg`
  - runtime route label -> `收回机会腿`
  - allocator summary -> `已批准收回机会腿的账户级执行目标`

## Caching and Performance

- 仅增加少量字符串分支，无实质性能影响。

## Logging, Monitoring, Auditing

- operator/runtime/UI 对 residual close 的解释与腿级语义保持一致，便于 live 复盘。

## Testing Strategy

- unit：
  - residual close 下 allocator summary 文案
- integration：
  - 策略页显示 close route label 和 allocator closing 文案

## Migration, Rollback, Compatibility

- 对非 residual-close 场景保持原有 route/allocator 文案不变。

## Configuration and Environment Isolation

- 无配置变更。

## Code Organization and Dependencies

- 最小变更 `allocator.py`、`strategy-view.js` 和相关测试，不做无关重构。

## Documentation and Operations Manual

- 本文档作为本次修复的 SOW 与交付说明。

## Deployment and Acceptance Criteria

- residual close 场景下，策略页不再只显示通用 `override_target`。
- allocator summary 在 residual close 场景下显示 family-specific closing 文案。
- lint、相关单测与最窄 dashboard 集成测试通过。
