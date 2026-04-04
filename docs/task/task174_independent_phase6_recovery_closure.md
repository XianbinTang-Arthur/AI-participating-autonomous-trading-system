# Task 174: Independent Phase-6 Replay / Recovery Closure

## Business objectives and boundaries
- 收口 `independent` phase-6 的 replay / recovery 闭环。
- 仅做加性恢复摘要与 replay 语义修正，不改现有启动恢复阻断策略。
- 不引入新的 live gating，不改 allocator / coordinator 选择逻辑。

## Module responsibilities and domain model
- `independent/replay.py`
  - 负责生成更可信的 decision replay snapshot
  - 负责从 allocation decision + open orders + recent bundles 组装 `independent` recovery snapshots
- `execution_engine/recovery.py`
  - 负责把 `independent` recovery snapshots 接入主恢复状态
- `execution_engine/bundle_recovery.py`
  - 负责在 bundle leg recovery 视图里补齐 chain / attempt 身份
- `startup_recovery.py`
  - 负责把新的 independent recovery 摘要保留到 phase4 恢复状态

## Input/output interfaces
- 输入
  - `PortfolioAllocationDecision.sleeve_intents[].metrics`
  - `PortfolioAllocationDecision.execution_legs`
  - `OrderState.execution_chain_id / execution_attempt_id`
  - `StrategyExecutionBundle.legs`
- 输出
  - `RecoveryStatus.independent_recovery_snapshots`
  - `RecoveryBundleLegStatus.execution_chain_id / execution_attempt_id`
  - 更可信的 `IndependentReplayDecisionSnapshot`

## Transactions, consistency, concurrency
- 仅增加恢复摘要与只读重建，不新增事务边界。
- 恢复快照优先读取当前 scope 内的 allocation decisions、open orders 和 bundles。

## Error handling and idempotency
- 若 `book_runtime_states` 无法反序列化，跳过坏行，不影响整体恢复状态。
- 新字段均为加性字段，旧 payload 保持兼容。

## State transition and lifecycle
- 不再把 replay transition 默认硬编码为 `flat -> next_state`
- 现在显式记录：
  - `prior_book_state`
  - `transition_reconstructed`
  - `transition_source`

## Logging, monitoring, auditing
- `RecoveryStatus.notes` 增加：
  - `independent_recovery_snapshots:{count}`
  - `independent_recovery_blocked_books:{count}`

## Testing strategy
- 单元测试
  - `test_independent_replay.py`
  - `test_execution_recovery.py`
- 窄集成
  - `test_operator_api.py` 的 recovery endpoint 暴露

## Migration, rollback, compatibility
- 全部字段为 additive。
- 无 schema migration，无需回滚脚本。
