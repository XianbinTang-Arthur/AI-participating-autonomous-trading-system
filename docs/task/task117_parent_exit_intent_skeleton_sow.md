# Task117 Parent Exit Intent Skeleton SOW

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## Business Objectives And Boundaries

- 为未来超 `max-size` 退出自动拆单建立父退出意图 / 子订单聚合模型。
- 本轮只实现第一版骨架：
  - parent schema
  - child mapping
  - 聚合器
  - `order_manager` 的最小挂接
- 本轮明确不实现：
  - 自动拆单 dispatch
  - 并行扇出
  - 父意图持久化数据库 schema / migration
  - operator 动作面板

## Current Behavior Summary

- 当前执行模型仍是 “一个 `OrderState` 对一个执行事实”。
- unknown write 已经被提升成统一语义，但系统还没有父级退出任务，因此无法安全聚合多个 child 的执行真相。
- 风险在于：一旦后续接入拆单，没有父聚合层就无法正确计算 `remaining_dispatchable_quantity`，也无法在 child unknown 时防止过度派发。

## Module Responsibilities And Domain Model

### `aats/schemas/exit_execution.py`

- 定义：
  - `ExitExecutionIntent`
  - `ChildExitOrderRef`
  - `ParentAggregateStatus`
  - `ChildExitOrderCategory`

### `aats/services/execution_engine/exit_intent_aggregator.py`

- 定义：
  - parent 创建
  - child `OrderState` → 聚合类别映射
  - child ref 构造
  - parent 聚合重算
  - parent cancel request 语义

### `aats/storage/exit_execution_repo.py`

- 提供 `InMemoryExitExecutionRepository`
- 负责 parent / child ref 的最小持久化与查询

### `aats/services/execution_engine/order_manager.py`

- 可选接入 `exit_execution_repo`
- 对 risk-reducing child 自动创建 / 附着 parent
- child 状态变化后自动触发 parent 重算
- 暂不做自动拆单；只为后续多 child 聚合提供骨架

### `aats/bootstrap/config.py`

- 仅为 memory runtime 注入 `InMemoryExitExecutionRepository`
- postgres runtime 先保持 `None`

## Input / Output Interfaces

- 输入：
  - `OrderIntent` / `LegOrderIntent` 的 risk-reducing 语义
  - child `OrderState`
- 输出：
  - 可重算的 `ExitExecutionIntent`
  - 对应的 `ChildExitOrderRef`
  - parent aggregate status / remaining quantity

## Database Schema / Tables / Indexes / Constraints

- 本轮不改数据库 schema。
- 第一版仅提供内存仓储和可选 runtime 注入。
- 为后续数据库落地保留字段和接口，不做 migration。

## Transactions, Consistency, Concurrency

- parent 聚合结果视为“可重算投影”，不是唯一真相源。
- `aggregate_version` 作为并发与调试辅助字段，在每次重算时递增。
- 真实安全控制仍依赖 child `OrderState` / fills；parent 仅做聚合控制。

## Error Handling And Idempotency

- 只对 risk-reducing order 创建 / 更新 parent。
- child ref 以 `client_order_id` 幂等覆盖。
- parent 聚合若发现 risk-reducing invariant 被破坏，升级为 `REVIEW_REQUIRED`。

## State Transition And Lifecycle

- parent 第一版状态：
  - `CREATED`
  - `DISPATCHING`
  - `WORKING`
  - `PARTIALLY_FILLED`
  - `CANCEL_PENDING`
  - `COMPLETED`
  - `CANCELED`
  - `REVIEW_REQUIRED`
  - `FAILED_SAFE`
- 仅实现聚合 derivation，不接自动 child dispatch。

## Logging, Monitoring, Auditing

- 本轮不扩展新的 operator 日志接口。
- parent / child ref 可通过内存 repo 与单测验证。

## Testing Strategy

### Unit Tests

- 聚合器：
  - 单 child fill → `COMPLETED`
  - child unknown submit 占住 `remaining_dispatchable_quantity`
  - mixed child → `PARTIALLY_FILLED`
  - cancel requested → `CANCEL_PENDING`
  - invariant 破坏 → `REVIEW_REQUIRED`

- `order_manager`：
  - risk-reducing submit 自动创建 parent
  - child 状态更新后 parent 自动重算
  - non risk-reducing submit 不创建 parent

### Integration Test

- 在真实 `OKXExecutionAdapter + OrderManager` submit 路径里验证 close leg 提交后 parent 被创建并聚合 child。

## Migration, Rollback, Compatibility

- 全部改动向后兼容。
- 未注入 `exit_execution_repo` 时，现有运行时行为不变。
- 如需回滚，仅需移除可选 repo、聚合器接线和新 schema。

## Configuration And Environment Isolation

- 无新增环境变量。
- memory runtime 默认启用 in-memory parent repo。
- postgres runtime 暂不落地该 repo。

## Code Organization And Dependencies

- 新增模块限定在 `schemas` / `services/execution_engine` / `storage`
- 避免扩散到 adapter / reconciliation / operator 的拆单执行逻辑

## Deployment And Acceptance Criteria

- risk-reducing child 能自动挂接到 parent
- child 状态变化后，parent 的 `remaining_dispatchable_quantity` 会重算
- unknown child 会占住 dispatchable 额度
- lint、相关 unit tests、最窄 integration test 全部通过
