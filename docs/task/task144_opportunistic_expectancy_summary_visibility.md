# Task 144 - Opportunistic Expectancy Summary Visibility

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## Business objectives and boundaries

- 把 `opportunistic` 新增的 execution-cost discipline 从 `candidate.metrics` 提升成正式结构化摘要。
- 让 coordinator / runtime / operator / UI 优先消费这组摘要，而不是继续展示 directional-style `expected_*_bps`。
- 不改 allocator、risk 或 executor 的真实交易逻辑。

## Module responsibilities and domain model

- `opportunistic_family.py`
  - 继续负责机会腿评估与执行纪律计算。
  - 新增单腿版 `book_expectancy_summary`，来源标记为 `opportunistic_overlay`。
- `coordinator.py`
  - 继续沿用现有 `selected_candidate.book_expectancy_summary -> family_execution_summary -> position_target / decision_outcome` 复制链。
- `query_service.py`
  - 在单腿摘要存在时，优先使用结构化 `book_expectancy_summary` 生成 `target_expected_*_bps`。
- `strategy-view.js`
  - 候选摘要、已应用目标摘要、overlay meta 优先显示 opportunistic 结构化边际。

## Input/output interfaces

- 输入：
  - `OpportunisticExecutionDiscipline`
  - `HedgeOverlayDecision`
  - `PositionTarget.book_expectancy_summary`
- 输出：
  - `StrategyCandidate.book_expectancy_summary`
  - `family_execution_summary.book_expectancy_summary`
  - `PositionTarget.book_expectancy_summary`
  - `DecisionOutcome.book_expectancy_summary`
  - operator/detail/UI 的单腿边际展示

## Database schema / tables / indexes / constraints

- 无数据库 schema 变更。

## Transactions, Consistency, Concurrency

- 无事务逻辑变更。
- 语义一致性要求：
  - opportunistic 单腿 execution discipline 必须与 runtime/operator/UI 显示一致。

## Authorization, Authentication, Data Security

- 无认证授权变更。

## Error Handling and Idempotency

- 当没有结构化 `book_expectancy_summary` 时，仍回退到旧的 `position_target.expected_*_bps`，保持兼容。

## State Transition and Lifecycle

- 无状态机迁移。
- 只增强 `opening / holding / closing / blocked` 等现有 opportunistic 状态的解释面。

## Caching and Performance

- 仅增加轻量级摘要组装，无额外 IO。

## Logging, Monitoring, Auditing

- operator/detail drawer 现在能看到 opportunistic 单腿边际摘要，便于复盘 weak-edge / cost gate。

## Testing Strategy

- unit
  - opportunistic candidate 生成结构化 summary
  - operator economic summary 优先读取单腿 summary
  - coordinator cutover 贯通 summary
- integration
  - dashboard strategy view 显示 opportunistic 边际摘要

## Migration, Rollback, Compatibility

- 保持兼容：
  - 旧 `metrics.expected_*_bps` 仍保留
  - 没有 summary 时仍使用旧字段

## Configuration and Environment Isolation

- 无新增环境变量。

## Code Organization and Dependencies

- 复用现有 `StrategyBookExpectancySummary` 结构，不新增平行 schema。

## Documentation and Operations Manual

- 本文档记录 opportunistic 结构化边际摘要的贯通范围与兼容策略。

## Deployment and Acceptance Criteria

- opportunistic candidate 带 `book_expectancy_summary`
- applied target / decision outcome 能带出该 summary
- operator/detail 能优先显示 opportunistic 单腿边际
- strategy view 能显示 opportunistic 的 safe net / max cost / 单腿边际摘要
