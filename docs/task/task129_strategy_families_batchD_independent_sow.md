# Task 129：Strategy Families 重构 Batch D SOW

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## Business objectives and boundaries

- 只迁移 `independent family` 的业务评估归属
- 补齐 `expected net edge gating`、`hysteresis`、`execution gating`
- 让 snapshot / audit 首次出现真实 `family="independent"` 候选
- 不提前切 allocator / apply / execution 主路径

## Module responsibilities and domain model

- `target_position.py`
  - 保留薄封装和现有 patch 点
  - 不再持有独立双书的核心业务实现
- `families/independent_family.py`
  - 成为 independent book decision 的真实业务承载模块
  - 输出 candidate、legs、overlay decision
- `coordinator.py`
  - 只负责注册与调度 family engine
  - 不改变 allocatable family 集

## Input/output interfaces

- 输入：
  - `DecisionContext`
  - `BaselineAssessment`
  - `AIMarketAssessment | None`
  - directional target 的 `expected_signal_edge_bps / expected_cost_bps / expected_net_edge_bps`
- 输出：
  - `StrategyCandidate(family="independent")`
  - `HedgeOverlayDecision(effective_mode="independent")`
  - `StrategyLegIntent[]`

## Database schema / tables / indexes / constraints

- 本批次不新增表，不改索引
- 仅改变 snapshot / audit 中 candidate 的 family identity

## Transactions, Consistency, Concurrency

- 不改事务边界
- 不改 command flow
- 不改 recovery ownership

## Authorization, Authentication, Data Security

- 不改鉴权
- 不新增敏感配置源

## Error Handling and Idempotency

- family 评估失败时仍回退旧 directional 主线
- 不允许因新 family evaluation 影响既有 apply 幂等路径

## State Transition and Lifecycle

- independent 只新增真实 candidate 生命周期：
  - `inactive`
  - `opening`
  - `holding/active`
  - `closing`
  - `blocked`
- 不改变 selected family / applied target 的主路径 ownership

## Caching and Performance

- 只做进程内计算迁移
- 不新增远程 IO

## Logging, Monitoring, Auditing

- snapshot / audit 需显示真实 `family="independent"`
- metrics 需暴露 expectancy gating / hysteresis / execution gating 结果

## Testing Strategy

- unit
  - independent target behavior 不回归
  - expectancy gating 生效
  - hysteresis close threshold 生效
  - execution cost cap 生效
- coordinator unit
  - 出现真实 independent candidate
- runtime integration
  - `/strategy/runtime` 出现真实 independent candidate

## Migration, Rollback, Compatibility

- 仅迁评估归属
- allocator / apply / execution 主路径保持 directional
- rollback 方式：
  - 关闭 `strategy_family_independent_enabled`
  - 保留 legacy independent path

## Configuration and Environment Isolation

- 新增 independent family 所需配置：
  - `strategy_hedge_independent_min_safe_net_edge_bps`
  - `strategy_hedge_independent_expected_slippage_buffer_bps`
  - `strategy_hedge_independent_expected_execution_buffer_bps`
  - `strategy_hedge_independent_long_close_threshold`
  - `strategy_hedge_independent_short_close_threshold`
  - `strategy_hedge_independent_weak_edge_execution_mode`
  - `strategy_hedge_independent_max_acceptable_cost_bps`
  - `strategy_hedge_independent_passive_first_enabled`

## Code Organization and Dependencies

- family helper 只依赖现有 schema / settings / rollout helper
- 不改 allocator 依赖图

## Documentation and Operations Manual

- Batch D 交付后补 delivery 文档
- runtime 配置面需要能看到新配置项

## Deployment and Acceptance Criteria

- `independent` candidate 在 snapshot / audit 中不再是 skeleton
- 现有 independent target tests 不回归
- allocator 仍不选择 independent 作为主 family
- lint / unit / 最窄 integration 全通过
