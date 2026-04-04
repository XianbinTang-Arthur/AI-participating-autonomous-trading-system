# Task 166 - Overlay Parent Exposure Replay/Postmortem 专门摘要

## Business objectives and boundaries
- 把 `overlay_parent_exposure` 从“仅随 `PositionTarget / DecisionOutcome` 顶层 payload 暴露”，升级为 replay/postmortem 可直接消费的专门摘要。
- 不改交易执行主链，不新增外部消费者假设；只覆盖仓库内 operator / replay / audit 能力。
- 保持现有顶层 `overlay_parent_exposure` 与扁平 `parent_*` 字段兼容。

## Current behavior summary
- 当前系统已经把稳定的 `overlay_parent_exposure` 写进：
  - `family_execution_summary`
  - `hedge_overlay_decision`
  - `PositionTarget`
  - `DecisionOutcome`
- `ai_decision_audit` 与 replay validation 之前没有各自的专门摘要字段，postmortem 仍主要依赖通用 payload。

## Module responsibilities and domain model
- `aats/services/operator/query_service.py`
  - 新增 `overlay_parent_exposure_summary` helper
  - 接入 `ai_decision_audit`
  - 接入 overlay audit 摘要
- `aats/services/operator/audit_replay_queries.py`
  - replay validate/recent-validations 产出 `overlay_parent_exposure_summary`
- `aats/schemas/operator.py`
  - `ReplayValidationSummary` 正式声明该字段

## Input/output interfaces
- 输入：
  - `DecisionOutcome.overlay_parent_exposure`
  - `PositionTarget.overlay_parent_exposure`
  - 兼容旧 payload 的扁平 `parent_*`
- 输出：
  - `ai_decision_audit.overlay_parent_exposure_summary`
  - `ReplayValidationSummary.overlay_parent_exposure_summary`

## Database schema / tables / indexes / constraints
- 本轮不改数据库 schema。
- 摘要对象跟随现有 operator/replay 事件持久化链路落盘。

## Transactions, Consistency, Concurrency
- replay summary 与 decision audit 都优先读取同一稳定对象，避免 replay/postmortem 与 target/outcome 解释漂移。

## Authorization, Authentication, Data Security
- 不引入新权限模型。
- 摘要仅包含策略父腿暴露状态，不新增敏感账户凭据。

## Error Handling and Idempotency
- 新字段缺失时继续从旧扁平字段回填。
- 如果旧 payload 无足够 overlay 信息，则摘要返回 `None`，不伪造对象。

## State Transition and Lifecycle
- 专门摘要至少覆盖：
  - `parent_family`
  - `symbol`
  - `margin_mode`
  - `source_of_truth`
  - `lifecycle_state`
  - `target_signal / current_signal / effective_signal`
  - `target_qty / current_qty / effective_qty`
  - `target_active / inventory_active`

## Caching and Performance
- 仅复用现有 payload 归一化 helper，不新增额外查询。

## Logging, Monitoring, Auditing
- postmortem 可以直接读取 `overlay_parent_exposure_summary`，不必再从 target/outcome 通用 payload 手工拼装。
- replay validation 历史现在会保留该摘要，便于 residual inventory / mixed source 场景复盘。

## Testing Strategy
- unit:
  - `tests/unit/test_operator_position_states.py`
  - `tests/unit/test_audit_replay_queries.py`
- integration:
  - `tests/integration/test_operator_api.py`

## Migration, Rollback, Compatibility
- 无 migration。
- 回滚时仅需移除新增摘要字段与 backfill helper，不影响已有扁平字段读取。

## Configuration and Environment Isolation
- 无新增配置。

## Code Organization and Dependencies
- 继续复用现有 `overlay_parent_exposure` 稳定对象，不新增独立 repo 或服务。

## Documentation and Operations Manual
- 本文档即本轮交付说明。

## Deployment and Acceptance Criteria
- lint 通过
- 相关 unit tests 通过
- 最窄 operator API integration test 通过
- replay validate / replay recent / ai decision audit 都能直接读取 `overlay_parent_exposure_summary`
