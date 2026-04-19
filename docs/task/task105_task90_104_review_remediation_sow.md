# Task 105: Task90-104 审查后补丁修复 SOW

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## Business objectives and boundaries
- 目标：修复 task90 到 task104 审查中确认的高优先级缺陷，保证合约 hedge mode 的主执行链不再把 long/short 双腿串味，并把 overlay rollout 安全门禁下沉到执行层。
- 边界：只修审查确认的问题，不扩展新的交易能力，不改 public API，不改数据库 schema。

## Module responsibilities and domain model
- `aats/bootstrap/config.py`
  - 负责 `POSITION_TARGETS -> policy/risk/plan/intent/bundle` 的主总线执行编排。
  - 本轮要求：同一 symbol 的 long/short 腿必须按独立账本推进，不能共享 synthetic current。
- `aats/services/execution_engine/order_manager.py`
  - 负责标准化后的 order intent / leg intent 落地执行。
  - 本轮要求：overlay rollout 不能只靠决策层；执行层必须具备最终阻断能力。
- `aats/services/strategy_overlay_rollout.py`
  - 负责 overlay rollout 阶段判定。
  - 本轮要求：为执行层提供从 `strategy_execution_mode` 到 overlay mode 的稳定映射。

## Input/output interfaces
- 输入：
  - `PositionTarget.strategy_execution_legs`
  - `LegOrderIntent.strategy_execution_mode`
- 输出：
  - `StrategyExecutionBundle.legs`
  - `OrderState(submission_mode=leg_overlay_rollout_blocked)`

## Database schema / tables / indexes / constraints
- 不修改数据库 schema。
- 仅改变写入事件和执行状态时的正确性。

## Transactions, Consistency, Concurrency
- 一致性要求：
  - `long` 与 `short` 腿必须以 `pos_side` 作为账本隔离维度。
  - 同一 bundle 内腿的处理结果不能依赖遍历顺序。
- 并发要求：
  - 不改变 `OrderManager` 现有 reservation lock 语义。

## Authorization, Authentication, Data Security
- 不新增认证与鉴权面。
- 不扩大敏感数据暴露面。

## Error Handling and Idempotency
- overlay rollout 不允许的腿级订单要在执行层直接标记为 `BLOCKED`，并写入明确错误码。
- 重试同一腿级订单时，不能因为绕过决策层而突破 rollout gate。

## State Transition and Lifecycle
- `POSITION_TARGETS` -> `StrategyExecutionBundle`
  - 要保证腿级 `current/target/delta` 保持独立。
- `LegOrderIntent` -> `OrderState`
  - 对不允许的 overlay 腿，生命周期应在本地终止于 `BLOCKED`，不能进入 adapter submit。

## Caching and Performance
- 本轮仅增加常量级判断，不引入新的远程调用或扫描。

## Logging, Monitoring, Auditing
- 依赖现有 `OrderState.execution_error` 和 `submission_mode` 暴露 rollout gate 阻断原因。
- 依赖 `StrategyExecutionBundle.legs` 审计腿级账本正确性。

## Testing Strategy
- 单测：
  - 验证 direct `submit_leg_order()` 在 rollout 不允许时被执行层阻断。
  - 验证 rollout 已放开时不会误拦。
- 集成测试：
  - 验证 `POSITION_TARGETS` 经过主总线后，`StrategyExecutionBundle` 中的 long/short 腿仍保持独立 current/target/delta。

## Migration, Rollback, Compatibility
- 无数据迁移。
- 回滚方式：撤销本轮代码补丁即可。
- 兼容性：保留现有 schema 与运行配置，不改变外部接口。

## Configuration and Environment Isolation
- 继续沿用现有：
  - `strategy_hedge_overlay_enabled`
  - `strategy_hedge_opportunistic_enabled`
  - `strategy_hedge_independent_enabled`
  - rollout stage 配置

## Code Organization and Dependencies
- 仅触及：
  - `aats/bootstrap/config.py`
  - `aats/services/strategy_overlay_rollout.py`
  - `aats/services/execution_engine/order_manager.py`
  - 对应测试文件

## Documentation and Operations Manual
- 本 SOW 记录本轮修复目标、边界与验证策略。

## Deployment and Acceptance Criteria
- 验收标准：
  - 同 symbol `long + short` 双腿不会互相污染 current/target。
  - `independent/opportunistic` 的 rollout gate 在执行层可阻断直连腿级提交。
  - 定向 lint、单测、最窄集成测试通过。
