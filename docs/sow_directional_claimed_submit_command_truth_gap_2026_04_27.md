# Directional CLAIMED Submit Command Truth Gap SOW

## Business objectives and boundaries

目标是在 OKX + BTC-USDT-SWAP directional live canary 中，把最新 `SUBMITTING` 且无 venue/fill 的执行缺口从泛化的“缺成交证据”纠正为可审计的“submit command 未形成终态订单回执”。本轮只增加只读 runtime truth 分类和测试，不修改策略、风控、执行、provider、symbol、venue、release、promotion、tuning 或 timeframe plumbing。

## Module responsibilities and domain model

`scripts/runtime_truth_report.py` 负责把 decision、bundle、execution_order、order_state、execution_command、fill/lifecycle 证据压缩为 operator/runtime truth。domain model 保持现状：decision 产生目标仓位变化，bundle/intent/order 承载执行意图，execution_command 承载提交命令，venue order/fill/lifecycle 证明真实执行闭环。

## Input/output interfaces

输入为现有数据库只读聚合结果：latest decision、execution order states、order_state states、execution command states、fill references。输出为 runtime truth JSON 中的 `execution_truth`、`execution_chain` 和 live facts 字段，新增 submit command state counts 与 `submission_gap_root_cause` 分类。

## Database schema / tables / indexes / constraints

不变更 schema、索引、约束或数据。读取范围仍限现有 `portfolio_allocation_decisions`、`strategy_execution_bundles`、`execution_orders`、`order_states`、`execution_commands`、`execution_fills` 等 truth-chain 表。

## Transactions, consistency, concurrency

本轮无写事务。分类逻辑必须在并发订单状态不完整时保守处理：当 order/order_state 仍为 CREATED/SUBMITTING 且 submit command 已存在但未闭环时，不推断 fill expected，不做恢复动作，只输出需要 terminal ack 或 exchange order id 的最小缺口。

## Authorization, authentication, data security

不读取、不输出凭证、token、API key、数据库密码或完整连接串。dashboard protected fields 仍按 `unknown_auth_required` 处理，不把 configured AI target 误当 effective runtime。

## Error handling and idempotency

runtime truth 生成失败时保持现有错误路径。新增分类为纯函数式汇总，重复运行应得到相同结论，不产生副作用。

## State transition and lifecycle

不改变真实订单状态机。语义调整为：`SUBMITTING + CLAIMED/PENDING/SENT/FAILED submit command + no venue/fill` 属于 order submission gap，不属于 terminal no-fill，也不属于 fill/lifecycle missing。

## Caching and performance

新增 command state 聚合为小范围只读计数，不引入缓存写入，不改变热路径执行性能。

## Logging, monitoring, auditing

runtime truth artifact 将记录 submit command state counts、root cause 和 smallest missing field，供 operator 后续恢复或终结任务使用。

## Testing Strategy

新增 focused unit test 覆盖 CLAIMED submit command 未闭环场景；运行 changed-file ruff、focused runtime truth tests、全量 unit tests，并在部署后生成 post-deploy runtime truth smoke。

## Migration, rollback, compatibility

无迁移。回滚方式为 revert 本次 commit 并通过 `scripts/deploy.sh --profile derivatives-live --skip-commit` 重新部署；旧字段保持兼容，只新增可选 truth 字段。

## Configuration and environment isolation

不新增配置。不改变 live profile、provider、AI mode、strategy profile 或环境变量。

## Code Organization and Dependencies

仅触达 `scripts/runtime_truth_report.py` 和对应 unit tests；不新增依赖。

## Documentation and Operations Manual

本 SOW 记录边界和验收口径。实际 operator 下一步应基于分类结果处理 stuck submit command recovery，而不是手动修改订单。

## Deployment and Acceptance Criteria

部署仅在测试通过、提交推送后使用 `scripts/deploy.sh --profile derivatives-live --skip-commit`。验收标准：

- latest executable directional truth 不再把该场景标记为 generic expected execution/fill gap。
- `submission_gap_root_cause=execution_submit_command_claimed_without_terminal_order_ack`。
- `smallest_missing_field=execution_command_terminal_ack_or_exchange_order_id`。
- `fill_expected=false` 且 lifecycle transition 不再被期待。
- post-deploy runtime truth `blocking_findings=[]` 且 deployed head 与 Windows HEAD 一致。
