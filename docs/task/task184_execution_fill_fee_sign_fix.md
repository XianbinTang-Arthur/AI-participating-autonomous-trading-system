# Task 184 - 委托与成交页手续费符号修正

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## Business objectives and boundaries

- 修正“委托与成交”页成交记录中手续费符号显示错误的问题。
- 对普通成交手续费，页面应显示为负向影响；对 rebate，应显示为正向影响。
- 仅修正显示层，不改底层账务、成交回放或 API 公共字段语义。

## Module responsibilities and domain model

- `aats/api/static/modules/views/execution-view.js`
  - 负责成交记录表和移动卡片中的“影响摘要”展示。
- `aats/services/operator/query_service.py`
  - 继续提供 `fee_amount / fee_quote_amount / fee_delta` 原始字段，不在本任务中改语义。
- `tests/integration/test_dashboard_ui.py`
  - 覆盖前端渲染回归，确保手续费支出显示为负、rebate 显示为正。

## Input/output interfaces

- 输入：
  - recent fills payload 中的 `fee_amount / fee_quote_amount / fee_delta / realized_pnl`
- 输出：
  - “影响摘要”中的手续费文案，例如 `手续费 -0.3429`

## Database schema / tables / indexes / constraints

- 无 schema 变更。

## Transactions, Consistency, Concurrency

- 无事务语义变化。

## Authorization, Authentication, Data Security

- 不新增权限点。
- 不接触敏感凭据。

## Error Handling and Idempotency

- 当手续费字段缺失时，继续显示“手续费待同步”。
- 纯前端显示修正，重复刷新结果一致。

## State Transition and Lifecycle

- 无状态机变化。

## Caching and Performance

- 无新增 I/O。
- 仅新增常量级格式化逻辑。

## Logging, Monitoring, Auditing

- 不新增日志。

## Testing Strategy

- 前端渲染回归：
  - 正常手续费成本显示为负号。
  - rebate 显示为正号。

## Migration, Rollback, Compatibility

- 无 migration。
- 回滚方式：恢复 `execution-view.js` 中原手续费展示逻辑。

## Configuration and Environment Isolation

- 无新增配置。

## Code Organization and Dependencies

- 仅修改现有前端视图文件和测试文件。
- 不新增第三方依赖。

## Documentation and Operations Manual

- 本文档记录 bug 原因和修复边界。

## Deployment and Acceptance Criteria

- 成交记录页手续费成本显示为负值。
- rebate 显示为正值。
- lint、相关测试、全量单测、最窄集成测试通过。
