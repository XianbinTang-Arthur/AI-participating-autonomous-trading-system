# Task 185 - Overlay Parent Exposure 独立持久化与 Transition 异常摘要

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## Business objectives and boundaries
- 把 `overlay parent exposure` 从“稳定嵌套 payload”升级为可单独持久化、可按 `decision_id` 直接检索的正式事件实体。
- 把 `transition_valid / transition_violation_reason` 从 runtime snapshot 内部字段升级为 operator/replay/UI 可直接消费的异常摘要。
- 不新增独立 SQL 表，不改交易执行主链，不破坏现有 `overlay_parent_exposure` / `book_runtime_states` 兼容读法。

## Current behavior summary
- 当前 `overlay_parent_exposure` 已经稳定写入 `PositionTarget / DecisionOutcome / family_execution_summary / hedge_overlay_decision`，但 operator/replay 仍主要从嵌套 payload 回填。
- 当前 `transition_valid / transition_violation_reason` 已经落在 `StrategyBookRuntimeState`，但 operator/replay/UI 还没有专门的异常摘要，只能从原始 snapshot 手工解释。

## Module responsibilities and domain model
- `aats/schemas/decision.py`
  - 新增 `OverlayParentExposureRecord`，作为独立持久化事件实体。
- `aats/services/strategy_engines/overlay_parent_exposure.py`
  - 提供 `OverlayParentExposureAudit -> OverlayParentExposureRecord` 构造入口。
- `aats/events/topics.py`
  - 新增独立 topic。
- `aats/services/decision_engine/orchestrator.py`
  - 在 `PositionTarget` 发布后补发 overlay parent exposure 独立事件。
- `aats/bootstrap/config.py`
  - 在 finalized `DecisionOutcome` 发布后补发 overlay parent exposure 独立事件。
- `aats/schemas/operator.py`
  - 新增 independent transition exception item/summary schema。
- `aats/services/operator/query_service.py`
  - operator 优先读取独立 overlay 事件，缺失时回退到旧 payload。
  - 新增 transition exception summary helper，并接入 detail/latest/recent/runtime payload。
- `aats/services/operator/audit_replay_queries.py`
  - replay validate / recent validations 接入 transition exception summary。
- `aats/api/static/modules/*`
  - 将 transition exception summary 抬升为专门诊断卡片/摘要，而不是埋在 snapshot 字段里。

## Input/output interfaces
- 输入
  - `PositionTarget.overlay_parent_exposure`
  - `DecisionOutcome.overlay_parent_exposure`
  - `StrategyBookRuntimeState.transition_valid`
  - `StrategyBookRuntimeState.transition_violation_reason`
- 输出
  - `strategy.overlay_parent_exposure` 独立事件
  - `position_target.overlay_parent_exposure`
  - `decision_outcome.overlay_parent_exposure`
  - `ai_decision_audit.independent_transition_exception_summary`
  - `ReplayValidationSummary.independent_transition_exception_summary`
  - `strategyRuntime.independent_transition_exception_summary`

## Database schema / tables / indexes / constraints
- 不新增 SQL 表。
- 复用现有 `event_store` 事件持久化链，把 overlay parent exposure 作为单独 topic 的事件实体落盘。
- 复用现有 operator/replay 持久化事件链，追加 transition exception summary 字段。

## Transactions, Consistency, Concurrency
- `PositionTarget` 与其 overlay exposure 事件在同一决策轮发布，`DecisionOutcome` 与其 overlay exposure 事件在 finalize 阶段发布。
- operator 读取时优先取最新独立事件，确保后续 replay/postmortem 解释口径稳定。
- 旧数据无独立事件时继续回退嵌套 payload，保证历史兼容。

## Authorization, Authentication, Data Security
- 不引入新的鉴权模型。
- 新对象只包含策略与持仓语义，不新增账户凭据或敏感密钥。

## Error Handling and Idempotency
- overlay 独立事件缺失时，operator/replay 回退旧 payload。
- transition summary 只在存在非法迁移或 violation reason 时生成，否则返回 `None`，不伪造异常。
- 事件持久化复用 event id 去重，不增加重复写入风险。

## State Transition and Lifecycle
- overlay 实体显式区分 `source_stage = position_target / decision_outcome`。
- transition 异常摘要显式聚合：
  - `invalid_transition_count`
  - `affected_legs`
  - `violation_reasons`
  - `items`

## Caching and Performance
- operator 读取独立 overlay 事件时按 `decision_id` 做本地 cache，避免重复扫描 `by_decision`。
- transition summary 基于已有 `book_runtime_states` 就地归纳，不增加数据库 round trip。

## Logging, Monitoring, Auditing
- replay / operator / UI 都能直接消费 overlay 独立事件和 transition 异常摘要。
- postmortem 不再需要手工从 runtime snapshot 逐字段解释 transition 违规。

## Testing Strategy
- unit
  - `tests/unit/test_operator_position_states.py`
  - `tests/unit/test_audit_replay_queries.py`
- integration
  - `tests/integration/test_operator_api.py`
  - `tests/integration/test_dashboard_ui.py`

## Migration, Rollback, Compatibility
- 无 migration。
- 回滚时移除新 topic 发布与 operator/replay/UI 汇总即可，旧 payload 读法仍可工作。

## Configuration and Environment Isolation
- 无新增配置项。

## Code Organization and Dependencies
- 复用现有 `overlay_parent_exposure.py` 作为领域转换入口。
- 复用现有 `event_store` / operator query / replay validate，不引入新的 repo/service 分层。

## Documentation and Operations Manual
- 本文档即本轮实现说明。

## Deployment and Acceptance Criteria
- lint 通过
- 相关 unit tests 通过
- 最窄 operator/replay integration tests 通过
- operator/detail/replay/UI 能看到独立 overlay 事件回读和 transition 异常摘要
