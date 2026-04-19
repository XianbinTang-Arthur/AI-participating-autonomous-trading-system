# Task 177 - 状态机违规收口、命令尝试计数修正、obligation 手续费隔离、启动恢复计数拆分

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## Business objectives and boundaries
- 将 `independent` 非法状态转移从“仅记录”升级为“安全降级并阻断动作”，避免已知非法转移继续进入 live action。
- 将 execution command 的 `attempt_count` 语义统一为“领取执行次数”，消除一次执行被双计数的问题。
- 为 obligation fill 消费路径增加未知手续费币种的隔离处理，避免单条坏 fill 直接中断保留金推进。
- 将启动恢复中的命令计数从混合口径拆分为 `PENDING` 与 stale `SENT` 两类，降低 operator 误判。
- 保持 public API 兼容，新增字段使用加性方式。

## Module responsibilities and domain model
- `independent/state_machine.py` 负责状态合法性判定与状态推进。
- `independent/engine.py` 负责在发现状态机违规时将 decision 降级为安全状态。
- `execution_command_repo_postgres.py` 负责命令状态持久化与尝试次数维护。
- `execution_engine/obligations.py` 负责 fill 对保留金 obligation 的消费与异常隔离。
- `recovery_control/startup_recovery.py` 负责 phase4 启动恢复摘要与阻断原因。

## Input/output interfaces
- `IndependentBookDecision`/`StrategyBookRuntimeState` 继续暴露 `transition_valid` 与 `transition_violation_reason`，新增行为约束但不改字段名。
- `RecoveryStatus` 增加加性计数字段，用于区分 `PENDING` 与 stale `SENT` 命令。
- `OrderObligation` 增加加性 processing-failure 字段，用于保留 obligation 层的隔离原因与明细。

## Database schema / tables / indexes / constraints
- 不新增表。
- 不改现有命令表唯一约束。
- obligation 加性字段继续依赖现有 payload/raw payload 持久化。

## Transactions, Consistency, Concurrency
- `attempt_count` 只在 `claim_command()` 增长，terminal 状态更新不再重复增长。
- obligation 隔离发生在单条 fill 处理边界，避免异常向上传播。

## Authorization, Authentication, Data Security
- 本轮不改认证授权模型。

## Error Handling and Idempotency
- 非法状态转移不会继续执行原 book action，而是阻断并保留 violation reason。
- 未知手续费币种在 obligation 路径中转为隔离状态，不再直接抛异常打断调用方。

## State Transition and Lifecycle
- 对 `independent`：
  - 非法转移时降级到 `blocked`
  - 对空仓使用 `cooldown`
  - 对持仓使用 `suspended`
- 对 execution command：
  - `attempt_count = claim 次数`

## Caching and Performance
- 启动恢复命令计数改为数据库聚合查询，避免 `limit=1000` 截断。

## Logging, Monitoring, Auditing
- obligation processing failure 明细保存在 obligation 对象中，便于 operator/recovery 后续读取。
- 恢复状态新增更细的命令计数字段。

## Testing Strategy
- 增加/更新：
  - `independent` 状态机违规阻断测试
  - execution command attempt_count 计数测试
  - obligation 未知手续费币种隔离测试
  - phase4 recovery 计数拆分测试

## Migration, Rollback, Compatibility
- 所有新增字段均为加性字段。
- 现有调用面保持兼容。

## Configuration and Environment Isolation
- 不新增配置项。

## Code Organization and Dependencies
- 仅修改直接相关模块，不做无关重构。

## Documentation and Operations Manual
- 本文档记录本轮变更边界与验收口径。

## Deployment and Acceptance Criteria
- 非法状态转移不再继续执行原动作。
- `attempt_count` 不再双计数。
- obligation 遇到未知手续费币种不再直接抛异常。
- phase4 recovery 能区分 `PENDING` 与 stale `SENT` 命令，并避免与 `stuck_sent_submit_order_count` 混淆。
