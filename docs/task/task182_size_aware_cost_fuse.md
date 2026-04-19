# Task 182 - Size-Aware 成本模型与动态熔断

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## Business objectives and boundaries

- 将 `independent` family 的开仓成本估算从静态 bps 估算升级为与订单规模、盘口深度、参考价格相关的 size-aware 模型。
- 将 `max_acceptable_cost_bps` 从普通硬门降级为名义成本锚点，仅用于动态异常熔断。
- 保持现有 public API、配置结构、执行链路与其他 strategy family 行为兼容。
- 不在本轮引入新的数据库表、消息主题或外部依赖。

## Module responsibilities and domain model

- `aats/services/trade_costs.py`
  - 负责根据 `quantity / projected_notional / market_snapshot` 估算 size-aware execution drag。
- `aats/services/trade_drag.py`
  - 负责汇总成本组件、执行拖拽和诊断上下文，并输出统一 `TradeDragEstimate`。
- `aats/services/strategy_engines/families/independent_family.py`
  - 负责把计划下单量、市场快照和成本诊断注入 `IndependentBookExpectancy`。
- `aats/services/strategy_engines/independent/gates.py`
  - 负责用净边际做主判断，并在极端成本异常时 fail-closed。

## Input/output interfaces

- 输入：
  - `planned_delta_qty`
  - `projected_notional`
  - `MarketSnapshot`
  - `IndependentBookExpectancy`
  - `AATSSettings`
- 输出：
  - `TradeDragEstimate.execution_context`
  - `IndependentBookExpectancy.depth_consumption_ratio / size_impact_bps / cost_confidence`
  - `IndependentEligibilityOutcome.effective_max_cost_bps`

## Database schema / tables / indexes / constraints

- 无数据库 schema 变更。
- 现有审计与 runtime 投影表继续复用。

## Transactions, Consistency, Concurrency

- 仅改变决策计算逻辑，无事务边界变化。
- 所有新逻辑均为纯计算，不引入共享可变状态。

## Authorization, Authentication, Data Security

- 不涉及认证、授权或新的敏感数据写入。

## Error Handling and Idempotency

- 缺少 `market_snapshot`、深度或参考价格时自动退回到旧的静态成本估算。
- 动态熔断保持 fail-closed，但只拦截对当前规模/深度而言异常昂贵的订单。

## State Transition and Lifecycle

- `flat -> opening`
  - 先由净边际安全阈值判断是否具备开仓资格，再由动态异常熔断决定是否 fail-closed。
- `blocked`
  - 当净边际不足、成本异常、流动性质量不足或确认不足时保持 `blocked`。

## Caching and Performance

- 无新增外部 I/O。
- 仅增加轻量级深度遍历与数值计算，复杂度与盘口层数线性相关。

## Logging, Monitoring, Auditing

- 保持现有 blocked reason 语义与审计链路兼容。
- 通过 `execution_context` 与 expectancy 诊断字段提升排障可见性。

## Testing Strategy

- unit:
  - 同一市场快照下，大单成本高于小单。
  - 高净边际但普通高成本不再被静态成本门误拦。
  - 深度占用高、size impact 大时，动态熔断仍会阻断开仓。
  - short 的条件化确认逻辑不回退。
- integration:
  - 运行最窄 `strategy_runtime` 集成测试，确认独立 family 仍能接入主链路。

## Migration, Rollback, Compatibility

- 无 migration。
- 回滚方式：
  - 恢复 `trade_costs.py`、`trade_drag.py`、`independent_family.py`、`gates.py` 及对应测试。
- 兼容性：
  - 新参数均为可选，旧调用方无需修改即可运行。

## Configuration and Environment Isolation

- 保持 `derivatives_live` 现有配置键兼容。
- `strategy_hedge_independent_max_acceptable_cost_bps` 继续存在，但仅作为动态熔断的名义锚点。

## Code Organization and Dependencies

- 修改范围限定在现有成本模型、independent family、gate 与测试文件。
- 不新增第三方依赖。

## Documentation and Operations Manual

- 本文档为本轮实现的 SOW。
- 运维侧可直接观察 expectancy 与 blocked reasons 变化验证新语义。

## Deployment and Acceptance Criteria

- 成本估算会随着 `planned_delta_qty / projected_notional / orderbook_depth` 变化。
- 普通成本超 nominal 上限但净边际充足时不再默认阻断。
- 大单、浅盘口、低置信度时动态熔断能够 fail-closed。
- lint、unit tests、最窄 integration test 通过。
