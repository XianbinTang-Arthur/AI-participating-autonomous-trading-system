# Task 188 - 策略判断页当前候选与自动调度收口

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## Business objectives and boundaries

- 将策略判断页“当前候选与自动调度”区域收口为仅保留摘要卡，不再展示候选表、调度结论、sleeve 意图、预算快照、冲突解算、净额决策等明细。
- 同步清理策略页数据流中仅服务这些已删除明细的后端字段，避免继续向该页面下发无用内容。
- 不改动其它工作区的展示目标与交互，不做无关重构。

## Current behavior summary

- 当前“当前候选与自动调度”卡片包含四个摘要卡，但其下方仍附带大量调度与分配明细。
- 策略页 bundle 当前会携带候选列表、最近 sleeve 意图、预算快照、冲突解算、净额决策、最近执行包等数据，主要用于这块展开展示。

## Module responsibilities and domain model

- `aats/api/static/modules/views/strategy-view.js`
  - 负责策略页该区域卡片结构与内容拼装。
- `aats/api/auth_routes.py`
  - 负责 dashboard bundle 面向策略页的后端数据装配路径。
- `aats/services/operator/query_service.py`
  - 提供原始 `strategy/runtime` 查询载荷。
- `tests/integration/test_dashboard_ui.py`
  - 锁定策略页该区域的前端渲染行为。

## Input/output interfaces

- 输入：
  - `GET /dashboard/bundle?view=strategy`
  - `renderStrategyView(data)`
- 输出：
  - “当前候选与自动调度”仅保留摘要卡内容。
  - 策略页 bundle 不再携带该区域已删除明细所需字段。

## Database schema / tables / indexes / constraints

- 无数据库变更。

## Transactions, Consistency, Concurrency

- 纯查询与前端渲染收口，无事务语义变化。

## Authorization, Authentication, Data Security

- 不新增权限点，不改变认证流程。

## Error Handling and Idempotency

- 缺少被删除字段时页面仍应稳定渲染摘要卡。
- 重复刷新策略页不会产生额外副作用。

## State Transition and Lifecycle

- 无状态机变化。

## Caching and Performance

- 减少策略页 bundle 体积与前端渲染开销。

## Logging, Monitoring, Auditing

- 不新增日志与审计事件。

## Testing Strategy

- 更新策略页渲染测试，确认该区域仅剩摘要卡。
- 更新策略页 bundle 路径相关断言，确认删除的明细不再出现在策略页渲染结果里。
- 运行 lint、unit tests、受影响集成测试；若被仓库现有错误阻断，明确记录。

## Migration, Rollback, Compatibility

- 无 migration。
- 本次优先清理策略页 bundle 路径，避免扩大到不必要的通用接口兼容面。

## Configuration and Environment Isolation

- 无新增配置。

## Code Organization and Dependencies

- 仅修改现有前端视图、auth bundle 裁剪逻辑、文档与测试。
- 不引入第三方依赖。

## Documentation and Operations Manual

- 本文档记录本次收口范围与验收点。

## Deployment and Acceptance Criteria

- “当前候选与自动调度”只保留截图所示摘要内容。
- 策略页不再显示候选表、预算快照、冲突解算、净额决策等内容。
- 策略页 bundle 不再下发这些删除内容所需的冗余字段。
- lint 通过，其他测试按仓库当前状态尽可能验证并记录结果。
