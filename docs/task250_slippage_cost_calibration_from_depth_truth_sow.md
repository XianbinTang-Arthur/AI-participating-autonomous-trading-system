# Task250: 深度真相驱动滑点与成本校准 SOW

## Business objectives and boundaries
- 目标：在 operator execution-science truth surface 中，把 depth-backed fill feasibility 聚合为可审计的滑点与成本校准证据。
- 边界：只读归因增强；不改变策略、风控、执行门、provider、symbol、venue、strategy family、release、promotion、tuning、schema 或 live order behavior。

## Module responsibilities and domain model
- `OperatorQueryService`：基于已有 fill evidence 汇总 `slippage_cost_calibration`。
- 域模型：实际成交价与 pre-event depth weighted average price 对比，估算 depth-backed adverse slippage bps 与 quote cost。

## Input/output interfaces
- 输入：`fill_feasibility.stage_evidence[].fill_evidence[]` 中的 depth expected price、fill notional、depth adverse slippage、depth estimated adverse cost。
- 输出：`slippage_cost_calibration`，包含 status、missing evidence、calibrated fill count、total notional、total estimated depth adverse cost、weighted/arithmetic slippage bps、top-of-book 对照成本和 per-fill calibrated evidence。

## Database schema / tables / indexes / constraints
- 不改 schema。
- 不新增查询，只消费内存中的只读 truth payload。

## Transactions, Consistency, Concurrency
- 无写事务。
- 计算对同一输入幂等。

## Authorization, Authentication, Data Security
- 不读取或输出凭证。
- 不暴露 raw payload；仅输出已有 truth surface 中的非敏感数值字段。

## Error Handling and Idempotency
- 无 fill evidence 时返回 `no_fill_evidence_for_slippage_calibration`。
- 缺少 depth evidence、fill notional 或 cost basis 时返回 `missing_depth_backed_slippage_cost_calibration`，并保留 missing evidence。

## State Transition and Lifecycle
- 不改变订单、fill、profile、runtime 或恢复状态。
- lifecycle refs 和 sequence validation 仍由原逻辑控制。

## Caching and Performance
- 只遍历当前 decision truth payload，最多聚合展示前 50 条 per-fill evidence。
- 不增加外部 IO。

## Logging, Monitoring, Auditing
- 新字段用于 UI/API 审计和后续 calibration dashboard。
- 不新增日志。

## Testing Strategy
- 单测覆盖：
  - top-only / no-sidecar fill 不伪造 depth calibration。
  - no fill stage 明确 no-fill calibration status。
  - sidecar-backed books5 depth fill 输出完整 calibration 汇总。

## Migration, Rollback, Compatibility
- 无迁移。
- 回滚：revert 本任务 commit 并通过 `scripts/deploy.sh` redeploy。
- 向后兼容既有 `fill_feasibility` 字段。

## Configuration and Environment Isolation
- 不新增配置。
- 不改变 live/staging 环境行为。

## Code Organization and Dependencies
- 只修改 operator query service、相关单测和本 SOW。
- 不新增依赖。

## Documentation and Operations Manual
- 本文件记录范围、验收和回滚方式。
- Operator 应把 calibration 视为只读成交归因，不作为自动放行或调参依据。

## Deployment and Acceptance Criteria
- 验收：
  - depth-backed fills 输出 calibrated slippage/cost attribution。
  - depth/fill/cost basis 不足时显式 missing。
  - raw payload 不出现在 calibration surface。
  - 单测、lint、部署和 runtime smoke 通过。
