# Task 139 - Independent Book Expectancy Runtime Visibility

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## Goal

让 `independent` 的每条 book 级 expectancy 不再只藏在 candidate metrics 里，而是能在更多 runtime/operator 视图里直接读取和展示。

## Delivered

- 为 `StrategyCandidate` 增加结构化 `book_expectancy_summary`
- 为 `StrategyExecutionSummary` 增加结构化 `book_expectancy_summary`
- `independent family` candidate 现在会发布：
  - `source=independent_book`
  - `books=[long, short]`
  - 每条 book 的：
    - `expected_gross_edge_bps`
    - `expected_signal_edge_bps`
    - `expected_slippage_bps`
    - `expected_cost_bps`
    - `expected_net_edge_bps`
- coordinator 在 family cutover 时会把 selected candidate 的 book expectancy 复制到：
  - `PositionTarget.family_execution_summary`
  - `DecisionOutcome.family_execution_summary`
- operator / runtime / UI 现在可以直接显示：
  - 多书 毛/成本/净
  - 空书 毛/成本/净

## Scope

这次只做结构化暴露和展示，不改：

- allocator
- risk
- executor
- trade cost model

## Validation

- lint
- unit tests
- strategy runtime integration
- dashboard UI integration
