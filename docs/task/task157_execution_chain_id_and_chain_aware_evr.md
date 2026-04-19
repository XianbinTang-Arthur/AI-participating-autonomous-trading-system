# Task 157 - Execution Chain Id 与 Chain-Aware EVR

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## Business objectives and boundaries

- 为执行主链引入显式 `execution_chain_id`，让一条执行链不再只能靠 `decision_id + leg` 间接推断。
- 将该字段贯穿到执行对象、fill outcome 和 replay 校验。
- 在 Deliverable 5 的 independent `expected vs realized` 统计里，优先使用 `execution_chain_id` 做 sample key，旧数据缺字段时回退到 `decision_id + leg`。
- 本任务不引入新的数据库迁移，不新增独立 execution-chain 表，不改 allocator / risk / 下单策略。

## Module responsibilities and domain model

- `execution.py`
  - 定义 `execution_chain_id` 字段
  - 保证 `OrderIntent / LegOrderIntent / ExecutionPlan / LegExecutionPlan / OrderState / FillEvent` 可携带该字段
- `independent_family.py` / `independent_models.py` / `strategy_runtime.py`
  - 为 independent long/short book 生成稳定 chain id
  - 将 chain id 放入 `StrategyLegIntent` 与 `book_runtime_states`
- `planner.py`
  - 传递或生成 `execution_chain_id`
- `order_manager.py` / adapters / storage repos
  - 保持 `execution_chain_id` 在 order state / fill event / fill outcome 链上不丢失
- `portfolio.py`
  - `FillOutcomeRecord` 持有 `execution_chain_id`
- `replay.py`
  - 校验 intent / order state / fill 的 chain 一致性
- `query_service.py`
  - Deliverable 5 的 EVR 聚合优先按 chain key，回退 decision-leg key

## Input/output interfaces

- Inputs
  - independent book runtime state
  - strategy leg intent
  - order intent / order state / fill / fill outcome payload
- Outputs
  - `execution_chain_id` 顶层字段出现在执行与 realized 相关对象上
  - independent EVR summary 采用 chain-aware sample key

## Database schema / tables / indexes / constraints

- 本任务不新增 SQL 列或索引。
- 兼容策略：
  - 通过现有 `payload/raw_payload` JSON 持久化 `execution_chain_id`
  - legacy rows 没有该字段时，由应用层回退到 `decision_id + leg`

## Transactions, Consistency, Concurrency

- `execution_chain_id` 必须在同一条执行链上的 intent / order state / fill / fill outcome 保持一致。
- partial fills、重试、撤单重提都应保留原 chain id，不得为同一条链生成多个 id。

## Authorization, Authentication, Data Security

- 本任务不修改 auth / session / permission 边界。
- `execution_chain_id` 仅作为内部执行与诊断标识，不承载敏感用户数据。

## Error Handling and Idempotency

- 若 payload 缺少 `execution_chain_id`：
  - EVR 聚合回退到 `decision_id + leg`
  - replay 不因缺字段直接失败，但若同一 decision chain 内字段冲突，应产出问题项

## State Transition and Lifecycle

- independent 中每条 book 每个 decision 只允许一个原生 runtime state。
- 对于该 runtime state，对应的 execution chain id 必须稳定，且能映射到后续 realized fills。

## Caching and Performance

- 不新增数据库查询。
- EVR 聚合仅在现有 fill outcome 遍历中多做一次 chain-key 解析，复杂度与现状同阶。

## Logging, Monitoring, Auditing

- replay 新增 execution-chain mismatch 问题码。
- operator EVR 继续输出现有 summary，不额外引入大对象。

## Testing Strategy

- unit
  - planner / intent 保留 chain id
  - independent runtime state 带 chain id
  - EVR 忽略同 decision+leg 但不同 chain 的 stray fill
  - replay 报告 chain mismatch
- integration
  - runtime/operator payload 保持兼容
  - mainline / replay 相关窄回归通过

## Migration, Rollback, Compatibility

- 完全向后兼容：
  - 旧 payload 无 `execution_chain_id` 仍可工作
  - EVR 自动回退到 `decision_id + leg`
- 若需要回滚，只需停止使用 chain-aware key；历史数据不需要迁移

## Configuration and Environment Isolation

- 本任务不新增配置项。
- 行为由 payload 是否带 `execution_chain_id` 决定。

## Code Organization and Dependencies

- 只在现有 execution / independent / operator / replay 模块内加字段和 helper。
- 不引入新依赖。

## Documentation and Operations Manual

- 本文档作为 Task 157 的 SOW 与交付说明起点。
- 最终交付需说明：
  - chain id 生成规则
  - EVR 默认口径
  - legacy fallback 行为

## Deployment and Acceptance Criteria

- `execution_chain_id` 已进入：
  - `OrderIntent`
  - `OrderState`
  - `FillEvent`
  - `FillOutcomeRecord`
  - replay 校验
- independent EVR 已优先使用 chain-aware sample key
- 针对 stray fill 污染与 replay mismatch 的回归测试通过
