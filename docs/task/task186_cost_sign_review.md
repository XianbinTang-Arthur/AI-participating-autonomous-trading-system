# Task 186 - 资金费 / 借币费 / 账单类页面正负号复查

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## Business objectives and boundaries

- 复查资金费、借币费、账单解释链以及相关收益归因页面的正负号语义。
- 修正与成交手续费页同类的“原始成本被显示成正号”问题。
- 仅改前端展示层和前端回归测试，不改后端账务、持久化或 API 字段语义。

## Current behavior summary

- 成交列表与成交详情已统一修正为：原始手续费成本显示为负，rebate 显示为正。
- 资金费相关字段在后端主要分为两类：
  - 净影响字段：正数收入、负数支出。
  - 成本字段：正数表示成本大小。
- 策略归因页里 `SleevePnLRecord.fee_amount` 仍直接按 signed 数显示，存在把原始成本显示成正号的风险。

## Module responsibilities and domain model

- `aats/api/static/modules/views/strategy-view.js`
  - 展示策略归因、套利成本和运行时收益摘要。
- `aats/api/static/modules/views/risk-view.js`
  - 展示 trial guard / guarded live run packet 等净收益与资金费摘要。
- `aats/api/static/modules/trade-display.js`
  - 已经提供成交手续费符号归一化语义，本任务只参考其显示规则。
- `tests/integration/test_dashboard_ui.py`
  - 增补前端渲染回归，锁住费用与资金费的显示方向。

## Input/output interfaces

- 输入：
  - `SleevePnLRecord.fee_amount`
  - `SleevePnLRecord.funding_fee_amount`
  - `summary_metrics.funding_fee_net_pnl`
  - `trialGuard.daily_funding_fee_net`
- 输出：
  - 成本型手续费：显示为负向影响
  - rebate：显示为正向影响
  - 净收益 / 净资金费：显式展示正负号

## Database schema / tables / indexes / constraints

- 无 schema 变更。

## Transactions, Consistency, Concurrency

- 纯前端展示修正，无事务语义变化。

## Authorization, Authentication, Data Security

- 不新增权限点，不接触敏感数据。

## Error Handling and Idempotency

- 字段缺失时继续显示原有 fallback。
- 重复刷新页面的展示结果一致。

## State Transition and Lifecycle

- 无状态机变化。

## Caching and Performance

- 仅新增常量级字符串格式化逻辑，无额外 I/O。

## Logging, Monitoring, Auditing

- 不新增日志。

## Testing Strategy

- 前端集成回归：
  - 策略归因页手续费成本显示为负。
  - rebate 显示为正。
  - 风险页净收益 / 资金费净影响保留显式方向。

## Migration, Rollback, Compatibility

- 无 migration。
- 回滚方式：恢复 `strategy-view.js` / `risk-view.js` 的显示逻辑。

## Configuration and Environment Isolation

- 无新增配置。

## Code Organization and Dependencies

- 仅修改现有前端视图文件与测试文件。
- 不新增第三方依赖。

## Documentation and Operations Manual

- 本文档记录本轮复查范围和修复边界。

## Deployment and Acceptance Criteria

- 策略归因页不再把手续费成本显示成正号。
- 风险页净收益/资金费字段显示方向明确。
- lint、相关测试、全量 unit 通过。
