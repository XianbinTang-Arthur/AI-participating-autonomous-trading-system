# Task116 Unknown Write Semantic Uplift SOW

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## Business Objectives And Boundaries

- 把 `submission_unknown_check_exchange:*` / `cancel_unknown_check_exchange:*` 从 adapter 层错误标签提升为 execution domain 的统一语义。
- 第一阶段只做语义上收，不改数据库 schema，不实现父子拆单或超 `max-size` 自动拆单。
- 优先保证 unknown write 状态能够被 `order_manager`、reconciliation 和 recovery/operator 视图稳定识别并收敛。

## Current Behavior Summary

- `okx_adapter` 已经会返回可跟踪的 unknown submit / unknown cancel 状态。
- `order_manager` 目前仅在 `_sync_candidates()` 里基于 `execution_error` 前缀提优，尚未统一控制 duplicate submit 或同 symbol 新增风险动作。
- reconciliation 已有 `unknown_state_details` 和 finding/report 通路，但 unknown write 仍未建模成独立 finding，也没有按年龄升级人工 review。

## File-Level Task Breakdown

### `aats/services/execution_engine/order_truth.py`

- 新增统一语义 helper：
  - `unknown_write_operation(order)`
  - `is_unknown_write_state(order)`
  - `exchange_truth_pending(order)`
  - `unknown_write_age_seconds(order, now)`
  - `requires_unknown_write_review(order, now, settings)`
  - `blocks_new_risk_actions(order)`
  - `is_risk_reducing_order_intent(intent)`
- 兼容旧数据，第一版仍从现有 `execution_error` 前缀归一化。

### `aats/bootstrap/settings.py`

- 增加 unknown write 年龄阈值配置：
  - `execution_unknown_submit_review_after_seconds`
  - `execution_unknown_cancel_review_after_seconds`
- 提供默认值，避免影响现有 profile。

### `aats/services/execution_engine/order_manager.py`

- 统一改用 `order_truth` helper，不再散落写字符串前缀判断。
- `sync_exchange_state()` / `_sync_candidates()` 保持 unknown write 高优先级。
- 在 submit path 增加两类控制：
  - unresolved unknown submit 阻断同 `execution_chain_id` / `intent_id` / `client_order_id` 的重复提交流程。
  - unresolved unknown write 阻断同 symbol 的新增风险订单，但不阻断明确的 risk-reducing intent。
- 复用现有 `BLOCKED` 订单状态承载控制结果，避免 schema 变更。

### `aats/services/reconciliation_service/comparator.py`

- 基于 `order_truth` helper 抽取 unknown write detail。
- 生成独立 finding：
  - `unknown_submit_unresolved`
  - `unknown_cancel_unresolved`
- 按年龄阈值升级为 `review_required`，并透传到：
  - `findings`
  - `unknown_state_details`
  - `recommended_operator_action`

### `aats/services/recovery_control/startup_recovery.py`

- 只验证现有 `latest_reconciliation.review_required` / `unknown_state_details` 透传是否足够。
- 本轮不改 recovery 状态机，只依赖 reconciliation report 的既有接线。

## Input / Output Interfaces

- 输入：
  - `OrderState` 上的 `status`、`execution_error`、`exchange_order_id`、`client_order_id`、时间字段
  - `OrderIntent` 上的 reduce/close 语义字段
- 输出：
  - `order_manager` 的 submit block 决策
  - reconciliation finding / `unknown_state_details`
  - `review_required` 与 `recommended_operator_action`

## State Transition And Lifecycle

- unknown submit / cancel 仍保持现有主状态：
  - `SUBMITTED`
  - `CANCEL_PENDING`
- 第一阶段通过 helper 提供 truth overlay，不重写主状态机。
- aged unknown write 由 reconciliation 升级为 `review_required`，而不是直接伪造为失败。

## Error Handling And Idempotency

- duplicate submit block 只阻断 truth 未收敛的 unknown submit，避免重复下单。
- 同 symbol 新增风险阻断只针对 add-risk intent；risk-reducing intent 继续允许恢复路径。
- unknown write 识别继续兼容现有 `execution_error` 前缀，避免老数据失真。

## Logging, Monitoring, Auditing

- 复用现有日志，不额外改 adapter 协议。
- reconciliation report 中新增明确的 unknown write finding 和 detail，便于 operator/recovery 复盘。

## Testing Strategy

### Unit Tests

- `tests/unit/test_order_manager_errors.py`
  - unresolved unknown submit 阻断同 execution chain 的重复提交
  - unresolved unknown write 阻断同 symbol 新增风险订单
  - risk-reducing intent 不被上述 symbol block 误拦截
  - `_sync_candidates()` 继续优先处理 unknown write

- `tests/unit/test_reconciliation.py`
  - unknown submit 生成 `unknown_submit_unresolved` finding
  - unknown cancel 生成 `unknown_cancel_unresolved` finding
  - aged unknown submit / cancel 升级 `review_required`
  - 未到阈值时仅生成 soft finding，不升级 review

## Migration, Rollback, Compatibility

- 不改持久化 schema。
- helper 完全兼容旧 `execution_error` 文本。
- 如需回滚，只需移除 helper 接线和新增 finding 逻辑。

## Deployment And Acceptance Criteria

- `order_manager` 不再依赖 scattered string-prefix checks 做控制决策。
- unresolved unknown submit 会阻断重复提交；unresolved unknown write 会阻断同 symbol 新增风险单。
- reconciliation report 能明确给出 unknown submit / cancel finding，并在 aged 场景升级 `review_required`。
- lint、相关 unit tests、最窄 integration test 全部通过。
