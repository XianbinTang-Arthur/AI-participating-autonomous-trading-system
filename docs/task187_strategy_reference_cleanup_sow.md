# Task 187 - 策略判断页配置与成本参考清理

## Business objectives and boundaries

- 删除策略判断页中“配置与成本参考”整块前端展示，避免无关信息占用主工作区。
- 同步清理仅供该分区消费的后端 `strategy/runtime` 参考字段，避免继续下发无前端消费者的数据。
- 保持“当前结论 / 当前机会 / 运行质量 / 历史归因”现有能力不变，不做无关重构。

## Current behavior summary

- 当前策略判断页在主工作区下方额外提供“配置与成本参考”折叠区。
- 该折叠区会展示统一交易成本配置、方向策略配置、智能套利配置与智能套利成本模型。
- `GET /strategy/runtime` 当前会返回 `configured_parameters.trade_costs`、`configured_parameters.directional`、`configured_parameters.smart_arbitrage` 以及 `smart_arbitrage_cost_summary`，主要服务这个参考区。

## Module responsibilities and domain model

- `aats/api/static/modules/views/strategy-view.js`
  - 负责策略判断页分区导航与各工作区卡片装配。
- `aats/services/operator/query_service.py`
  - 负责组装 `GET /strategy/runtime` 的 operator 查询载荷。
- `tests/integration/test_dashboard_ui.py`
  - 锁定策略页结构与前端渲染回归。
- `tests/integration/test_strategy_runtime_integration.py`
  - 锁定 `strategy/runtime` 载荷的集成行为。

## Input/output interfaces

- 输入：
  - `GET /strategy/runtime`
  - `renderStrategyView(data)`
- 输出：
  - 前端不再渲染 `strategy-reference` 分区及其所有卡片。
  - 后端不再返回仅供该分区使用的参考字段。

## Database schema / tables / indexes / constraints

- 无数据库变更。

## Transactions, Consistency, Concurrency

- 纯查询与前端渲染清理，无事务语义变化。

## Authorization, Authentication, Data Security

- 不新增权限点，不改变认证流程，不扩大数据暴露范围。

## Error Handling and Idempotency

- 删除展示后，页面在缺少这些参考字段时应继续稳定渲染剩余工作区。
- 重复请求 `strategy/runtime` 不引入额外副作用。

## State Transition and Lifecycle

- 无状态机或生命周期变化。

## Caching and Performance

- 减少策略页 HTML 体积与 `strategy/runtime` 返回字段，降低无效数据传输与拼装开销。

## Logging, Monitoring, Auditing

- 不新增日志与审计事件。

## Testing Strategy

- 更新策略页结构渲染测试，确认参考分区已移除且其他分区仍保留。
- 更新 `strategy/runtime` 集成测试，确认参考字段不再返回而现有核心摘要仍可用。
- 运行 lint、unit tests 与受影响的 dashboard / strategy runtime integration tests。

## Migration, Rollback, Compatibility

- 无 migration。
- 兼容性变化：
  - `GET /strategy/runtime` 将不再返回仅供策略页参考区使用的部分字段。
- 回滚方式：
  - 恢复 `strategy-view.js` 中的参考分区装配及 `query_service.py` 中对应字段组装。

## Configuration and Environment Isolation

- 无新增配置。
- 使用现有 `.venv\Scripts\python.exe` 运行验证。

## Code Organization and Dependencies

- 仅修改现有前端视图、operator 查询服务、文档与测试。
- 不引入第三方依赖。

## Documentation and Operations Manual

- 本文档记录本次清理范围、边界与验收点。

## Deployment and Acceptance Criteria

- 策略判断页不再显示“配置与成本参考”导航与内容。
- `strategy/runtime` 不再返回该分区专用的配置/成本参考字段。
- 其余策略页核心区域保持可用。
- lint、unit tests 与受影响集成测试通过。
