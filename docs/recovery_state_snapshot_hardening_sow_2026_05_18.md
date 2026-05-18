# Recovery State Snapshot Hardening SOW

## Business objectives and boundaries

修复恢复页面和恢复控制在多进程部署下误信旧 `ReconciliationStateSnapshot` 的问题。边界限定在恢复状态读模型、bundle blocker 清理和回归测试；不改变交易策略、下单参数或对账判定规则。

## Module responsibilities and domain model

- `RecoveryPostureEvaluator` 负责把最新对账、当前执行账本、kill switch 和运行时恢复状态归一化为可恢复状态。
- `RecoveryQueryFacade` 负责 operator API/dashboard 的恢复视图，在 gateway 进程应使用 Postgres 快照作为跨进程信号，但不能让旧快照覆盖更新的 clean 对账和当前执行账本。
- `execution_orders`、`execution_fills`、`order_states`、`strategy_execution_bundles`、`order_obligations` 仍是执行恢复判断的数据来源。

## Input/output interfaces

输入是 `/system/recovery`、dashboard `systemRecovery` 面板读取、`/system/resume` 触发的最新对账报告和恢复快照。输出是恢复状态字段：`recovery_state`、`safe_to_trade`、`resume_eligible`、`bundle_recovery_required` 和 `resume_blocked_reasons`。

## Database schema / tables / indexes / constraints

不新增 schema、索引或迁移。只改变读模型和状态归一化逻辑。

## Transactions, consistency, concurrency

GET 路径沿用现有 `finalize_status` 行为，必要时用最新对账重新生成状态快照。必须避免 `multi_process_role_skip` 覆盖 execution 进程写入的真实快照，同时允许当前账本已清理后清除旧 bundle blocker。

## Authorization, authentication, data security

不改变认证授权。不打印 `.env`、cookie、API key、交易所凭证或数据库密码。

## Error handling and idempotency

归一化必须是幂等的：相同最新对账和相同执行账本重复读取，应返回相同恢复状态，并且不会重复制造 blocker。

## State transition and lifecycle

旧状态 `review_required + strategy_bundle_recovery_requires_review` 只有在当前仍存在 bundle recovery 条件时才保留；当前 bundle 条件消失且最新对账不要求 review/halt/only-reduce 时，应转为 `normal_operation` 或 `degraded_continue`。

## Caching and performance

dashboard 摘要路径只在快照携带 bundle recovery 阻断时补充读取最新对账并归一化，避免正常路径扩大读开销。

## Logging, monitoring, auditing

不新增日志。通过既有 operator action、state snapshot 和 blocker snapshot 保持审计。

## Testing Strategy

新增/更新单元测试覆盖：

- 当前 bundle 已消失时，旧 `strategy_bundle_recovery_requires_review` 不再阻止恢复状态清理。
- dashboard recovery 摘要遇到旧 bundle 快照时，会用最新 clean 对账归一化为可运行状态。

## Migration, rollback, compatibility

无迁移。回滚为恢复旧读模型逻辑，但会重新暴露旧快照卡住 UI/恢复的风险。

## Configuration and environment isolation

不新增配置。实盘恢复仍基于当前 `derivatives-live` scope。

## Code Organization and Dependencies

变更限制在 `aats/services/governance_engine/recovery_posture.py`、`aats/services/operator/recovery_queries.py` 和对应测试。

## Documentation and Operations Manual

本 SOW 记录操作边界。实际部署仍按 `CLAUDE.md` 使用 `scripts/deploy.sh --skip-commit`。

## Deployment and Acceptance Criteria

验收标准：lint、相关单元/集成测试通过；部署成功；部署后 API 和 UI 不再显示已消失的 bundle recovery 阻断；手动恢复后 `safe_to_trade=true` 且 `resume_eligible=true`。
