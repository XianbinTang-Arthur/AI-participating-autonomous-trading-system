# Task 148 - Execution Discipline Summary Visibility

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## Business objectives and boundaries

- 把 opportunistic / independent 当前真正参与决策的 execution-discipline 结果提升成正式摘要。
- 让 runtime / operator / UI 不再只能看到边际数值，也能直接看到安全净边际、弱边际模式、是否仅报告、是否要求被动优先。
- 不改 allocator、risk 或 executor 的交易逻辑，只补结构化摘要与展示。

## Module responsibilities and domain model

- `strategy_runtime.py`
  - 为 `StrategyBookExpectancyEntry` 增加 execution-discipline 字段。
- `opportunistic_family.py`
  - 把 opportunistic 单腿边际与 discipline 结果写入 `book_expectancy_summary`。
- `independent_family.py`
  - 把每条 book 的 discipline 结果写入 `book_expectancy_summary`。
- `query_service.py`
  - 从结构化 summary 中提取目标边际与 discipline 指标。
- `strategy-view.js` / `detail-drawers.js` / `terms.js`
  - 优先展示结构化 discipline 摘要。

## Input/output interfaces

- 输入：
  - `book_expectancy_summary.books[]`
  - opportunistic / independent 的运行时 discipline 计算结果
- 输出：
  - `PositionTarget.book_expectancy_summary`
  - `DecisionOutcome.book_expectancy_summary`
  - operator/detail drawer/UI 的结构化 execution-discipline 展示

## Database schema / tables / indexes / constraints

- 无数据库变更。

## Transactions, Consistency, Concurrency

- 仅 payload / UI 摘要层变更，无事务与并发影响。

## Authorization, Authentication, Data Security

- 无新增权限或敏感数据处理。

## Error Handling and Idempotency

- 结构化 summary 缺字段时继续回退到原有边际数字展示。

## State Transition and Lifecycle

- 不改变交易状态机。
- 只增强 execution-discipline 的可观测性。

## Caching and Performance

- 仅增加少量 payload 字段与字符串拼装，对性能影响可忽略。

## Logging, Monitoring, Auditing

- operator / detail drawer / strategy view 能直接看到：
  - 安全净边际
  - 成本上限
  - 弱边际模式
  - 是否仅报告
  - 是否要求被动优先

## Testing Strategy

- unit：
  - opportunistic / independent summary 字段断言
  - operator target expectancy 提取断言
- integration：
  - dashboard UI 断言新 discipline 文案出现

## Migration, Rollback, Compatibility

- 仅新增可选字段，保持向后兼容。
- 旧消费者继续读取原有 `expected_*_bps` 不会被破坏。

## Configuration and Environment Isolation

- 无配置变更。

## Code Organization and Dependencies

- 只改 schema、family summary、operator payload 与展示层。
- 不做 unrelated refactor。

## Documentation and Operations Manual

- 本文档作为本轮 execution-discipline 摘要可观测性增强的 SOW 与交付说明。

## Deployment and Acceptance Criteria

- opportunistic / independent 的结构化边际摘要包含 execution-discipline 结果。
- operator / strategy view / decision drawer 可直接展示这组信息。
- 最窄 unit / integration 测试通过。
