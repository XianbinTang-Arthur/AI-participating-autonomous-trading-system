## 背景

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


family refactor 完成后，仍有 3 条高优先级残留：

1. `independent` 的交易资格仍复用 directional 的净边际。
2. `DecisionOutcome.finalized` 在正式输出与 operator fallback 中仍固定为 `False`。
3. `StrategyCoordinatorSnapshot.margin_mode` 仍使用全局 settings，而不是本次运行目标语义。

## Business objectives and boundaries

- 让 `independent` 真正按 long/short book 独立评估 gross/cost/net。
- 让 `DecisionOutcome.finalized` 在正式决策输出链上为真。
- 让 coordinator snapshot 的 `margin_mode` 与本次 target/runtime 语义一致。
- 不改 allocator 主逻辑，不改数据库 schema，不改外部 API 字段名。

## Module responsibilities and domain model

- `independent_family.py` 负责 `independent` book 级交易资格与 candidate 评估。
- `target_position.py` 负责正式 `DecisionOutcome` 生成。
- `query_service.py` 负责 operator fallback payload，不得覆盖正式 finalized 语义。
- `coordinator.py` 负责 snapshot 审计语义，与本次 target 对齐。

## Input/output interfaces

- 保持 `evaluate_independent_books(...)` 现有公开调用签名兼容。
- 新增 internal book-level expectancy helper，并在 candidate metrics 中暴露 long/short 结果。
- `DecisionOutcome` schema 不变，只修正 `finalized` 值来源。

## Database schema / tables / indexes / constraints

- 无 schema 变更。

## Transactions, Consistency, Concurrency

- 纯内存决策逻辑修改，无额外事务语义变化。

## Authorization, Authentication, Data Security

- 无权限模型变更。
- 不读取新的密钥或敏感配置。

## Error Handling and Idempotency

- 维持当前 fallback 语义。
- book expectancy helper 计算失败时应退回安全默认，不引入部分写入。

## State Transition and Lifecycle

- `DecisionOutcome.finalized` 从正式输出开始即为 `True`。
- operator fallback 仅在缺失 native outcome 时构造 payload，但仍视为 finalized 决策结果。

## Caching and Performance

- `independent` book expectancy 每次评估最多增加 2 次单腿成本估算，属于可接受范围。

## Logging, Monitoring, Auditing

- `independent` candidate metrics 增加 long/short book 级 gross/cost/net，可用于审计和 runtime 解释。
- snapshot `margin_mode` 与 target 对齐，避免 operator/runtime 误导。

## Testing Strategy

- unit:
  - independent family book-level expectancy gating
  - target position finalized
  - operator fallback payload finalized
  - coordinator snapshot margin mode
- integration:
  - runtime / mainline 继续验证 refactor 主链无回归

## Migration, Rollback, Compatibility

- 向后兼容现有 schema 和调用。
- 如需回滚，仅撤销 helper 与字段赋值逻辑。

## Configuration and Environment Isolation

- 继续使用现有 `.env.derivatives.live`。
- 不新增环境变量。

## Code Organization and Dependencies

- 复用现有 `TradeCostService`，不新增新的服务层。
- 改动仅限 `independent_family.py`、`target_position.py`、`query_service.py`、`coordinator.py` 和对应测试。

## Documentation and Operations Manual

- 本文档作为本批修复的 SOW。

## Deployment and Acceptance Criteria

- `independent` 评估不再共享 directional 的 `expected_cost_bps / expected_net_edge_bps`。
- `DecisionOutcome.finalized` 正式输出和 operator fallback 均为 `True`。
- `StrategyCoordinatorSnapshot.margin_mode` 与 `directional_target.margin_mode` 一致。
- `ruff check .`、相关 unit tests、最窄 integration tests 通过。
