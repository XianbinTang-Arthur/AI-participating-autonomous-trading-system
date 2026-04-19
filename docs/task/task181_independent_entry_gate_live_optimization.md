## Business objectives and boundaries

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


- 优化 `independent` 家族 live 开仓门控，避免在净边际充足时被过紧的单腿成本硬门误杀。
- 将开仓确认逻辑调整为条件化确认：弱信号保持保守，强信号与高净边际允许更快放行，优先改善 short 机会捕捉。
- 保持现有 `independent` 家族 public API、运行配置结构和其他 family 行为不变。
- 不扩展为全面策略重构；仅在现有 `gates.py` / `engine.py` / 测试层做最小正确修改。

## Module responsibilities and domain model

- `aats/services/strategy_engines/independent/gates.py`
  - 负责 independent 开仓资格门和 entry quality gate 的阻断语义。
- `aats/services/strategy_engines/independent/engine.py`
  - 负责将资格门与 quality gate 组合成最终 book state / blocked reasons。
- `tests/unit/test_independent_engine.py`
  - 校验新门控语义在 representative book evaluation 场景中的表现。

## Input/output interfaces

- 输入：
  - 现有 `AATSSettings`
  - `IndependentBookExpectancy`
  - `ScoreStabilityMetrics`
  - long/short leg、score、entry threshold、净边际与成本诊断
- 输出：
  - `IndependentEligibilityOutcome`
  - `evaluate_entry_quality_gate(...)` 的 blocked reasons
  - 最终 `IndependentBookDecision.state / book_action / blocked_reasons`

## Database schema / tables / indexes / constraints

- 无数据库 schema 变更。
- 继续复用现有 `strategy_sleeve_intents` / `portfolio_allocation_decisions` / `decision_audit_records` 投影结构。

## Transactions, Consistency, Concurrency

- 无事务语义变化。
- 仅改变决策门控判定，不引入新并发路径。

## Authorization, Authentication, Data Security

- 无认证、授权或敏感数据处理变更。

## Error Handling and Idempotency

- 保持 fail-closed 原则：
  - 弱净边际仍不放行。
  - 极端成本仍应触发异常熔断。
- 仅放宽“成本略高但净边际足够”的场景，不影响现有幂等与下单链路语义。

## State Transition and Lifecycle

- `inactive -> opening`：
  - 由“净边际主判断 + 异常成本熔断 + 条件化确认”共同决定。
- `blocked`：
  - 当净边际不足、异常成本、流动性/健康度不达标或确认不足时保持 blocked。
- long / short：
  - short 允许在强信号/高净边际时使用更低确认门槛。
  - long 维持当前更保守的确认要求。

## Caching and Performance

- 无新增 IO。
- 仅增加轻量级条件判断，不引入显著性能回归。

## Logging, Monitoring, Auditing

- 保持现有 blocked reason 输出面。
- 新语义下仍通过 `blocked_reasons`、`reason_codes`、runtime snapshot 暴露开仓阻断原因。

## Testing Strategy

- unit:
  - 成本高于常规上限但净边际足够时不再被普通硬拦。
  - 成本显著异常时仍会熔断。
  - short 强信号且高净边际时 1 次确认即可开仓。
  - long 默认仍要求 2 次确认。
  - 弱信号/普通信号继续需要完整确认。
- integration:
  - 运行现有最窄 `independent` 主链测试，确认门控结果可进入既有 family/runtime 链路。

## Migration, Rollback, Compatibility

- 无 migration。
- 回滚方式：
  - 恢复 `gates.py` / `engine.py` 与对应测试即可。
- 兼容性：
  - 现有配置文件不需要新增字段即可运行。

## Configuration and Environment Isolation

- 仅影响当前 `independent` 家族开仓门控。
- 不改变 `spot`、`directional`、`smart_arbitrage` 等其他 family 的行为。
- 保持 `derivatives_live` 现有配置键兼容。

## Code Organization and Dependencies

- 最小改动：
  - `aats/services/strategy_engines/independent/gates.py`
  - `aats/services/strategy_engines/independent/engine.py`（如需传递动态确认要求）
  - `tests/unit/test_independent_engine.py`
- 不新增第三方依赖。

## Documentation and Operations Manual

- 本文档即本轮 live 门控优化的 SOW / 变更说明。
- 若行为落地后符合预期，可再补 operator 侧运行手册说明。

## Deployment and Acceptance Criteria

- 当 `expected_net_edge_bps` 明显高于安全净边际时，常规成本超限不再直接阻断开仓。
- 当 `expected_cost_bps` 进入异常区间时，系统仍会 fail-closed。
- short leg 在强信号、高净边际场景下允许 1 次确认开仓。
- long leg 维持 2 次确认。
- lint、unit tests、最窄 integration test 通过。
