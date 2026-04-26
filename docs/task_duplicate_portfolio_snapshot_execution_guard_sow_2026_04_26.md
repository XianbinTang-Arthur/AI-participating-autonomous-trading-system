# 同一持仓快照重复执行事故修复 SOW（2026-04-26）

## Business objectives and boundaries

目标是修复实盘暂停前暴露的重复执行缺陷：同一个 `portfolio_snapshot_ref` 被多个决策周期消费，执行层按不同 `intent_id` 视为新订单，导致同一持仓事实被重复开仓或重复减仓。修复范围限定在触发、决策目标管理、执行幂等三层防线，不重写 directional / independent 策略模型，不调整 alpha、阈值、仓位公式或交易品种配置。

## Module responsibilities and domain model

`decision_engine.trigger` 负责将 feature snapshot 转为串行决策周期。它必须在真正执行 `run_cycle` 前重新确认 pending trigger 仍然满足策略节流条件。

`decision_engine.target_position` 负责将当前持仓、baseline 和 AI 信号转成目标仓位。它必须避免同一持仓快照在本进程内重复触发 alpha decay reduce，同时保证 shadow / evaluation 构建不污染实盘路径的本地去重状态。

`execution_engine.order_manager` 是真实下单前的最后防线。它必须把 `portfolio_snapshot_ref + symbol + semantic action lane` 作为实盘语义幂等键，阻止同一持仓快照产生的重复订单，即使 `intent_id` 和 `client_order_id` 不同。对于 directional / one-way 路径，同一持仓快照下的相反方向风险动作也必须被阻止；只有显式 independent 双书上下文允许同一快照分别作用于 long book 和 short book。

## Input/output interfaces

输入保持现有 `FeatureSnapshot`、`MarketSnapshot`、`DecisionContext`、`OrderIntent` 和 `OrderState` 结构不变。输出仍为原有 `PositionTarget`、`OrderState` 和事件总线消息。

新增行为只体现在日志、`OrderState.status="BLOCKED"`、`submission_mode="semantic_duplicate_snapshot_blocked"`、`execution_error` 以及 guardrail flags，不新增公共 API 字段。

## Database schema / tables / indexes / constraints

不修改数据库 schema。执行层语义去重通过查询最近 `order_states` 完成，依赖已有 `portfolio_snapshot_ref`、`symbol`、`position_intent`、`execution_chain_id`、`client_order_id` 字段。无需迁移、索引或约束变更。

## Transactions, Consistency, Concurrency

执行层检查放在 `_reservation_lock` 内，在保证金 reservation preview 和订单持久化之前执行，避免并发 intent 同时通过语义检查。`process_submit_command` 入口也重复执行相同检查，覆盖持久化命令回放或恢复路径。

同一 `execution_chain_id` 的 split / fallback / retry 不被语义去重误杀，避免破坏受控拆单生命周期。不同 `execution_chain_id` 但来自同一 stale portfolio snapshot 的 directional 风险动作会被执行层阻止，包括 open_long 后 open_short 这类 lane 不相交但共享同一持仓事实的动作。

## Authorization, Authentication, Data Security

不触碰认证、授权、密钥或环境变量。日志只记录订单、决策、快照和错误标识，不输出凭证。

## Error Handling and Idempotency

触发层 stale pending 被丢弃时只记录 info 日志，不调用 `record_trigger`，因为该 pending 没有真正产生决策周期。

执行层语义重复被拦截时持久化 `BLOCKED` 状态，保留审计与幂等保护。已 `FILLED`、`PARTIALLY_FILLED`、`SUBMITTED`、`CREATED` 等有执行效果或潜在执行效果的同快照订单会阻止重复语义订单；`FAILED`、`REJECTED`、`CANCELED`、`EXPIRED`、既有 `BLOCKED` 不阻止后续恢复。

## State Transition and Lifecycle

新增 BLOCKED 状态仍走既有 `_persist_order_state`，不会绕过 Redis / Postgres / event bus 同步路径。

Target alpha decay 本地快照 guard 只在实际缩仓发生时记录快照键；min-hold 等未实际缩仓的分支不占用该键。Shadow build 不写入也不读取该实盘去重 guard，避免污染 AI shadow 评估和自动治理判断。

## Caching and Performance

执行层仅扫描最近有限数量订单状态，默认 100 条，避免全表扫描。触发层新增一次 policy revalidation，计算成本与原 trigger 判断相同。

## Logging, Monitoring, Auditing

新增日志事件：

- `features_snapshot_trigger_dropped_stale_pending`
- `semantic_duplicate_order_intent_blocked`
- `alpha_decay_reduce_duplicate_snapshot_blocked`

这些日志用于事故复盘和实盘恢复后监控。

## Testing Strategy

补充单元测试覆盖：

- pending trigger 消费前二次校验失败时不会运行 `run_cycle`。
- 同一 `portfolio_snapshot_ref` 的重复开仓 intent 被执行层 BLOCKED。
- 同一 `portfolio_snapshot_ref` 的 directional 反向开仓 intent 被执行层 BLOCKED。
- 同一 `portfolio_snapshot_ref` 的 independent 双书 long/short 开仓 intent 被允许分别执行。
- 同一 `portfolio_snapshot_ref` 的重复 reduce intent 被执行层 BLOCKED。
- 同一 `portfolio_snapshot_ref` 的第二次 alpha decay reduce 在 target 层返回 hold。
- Shadow build 在同一 engine 实例上不会被 live alpha decay guard 误判为 duplicate。

运行 ruff、相关单测和全量 unit。若集成测试环境不可用或存在既有失败，明确报告。

## Migration, Rollback, Compatibility

无迁移。回滚方式是撤销本次代码改动。对旧数据兼容，缺少 `portfolio_snapshot_ref` 的历史订单不会参与语义去重。

## Configuration and Environment Isolation

不新增配置项。行为默认启用，因为这是真金白银执行幂等防线，不应依赖 profile 手动打开。

## Code Organization and Dependencies

代码只修改现有 trigger、target_position、order_manager 模块和对应单测。不引入新依赖。

## Documentation and Operations Manual

本 SOW 即为修复说明。恢复实盘前需要确认 kill switch 仍处于暂停状态，完成本地测试，并清理或 reconcile 事故期间残留的本地 CREATED 订单。

## Deployment and Acceptance Criteria

验收标准：

- 同一 `portfolio_snapshot_ref` 重复 pending trigger 不会执行第二个 `run_cycle`。
- 同一 `portfolio_snapshot_ref`、同一 symbol、同一或 directional 相反方向的语义风险动作被 BLOCKED。
- 显式 independent 双书上下文的同快照 long book / short book 动作不被 directional stale-snapshot guard 误杀。
- alpha decay reduce 不会在同一持仓快照上连续产生多次缩仓 target。
- AI shadow 构建不占用、不读取 live alpha decay snapshot guard。
- 单元测试通过，lint 通过。
