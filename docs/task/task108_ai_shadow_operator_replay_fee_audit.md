# Task 108 - AI Shadow / Operator Profitability / Replay Fee Audit

## Business objectives and boundaries
- 修复 AI shadow、operator profitability、replay 三条链路中剩余的 signed fee 与比例口径问题。
- 仅修复 quote 手续费换算、gross/net realized PnL 组装、回放重建中的公式缺陷。
- 不改公开 API 形状，不改现有运营页面字段命名，不做无关重构。

## Module responsibilities and domain model
- `aats/services/accounting.py`
  - 负责把成交手续费换算成 quote 口径。
- `aats/services/portfolio_service/positions.py`
  - 负责把 fill 应用到本地组合状态，并产出 `fee_delta` / `realized_pnl_delta`。
- `aats/services/ai_service/inference.py`
  - 负责 AI shadow baseline/shadow 路径回放。
- `aats/services/operator/query_service.py`
  - 负责 profitability / execution quality 行级与汇总字段。
- `aats/services/ledger/lot_projection.py`
  - 负责 lot-based replay / reconstruction 的 realized PnL 与 fee 累积。
- `aats/services/execution_engine/recovery.py`
  - 负责 recovery 加载历史 fills 时恢复 fee 累积状态。

## Input/output interfaces
- 输入
  - `FillEvent.fee_amount`
  - `FillEvent.fee_currency`
  - `FillOutcomeRecord.fee_delta`
  - `FillOutcomeRecord.realized_pnl_delta`
- 输出
  - signed quote fee delta
  - non-negative fee cost ratio inputs
  - corrected `gross_realized_pnl`

## Database schema / tables / indexes / constraints
- 本次不修改表结构。
- 兼容现有 `fill_outcomes.fee_delta` 历史数据，允许同时处理旧的“负数表示手续费支出”和新的 signed quote delta 口径。

## Transactions, Consistency, Concurrency
- 不新增事务边界。
- 修复后，portfolio state、lot projection、operator query 对同一 fill 的净值解释保持一致。

## Authorization, Authentication, Data Security
- 不涉及鉴权与敏感信息处理变更。

## Error Handling and Idempotency
- 保持现有 fill 去重与 replay 幂等行为。
- fee 口径缺失时优先回退到现有字段，不新增异常抛出路径。

## State Transition and Lifecycle
- 不变更订单、成交、replay、recovery 的状态机。
- 仅修正状态派生数值。

## Caching and Performance
- 仅增加轻量 helper 调用，不增加额外 IO。

## Logging, Monitoring, Auditing
- 不新增日志字段。
- 通过回归测试锁定 signed fee 与 gross/net PnL 契约。

## Testing Strategy
- unit
  - portfolio state negative fee rebate
  - AI shadow fill-backed replay with rebate
  - lot projection / reconstruction rebate replay
- integration
  - operator profitability 对旧 `fee_delta < 0` 测试数据仍能给出正确 gross PnL

## Migration, Rollback, Compatibility
- 无迁移。
- 向后兼容旧 `FillOutcomeRecord.fee_delta` 测试数据。

## Configuration and Environment Isolation
- 不新增配置项。
- 验证继续使用仓库现有 `.venv` 与 PostgreSQL 测试环境。

## Code Organization and Dependencies
- 仅在现有服务层内补 helper 和最小调用点修复。
- 不引入新依赖。

## Documentation and Operations Manual
- 本文档即本次审计与修复的 SOW。

## Deployment and Acceptance Criteria
- maker rebate 不得在 AI shadow / replay 中被抹成 0。
- operator profitability 在旧 `fee_delta < 0` 数据下仍需输出正确 `gross_realized_pnl`。
- 相关 lint、unit、最窄 integration 验证通过。
