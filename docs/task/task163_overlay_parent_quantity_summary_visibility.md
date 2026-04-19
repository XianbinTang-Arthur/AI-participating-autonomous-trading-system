# Task 163 - Overlay Parent Quantity Summary Visibility

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## Business objectives and boundaries
- 把 `parent_source_of_truth / parent_target_qty / parent_current_qty / parent_effective_qty` 正式抬进 runtime/operator 的结构化摘要链。
- 不改 overlay 执行逻辑，只补 schema、coordinator 回写、operator payload/backfill 和 UI 摘要可见性。
- 保持旧 payload 兼容，允许通过 backfill 从 `family_execution_summary` 或 `hedge_overlay_decision` 补字段。

## Current behavior summary
- 这 4 个字段当前只停留在 `protective / opportunistic` candidate metrics。
- coordinator / `PositionTarget` / `DecisionOutcome` / operator payload 还没有正式承接。
- UI 摘要 helper 只能展示父腿信号和活跃状态，看不到判定口径与 signed qty。

## Module responsibilities and domain model
- `aats/schemas/decision.py`
  - 把 4 个字段加入 `StrategyExecutionSummary`、`HedgeOverlayDecision`、`PositionTarget`、`DecisionOutcome`
- `aats/services/strategy_engines/coordinator.py`
  - 把 candidate metrics 中的 4 个字段抬进
    - `family_execution_summary`
    - `hedge_overlay_decision`
    - 最终 `PositionTarget / DecisionOutcome`
- `aats/services/operator/query_service.py`
  - 对新旧 payload 统一 backfill
  - 保证 `/decision/latest`、`/decision/{id}`、`/decision/recent` 顶层直接带字段
- `aats/api/static/modules/terms.js`
  - 在父腿摘要 helper 中展示
    - 判定口径
    - 目标仓位
    - 当前仓位
    - 生效仓位

## Input/output interfaces
- 输入：
  - overlay family candidate metrics
  - `family_execution_summary`
  - `hedge_overlay_decision`
- 输出：
  - `PositionTarget.parent_source_of_truth`
  - `PositionTarget.parent_target_qty`
  - `PositionTarget.parent_current_qty`
  - `PositionTarget.parent_effective_qty`
  - `DecisionOutcome` / `StrategyExecutionSummary` / `HedgeOverlayDecision` 同名字段
  - operator API 顶层 payload 同名字段

## Database schema / tables / indexes / constraints
- 本轮不改数据库 schema。

## Transactions, Consistency, Concurrency
- coordinator 在单次 apply 里统一回写，避免字段只停留在 candidate metrics 而控制面丢失。
- query service 对旧 payload 做只读 backfill，不改变已持久化原始事件。

## Authorization, Authentication, Data Security
- 本轮不改鉴权或安全模型。

## Error handling and compatibility
- 新字段缺失时保持 `None`。
- operator payload 优先读顶层字段，缺失时回退 `hedge_overlay_decision` 和 `family_execution_summary`。

## State transition and lifecycle
- 新字段用来解释 parent exposure 的数量口径，不改变 overlay state machine。
- 重点覆盖三类阶段：
  - residual inventory
  - target-only
  - target + inventory

## Caching and performance
- 只是轻量字段透传与字符串摘要，不引入新查询。

## Logging, monitoring, auditing
- operator/detail drawer 现在能直接看到 parent exposure 的：
  - source of truth
  - target/current/effective signed qty

## Testing strategy
- unit
  - `tests/unit/test_operator_position_states.py`
- integration
  - `tests/integration/test_operator_api.py`
  - `tests/integration/test_dashboard_ui.py`

## Migration, rollback, compatibility
- 无 schema migration。
- 如需回滚，只需删除新增字段映射和 helper 展示，不影响历史事件兼容。

## Documentation and operations
- 本文件即本轮交付说明。

## Acceptance criteria
- lint 通过
- 相关 unit tests 通过
- 最窄 integration tests 通过
- runtime/operator/UI 都能显示这 4 个字段
