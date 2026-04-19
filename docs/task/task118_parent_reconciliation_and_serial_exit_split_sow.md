# Task118 Parent Reconciliation And Serial Exit Split SOW

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 业务目标与边界

- 将 parent-child 退出聚合正式接入 `reconciliation / unknown-write` 收敛回路。
- 在 `order_manager` 上实现第一版保守的串行退出拆单，仅处理 risk-reducing 的 `max-size` 场景。
- 本轮明确不实现：
  - 并行扇出拆单
  - 非 risk-reducing 订单拆单
  - parent / child 持久化数据库 schema
  - operator 级父任务控制面板

## 当前行为摘要

- parent 退出聚合目前只在 child `OrderState` 被持久化时重算。
- reconciliation 能识别 unknown write，但不会回推 parent 的 `aggregate_status` / `operator_review_required`。
- OKX `max-size` 目前仍由 adapter 单笔预检查拦截；risk-reducing 订单即使已有 parent 模型，也不会自动拆成多个 child 顺序提交。

## 模块职责与领域模型

### `aats/services/execution_engine/exit_intent_aggregator.py`

- 补充 parent 刷新 helper：
  - 基于当前 child `OrderState` 重新归一化 `ChildExitOrderRef`
  - 重算 parent 的聚合量、状态与 review 标记
  - 支持 scope 过滤，避免跨 runtime 误刷新

### `aats/services/reconciliation_service/repair.py`

- 在 reconciliation 报告持久化后调用 parent 刷新 helper。
- 目标是让 aged unknown write 即使没有新的 child 状态写入，也能推动 parent 升级为 `REVIEW_REQUIRED`。

### `aats/services/execution_engine/order_manager.py`

- 在 `sync_exchange_state()` 后补一次 parent 聚合刷新。
- 实现串行退出拆单：
  - 只对 risk-reducing intent 启用
  - 只在 adapter 提供 `max-size` 上限提示时启用
  - 串行提交 child
  - 每刀前基于 parent 的 `remaining_dispatchable_quantity` 重新收敛
  - 一旦 child 进入 live / unknown / review-required，就停止继续派发

### `aats/services/execution_engine/okx_adapter.py`

- 提供供 `order_manager` 调用的 risk-reducing `max-size` 内部数量上限提示。
- 继续保持 adapter 自身的 gate 语义不变；拆单调度仍由 `order_manager` 控制。

## 输入 / 输出接口

- 输入：
  - risk-reducing `OrderIntent` / `LegOrderIntent`
  - 当前 execution repo 内的 child `OrderState`
  - reconciliation 运行时的 unknown write 年龄状态
- 输出：
  - 已重算的 `ExitExecutionIntent`
  - 串行提交得到的一组 child `OrderState`
  - 最终返回给调用方的最后一个 child `OrderState`

## 数据库 / Schema / 索引

- 本轮不新增数据库 schema。
- 继续只使用 in-memory `ExitExecutionRepository`。
- Postgres runtime 仍保持 `exit_execution_repo=None`，行为向后兼容。

## 事务、一致性、并发

- parent 继续被视为“可重算投影”，不作为唯一执行真相源。
- child `OrderState` / fills 仍是执行真相。
- 串行拆单设置硬上限，防止无限派发循环。
- 每个 child 提交后立即基于当前 repo 状态重算 parent，再决定下一刀。

## 错误处理与幂等

- adapter 无法提供 `max-size` 上限提示时，回退到单笔原行为。
- child 只要进入 unknown write / working / review-required，就停止继续自动拆单。
- 额外 child 使用稳定的 slice 派生 `intent_id` / `idempotency_key`，避免与原始 child 冲突。

## 状态迁移与生命周期

- reconciliation 持久化报告后，如果 unknown write 已 aged out，parent 可从 `WORKING` 升级到 `REVIEW_REQUIRED`。
- 串行拆单只在 parent 未进入 `REVIEW_REQUIRED` / `CANCEL_PENDING` / 终态时继续。
- child truth 变化仍先写回 `OrderState`，再触发 parent 重算。

## 日志、监控、审计

- 复用现有 `order_state_persisted` / blocked submit 日志。
- 新增串行拆单日志，记录：
  - parent id
  - slice index
  - requested quantity
  - remaining dispatchable quantity
  - max-size limit

## 测试策略

### Unit Tests

- reconciliation 触发 aged unknown write 时，parent 升级为 `REVIEW_REQUIRED`
- `sync_exchange_state()` 在无新 child 更新时仍能刷新 parent
- risk-reducing 超 `max-size` 时被拆成多个 child 串行提交
- 第一刀 child 非终态时停止后续派发
- non risk-reducing 不启用自动拆单

### Narrow Integration

- OKX live mock 下，close leg 超 `max-size` 会被拆成多笔顺序提交，parent 最终 `COMPLETED`

## 回滚与兼容性

- 不注入 `exit_execution_repo` 时，parent 刷新逻辑自动退化为空操作。
- adapter 不提供 `max-size` 提示时，order manager 保持原单笔提交行为。
- 回滚只需移除新 helper 接线与串行拆单逻辑。

## 配置与环境隔离

- 本轮不新增环境变量。
- 继续复用现有：
  - `execution_unknown_submit_review_after_seconds`
  - `execution_unknown_cancel_review_after_seconds`
  - `okx_max_order_quantity_precheck_enabled`

## 代码组织与依赖

- 聚合刷新逻辑留在 `execution_engine` 聚合器模块。
- reconciliation 只负责触发刷新，不直接 patch parent 状态。
- 拆单调度留在 `order_manager`，OKX adapter 仅提供 venue-specific 上限提示。

## 部署与验收标准

- reconciliation 运行后，aged unknown child 能让 parent 自动进入 `REVIEW_REQUIRED`
- `sync_exchange_state()` 即使没有新 child 状态，也能刷新 parent 的 unknown-write age
- risk-reducing 超 `max-size` 提交时，能够生成多个 child 并顺序提交
- 任一 child 进入 nonterminal / unknown 时，自动拆单停止继续派发
- lint、相关 unit tests、最窄 integration test 全部通过
