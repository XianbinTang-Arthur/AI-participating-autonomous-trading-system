# Task115 Unknown Write Cancelability And Sync Priority SOW

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## Business Objectives And Boundaries

- 修复 `submission_unknown_check_exchange:*` 状态下订单无法继续取消的问题。
- 提高 unknown write state 的可观测性与后续收敛优先级。
- 仅修改 `okx_adapter`、`order_manager` 与相关测试，避免扩散到更大的拆单或对账架构调整。

## Module Responsibilities And Domain Model

- `aats/services/execution_engine/okx_adapter.py`
  - 负责 unknown submit 状态的取消前确认、按 `clOrdId` 的降级取消，以及关键日志补充。
- `aats/services/execution_engine/order_manager.py`
  - 负责后续同步候选排序，优先收敛 unknown write state。
- `tests/unit/test_guarded_simulated.py`
  - 覆盖 unknown submit 后的可取消路径与可恢复路径。
- `tests/unit/test_order_manager_errors.py`
  - 覆盖 sync 候选优先级。

## Input Output Interfaces

- 输入：
  - `OrderState(status="SUBMITTED", exchange_order_id=None, execution_error="submission_unknown_check_exchange:*")`
  - `client_order_id`
- 输出：
  - 可继续执行的取消路径，或可恢复的 `CANCEL_PENDING`
  - 更完整的 blocked / recovery 日志字段
  - unknown write state 优先进入 sync 队列

## Error Handling And Idempotency

- 允许在缺少 `exchange_order_id` 但存在 `client_order_id` 时继续尝试取消。
- 先查单补齐 `exchange_order_id`；若查不到则按 `clOrdId` 降级取消。
- 仍查不到时返回可恢复状态，不直接打成 `FAILED`。

## State Transition And Lifecycle

- `SUBMITTED + submission_unknown_check_exchange:*`
  - 可进入 `CANCEL_PENDING`
  - 后续继续由 exchange sync 收敛到 `CANCELED` / `FILLED` / 其他终态
- unknown write state 在 `order_manager._sync_candidates()` 中优先处理。

## Logging, Monitoring, Auditing

- 为 blocked submit 与 unknown write recovery 日志补充：
  - `is_risk_reducing_intent`
  - `position_intent`
  - `execution_action`
  - `leg_action`
  - `reduce_only`
  - `close_only`

## Testing Strategy

- 单测覆盖：
  - unknown submit state 后续可查到订单并取消成功
  - unknown submit state 仍查不到订单时进入可恢复 `CANCEL_PENDING`
  - sync 候选中 unknown write state 优先

## Deployment And Acceptance Criteria

- `submission_unknown_check_exchange:*` 状态不再因缺少 `exchange_order_id` 被立即取消失败。
- blocked / recovery 日志包含风险降低判定上下文。
- unknown write state 在同步队列中优先收敛。
- lint、相关单测和最窄集成测试通过。
