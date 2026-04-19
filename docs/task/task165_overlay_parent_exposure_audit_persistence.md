# Task 165 - Overlay Parent Exposure 审计对象持久化

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## Business objectives and boundaries
- 把 `overlay_parent_exposure.py` 的关键信息升级成正式的审计/持久化对象，而不是只在运行时推导。
- 保持现有扁平字段兼容，避免打断当前 runtime/operator/UI。
- 不新增独立数据库表；先复用 `PositionTarget / DecisionOutcome / family_execution_summary / hedge_overlay_decision` 的现有持久化链。

## Current behavior summary
- 当前仓库已有 `OverlayParentExposureLifecycle` 运行时对象。
- 关键信息已经以扁平字段进入：
  - `family_execution_summary`
  - `hedge_overlay_decision`
  - `PositionTarget`
  - `DecisionOutcome`
- 但仍缺一个稳定的嵌套对象，导致 replay / 审计读取时主要依赖扁平字段回填。

## Module responsibilities and domain model
- `aats/schemas/decision.py`
  - 新增 `OverlayParentExposureAudit`
  - 接入 `StrategyExecutionSummary / HedgeOverlayDecision / PositionTarget / DecisionOutcome`
- `aats/services/strategy_engines/overlay_parent_exposure.py`
  - 提供 `OverlayParentExposureLifecycle -> OverlayParentExposureAudit` 的统一转换
- `aats/services/strategy_engines/families/protective_family.py`
  - 候选 metrics 带上正式的 `overlay_parent_exposure`
- `aats/services/strategy_engines/families/opportunistic_family.py`
  - 同上
- `aats/services/strategy_engines/coordinator.py`
  - 把 overlay audit object 正式回写到 summary / decision / target
- `aats/services/operator/query_service.py`
  - 对新旧 payload 统一 backfill nested object

## Input/output interfaces
- 输入：
  - `OverlayParentExposureLifecycle`
  - overlay family candidate metrics
- 输出：
  - `family_execution_summary.overlay_parent_exposure`
  - `hedge_overlay_decision.overlay_parent_exposure`
  - `PositionTarget.overlay_parent_exposure`
  - `DecisionOutcome.overlay_parent_exposure`
  - operator payload 顶层 `overlay_parent_exposure`

## Database schema / tables / indexes / constraints
- 本轮不改数据库 schema。
- 持久化路径复用现有的事件/仓储对象序列化。

## Transactions, Consistency, Concurrency
- 由 coordinator 单次 apply 统一回写 audit object，避免 summary / target / outcome 之间的信息漂移。
- query service 只做兼容 backfill，不修改原始已落盘事件。

## Authorization, Authentication, Data Security
- 不改鉴权与权限模型。
- 该对象只包含策略状态与数量信息，不引入新的敏感凭据。

## Error Handling and Idempotency
- 新字段缺失时保持 `None`，继续兼容旧 payload。
- operator payload 优先读取嵌套对象，缺失时回退到旧扁平字段。

## State Transition and Lifecycle
- 审计对象显式记录：
  - `parent_family`
  - `symbol / target_leverage / margin_mode`
  - `target_long_qty / target_short_qty`
  - `current_long_qty / current_short_qty`
  - `target_qty / current_qty / effective_qty`
  - `target_signal / current_signal / effective_signal`
  - `signal_source / source_of_truth / lifecycle_state`
  - `target_active / inventory_active`
  - `source`

## Caching and Performance
- 不新增查询。
- 仅在 query payload 归一化阶段增加轻量 backfill。

## Logging, Monitoring, Auditing
- operator/detail/latest/recent 现在可以直接读取稳定的 `overlay_parent_exposure` 对象。
- replay 与 postmortem 不必只依赖扁平字段重新拼接。

## Testing Strategy
- unit:
  - `tests/unit/test_strategy_coordinator.py`
  - `tests/unit/test_operator_position_states.py`
- integration:
  - `tests/integration/test_operator_api.py`

## Migration, Rollback, Compatibility
- 无 migration。
- 如需回滚，只需移除 schema 字段与 coordinator/query backfill，不影响旧数据读取。

## Configuration and Environment Isolation
- 不新增配置项。

## Code Organization and Dependencies
- 复用现有 `overlay_parent_exposure.py` 作为领域转换入口。
- 不引入新的持久化服务或数据库对象。

## Documentation and Operations Manual
- 本文件即本轮交付说明。

## Deployment and Acceptance Criteria
- lint 通过
- 相关 unit tests 通过
- 最窄 operator integration test 通过
- 新事件与旧 payload 都能在 runtime/operator 读取到正式的 `overlay_parent_exposure`
