## Task 141 - Overlay Inventory Continuity And Live Family Config Alignment

### Business objectives and boundaries
- 修正 `protective` 与 `opportunistic` 在 directional target 变为 `flat` 时过早 `inactive` 的问题。
- 让 overlay family 在 target 已 flat 但真实库存仍存在时，继续按真实库存管理 overlay 腿。
- 将 `derivatives_live.yaml` 中 family 相关 live 配置显式化，特别是 `independent` 的安全参数。
- 不改 allocator、risk 主逻辑，不改非合约 family。

### Module responsibilities and domain model
- `protective_family.py` / `opportunistic_family.py`
  - 负责 overlay family 的主腿方向推断、overlay decision、candidate leg 构建。
- `derivatives_live.yaml`
  - 负责 live profile 的显式策略配置，不依赖隐式默认值解释关键行为。
- `test_*family.py`
  - 负责验证 overlay family 在 target flat 但库存未清时的行为连续性。
- `test_env_profiles.py` / `test_strategy_runtime_integration.py`
  - 负责验证 managed live profile 与 runtime 暴露的显式配置。

### Input/output interfaces
- 输入：
  - `DecisionContext.current_long_position_qty`
  - `DecisionContext.current_short_position_qty`
  - directional target 的 `long_target_qty / short_target_qty`
- 输出：
  - `HedgeOverlayDecision`
  - `StrategyCandidate`
  - live profile 的 family 参数

### Database schema / tables / indexes / constraints
- 无数据库 schema 变更。

### Transactions, consistency, concurrency
- 纯内存决策逻辑与配置变更，无事务或并发模型调整。

### Authorization, authentication, data security
- 不涉及鉴权变更。

### Error handling and idempotency
- overlay family 在 target flat 且无库存时仍返回 `inactive`，保持幂等。
- 仅在存在真实库存时改为继续按库存推断主腿并进入 closing/holding 语义。

### State transition and lifecycle
- 新增生命周期约束：
  - `target flat + residual inventory` 不再直接 `inactive`
  - 先按真实库存确定主腿/overlay 腿，再继续走原有 min-hold / cooldown / closing 状态机

### Caching and performance
- 仅增加常量级库存方向推断，无可见性能风险。

### Logging, monitoring, auditing
- 通过新增 `*_main_signal_inferred_from_inventory` reason code 改善审计可解释性。
- 通过在 live profile 中显式配置 `independent` 安全参数，降低 runtime 摘要解释歧义。

### Testing strategy
- unit：
  - protective target flat + residual inventory
  - opportunistic target flat + residual inventory
  - managed derivatives live profile 显式参数
- integration：
  - managed derivatives live runtime 读取并暴露新的 independent 安全参数

### Migration, rollback, compatibility
- 向后兼容：
  - 无库存时仍保持原有 `inactive` 行为
  - 仅对 residual inventory 场景修复
- rollback：
  - 可单独回退 family 文件和 YAML 显式参数

### Configuration and environment isolation
- 只修改 `configs/strategy_profiles/derivatives_live.yaml`
- 不改 `.env.derivatives.live`

### Code organization and dependencies
- 变更限制在：
  - `aats/services/strategy_engines/families`
  - `configs/strategy_profiles`
  - `tests`
  - `docs`

### Documentation and operations manual
- 本文档记录本次修复目的、边界与验证面。

### Deployment and acceptance criteria
- `protective/opportunistic` 在 `target flat + residual inventory` 下不再直接 `inactive`
- `derivatives_live.yaml` 显式包含 independent 的 close/buffer/cost/passive-first 相关配置
- lint、unit、最窄 integration 全通过
