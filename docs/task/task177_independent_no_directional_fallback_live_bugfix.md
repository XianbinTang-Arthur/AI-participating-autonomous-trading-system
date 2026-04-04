# Task 177: Independent Live No-Directional-Fallback Bugfix

## Business objectives and boundaries
- 修复 `configured_family=independent` 时，`independent` 被自身 thesis / expectancy / risk gate 阻断却回退到 `directional` 真下单的问题。
- 区分真正 `unavailable` 与 `blocked / hold / advisory` 语义：
  - `disabled / incompatible` 允许 directional fallback
  - `blocked / inactive / hold_current / advisory_only` 保留 independent 语义
- 保持现有 public API 与 managed profile 兼容。
- 不扩大到其他 family 的大规模路由重构。

## Module responsibilities and domain model
- `coordinator.py` 负责 family 选择与最终 target 应用。
- `allocator.py` 负责批准 sleeve intent、生成 allocation decision 与 execution legs。
- `independent` 在 fixed-family live 模式下应当拥有“不可执行则 hold/advisory，不得切回 directional 执行”的主语义。

## Input/output interfaces
- 输入：
  - `strategy_family_active=independent`
  - `strategy_family_auto_selection_enabled=false`
  - `independent` candidate blocked / unavailable
- 输出：
  - `selected_family` 仍为 `independent`
  - `primary_family` 仍为 `independent`
  - `route_action` 为 `hold_current` 或 `advisory_only`
  - 无新的 `directional` execution legs

## Database schema / tables / indexes / constraints
- 无数据库 schema 变更。

## Transactions, Consistency, Concurrency
- 无新增事务要求。
- 重点保证 allocation decision、snapshot、applied target 的 family 语义一致。

## Authorization, Authentication, Data Security
- 无认证或权限模型变更。

## Error Handling and Idempotency
- 当 fixed-family `independent` 属于 blocked / hold / advisory 时，显式降级为 independent hold/advisory。
- 仅当 `independent` 真正 `disabled / incompatible` 时，允许 directional fallback。
- 不再通过兼容 fallback 产生伪装成 advisory 的 `directional` 执行腿。

## State Transition and Lifecycle
- `independent unavailable` 不再触发 `directional` live action。
- target 应用阶段若 allocation 未批准 legs，必须清空继承来的旧执行腿。

## Caching and Performance
- 无显著性能影响。

## Logging, Monitoring, Auditing
- reason codes 保留：
  - `legacy_configured_strategy_family_independent_unavailable`
  - 新增 `legacy_configured_strategy_family_independent_hold_only`
- 不再产出误导性的 `legacy_configured_strategy_directional_fallback`

## Testing Strategy
- unit:
  - fixed `independent` unavailable 时 selector 不回退 directional
  - allocator 不批准 directional
  - apply_selected_target 不保留旧 directional legs
- integration:
  - 运行时 `independent` 主家族集成测试

## Migration, Rollback, Compatibility
- 无 migration。
- 如需回滚，只需恢复 `coordinator.py` 与 `allocator.py` 路由逻辑。

## Configuration and Environment Isolation
- 仅在 `derivatives` + `strategy_family_active=independent` + `auto_selection=false` 下收紧行为。
- spot 与其他 family 的既有 fallback 不主动改变。

## Code Organization and Dependencies
- 最小修改 `coordinator.py`、`allocator.py`、对应测试。

## Documentation and Operations Manual
- 本文档作为本次 live bugfix 的变更说明。

## Deployment and Acceptance Criteria
- `independent` 被自身 gate 阻断时，不再回退 `directional` 真下单。
- `independent disabled / incompatible` 时，仍保留 directional fallback 兼容能力。
- `PortfolioAllocationDecision.primary_family` 与最终 applied target family 一致。
- `advisory_only` / `hold_current` 场景下无遗留 execution legs。
