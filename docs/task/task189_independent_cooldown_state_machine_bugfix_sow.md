# Task 189: Independent Guard-State 残留锁死 Bugfix

## 业务目标与边界
- 修复 `independent` 在上一轮 `blocked` 被持久化为 guard-state（`cooldown` / `suspended`）后，下一轮真实 blocker 已消失却仍无法重新进入正常生命周期的问题。
- 仅修复 `independent` 状态机与 runtime state 归一化语义，不改 allocator、coordinator、execution policy，也不在本轮调整 `score_stability` 阈值或公式。
- 保持现有 public API、数据库 schema 与 runtime payload 兼容。

## 模块职责与领域模型
- `aats/services/strategy_engines/independent/engine.py`
  - 负责基于当前上下文和上轮 runtime state 生成本轮 `IndependentBookDecision`。
  - 本轮需要在进入状态机前归一化已过期的 `cooldown_until`。
- `aats/services/strategy_engines/independent/state_machine.py`
  - 负责从 decision 派生 `book_state`，并校验 `prior -> next` 状态转移是否合法。
  - 本轮需要把“零仓位 blocked 残留”与“真实冷却态”拆开。
- `aats/services/strategy_engines/independent/diagnostics.py`
  - 继续负责把 decision 转成 runtime state，并为真实冷却态暴露 `cooldown_until`。

## 输入输出接口
- 输入：
  - `StrategyBookRuntimeState.book_state`
  - `StrategyBookRuntimeState.cooldown_until`
  - `IndependentBookDecision.blocked_reasons`
  - `DecisionContext.as_of_ts`
- 输出：
  - 合法的 `IndependentBookDecision.book_state`
  - 正确的 `transition_valid` / `transition_violation_reason`
  - 对零仓位 blocked 但无真实冷却的场景输出 `flat`，而不是伪造 `cooldown`

## 数据库 Schema / 表 / 索引 / 约束
- 无数据库 schema 变更。
- 继续复用现有 runtime snapshot、allocation decision、audit payload 持久化结构。

## 事务、一致性、并发
- 无新增事务。
- 修复重点是一致性：当 `cooldown_until` 为空或已过期时，状态机不得再把零仓位 blocked 视为真实冷却。
- 并发模型不变，仍按单轮决策快照顺序推进。

## 授权、认证、数据安全
- 无鉴权或数据安全模型变化。
- 不引入新的外部输入面。

## 错误处理与幂等
- 非法状态转移仍需 fail-closed，并保留 `independent_state_transition_invalid` 审计语义。
- 但当 prior state 只是“伪冷却残留”时，应先归一化为 `flat`，避免误报非法转移。
- 同一输入多次评估应得到相同的归一化结果，保持幂等。

## 状态迁移与生命周期
- 真实冷却态定义：
  - `prior_book_state == "cooldown"`
  - 且 `cooldown_until` 存在
  - 且 `cooldown_until > as_of_ts`
- 真实挂起态定义：
  - `prior_book_state == "suspended"`
  - 且 `suspended_until` 存在并未过期，或当前仍有 `trial_guard` blocker
- 伪 guard-state 定义：
  - `prior_book_state == "cooldown"`
  - 但 `cooldown_until` 为空或已过期
  - 或 `prior_book_state == "suspended"`，但 `suspended_until` 为空/已过期且当前没有 `trial_guard`
- 修复后生命周期规则：
  - `blocked` 且无真实 guard-state blocker 时，`book_state` 回归 inventory-backed 基础状态：
    - 零仓位回归 `flat`
    - 有仓位回归 `holding`
  - `blocked` 且存在真实冷却 blocker 时，`book_state` 保持 `cooldown`
  - `blocked` 且存在真实挂起 blocker 时，`book_state` 保持 `suspended`
  - `cooldown` / `suspended` 仅在真实 blocker 仍有效时保持 guarded prior state；伪 guard-state 需先归一化成 `flat` 或 `holding`

## 缓存与性能
- 仅增加轻量级状态归一化判断，不引入额外 IO。
- 对单轮决策耗时影响可忽略。

## 日志、监控、审计
- 保留现有 `transition_valid` / `transition_violation_reason` 输出。
- 修复目标是减少伪造的 `independent_transition_invalid:cooldown->probing` 审计噪音。
- 不新增日志通道。

## 测试策略
- 单元测试：
  - 零仓位 blocked 且无冷却 blocker 时派生 `flat`
  - 零仓位 blocked 且有冷却 blocker 时仍派生 `cooldown`
  - 零仓位 blocked 且有 trial guard / suspended horizon 时仍派生 `suspended`
  - stale `cooldown` / stale `suspended` 在零仓位下允许重新进入 `probing`
  - stale `cooldown` / stale `suspended` 在有仓位下允许回到 `holding -> building/de_risking`
  - `prior_book_state="cooldown"` / `prior_book_state="suspended"` 且 guard horizon 未来未到期时，仍保持非法转移保护
- 最窄集成测试：
  - 验证 `evaluate_independent_book` 在伪冷却 runtime state 下可以重新生成 `open/probing`

## 迁移、回滚、兼容性
- 无 migration。
- 回滚方式为恢复 `independent` 状态机与 engine 的本次变更。
- 兼容现有 runtime schema；只修正状态语义，不改变字段形状。

## 配置与环境隔离
- 不新增配置项。
- 不修改 `.env`、profile 或 live 阈值。
- 影响范围限定在 `independent` family 状态机逻辑。

## 代码组织与依赖
- 最小修改目标文件：
  - `aats/services/strategy_engines/independent/state_machine.py`
  - `aats/services/strategy_engines/independent/engine.py`
  - `tests/unit/test_independent_state_machine.py`
  - `tests/unit/test_independent_engine.py`
  - 如有必要补一个最窄 integration test
- 不引入新依赖。

## 文档与运维手册
- 本文档作为本次 bugfix 的范围说明。
- 若修复上线后仍有“不下单”反馈，下一轮再单独处理 `score_stability` 语义与阈值回放。

## 部署与验收标准
- 零仓位 blocked 且无真实冷却时，不再把 `book_state` 持久化为 `cooldown`。
- 伪冷却残留不再触发 `independent_transition_invalid:cooldown->probing`。
- 有真实未过期 `cooldown_until` 时，状态机保护仍然生效。
- 相关 lint、unit tests、最窄 integration test 通过，且不引入 public API 变化。
