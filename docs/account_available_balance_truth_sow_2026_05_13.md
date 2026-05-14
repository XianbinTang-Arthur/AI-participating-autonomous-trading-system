# 账户可用余额真相源修复 SOW

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](project_positioning.md)。

## Business objectives and boundaries

目标是消除 exchange-coupled 运行域中把配置本金当作账户可用余额的路径。实时账户可用余额必须来自 OKX account snapshot，配置本金只允许服务本地 paper/demo 初始化；real-market paper 即使读取 OKX 账户状态，也仍保留本地纸面账本种子。本文不调整策略阈值、名义上限、仓位模式或订单状态机。

## Module responsibilities and domain model

- `OKXAccountService`: 提供账户余额、风险、持仓、挂单的 exchange snapshot。
- `DecisionContextBuilder` / `RiskEngine`: 只用 ready 且最新的 exchange snapshot 推导可用交易权益和保证金检查；账户状态刷新失败后不得消费旧快照。
- `DerivativesLiveGuardService`: 合约实盘 guard 只在 OKX 账户状态 ready 时读取账户快照；账户状态失败时不得用旧快照发布健康保证金状态。
- `PortfolioState` / `PortfolioReconstructionService` / `LedgerBackedPortfolioService`: 本地投影与重放，exchange 模式下不得用配置本金种子替代交易所基线。
- `ExecutionObligationService`: 提交前用最新 exchange 可用余额减本地 obligation。

## Input/output interfaces

输入为 `AATSSettings`、OKX account REST/WS snapshot、已有 portfolio snapshots、fills 和 obligations。输出为 `DecisionContext.available_trading_equity`、`RiskDecision`、reservation 校验、portfolio bootstrap/replay snapshot。

## Database schema / tables / indexes / constraints

本修复不新增表、索引或迁移。涉及的持久化表仍为 `portfolio_snapshots`、execution order/fill/obligation 表、ledger 表和 event store。

## Transactions, consistency, concurrency

余额检查在关键路径上先刷新账户状态，再用本地 obligation 做扣减。DB-first reservation 仍由现有事务边界保证，不扩大事务范围。

## Authorization, authentication, data security

不读取、不打印 `.env.*` 凭证。OKX 访问继续通过现有 `OKXRESTClient` 和配置注入的凭证完成。

## Error handling and idempotency

exchange-coupled 模式下账户可用余额缺失或强制账户状态刷新失败时 fail closed，返回 0 并记录 critical，而不是回落到配置本金或旧快照。RiskEngine、DecisionContextBuilder 与 DerivativesLiveGuardService 必须共享同一账户 ready 判断。已有 reservation idempotency 和 order idempotency 不变。

## State transition and lifecycle

启动时若 exchange-coupled baseline 可用，portfolio state 由 OKX snapshot 初始化；若已有可信 snapshot，则恢复沿用 snapshot；若两者都缺失，不用配置本金伪造 live 余额。local paper/demo 与 real-market paper 的本地账本初始化继续使用配置种子。recovery 的 fill gap replay 使用运行域 exchange-coupled 语义，不把 startup bootstrap 开关当作余额真相源开关。

## Caching and performance

新增账户状态强制刷新语义刷新余额、持仓、挂单、成交，并在未被限流 backoff 时刷新账户风险；低频元数据如 instruments、account config、fee、system status、bills 继续走缓存，避免每次决策/提交都打满 OKX 低频接口。

## Logging, monitoring, auditing

当 exchange 模式下可用余额来源缺失时沿用 `available_trading_equity_all_fallbacks_exhausted` critical 日志。OKX refresh 成功/失败仍由现有日志事件记录。

## Testing Strategy

补单测覆盖：
- exchange-coupled 模式不再使用 `initial_usdt_balance` 作为 portfolio/replay 初始余额；
- local paper/demo 与 real-market paper 在本地账本初始化时继续使用 `initial_usdt_balance`；
- exchange 可用交易权益优先使用 OKX balance available；
- exchange 可用余额缺失时不回落到 portfolio snapshot；
- real-market paper 在 OKX 状态不 ready 时拒绝旧 exchange snapshot，但仍可回落本地 paper portfolio snapshot；
- RiskEngine 在 OKX 状态不 ready 后不再读取旧 snapshot；
- DerivativesLiveGuardService 在 OKX 状态不 ready 后不再读取旧 snapshot，并发布不可用/只减仓 guard 状态；
- exchange-coupled 且 startup bootstrap 关闭时，fill gap replay 仍不使用配置本金；
- 账户状态强制刷新绕过主快照缓存但保留低频元数据缓存。

## Migration, rollback, compatibility

不迁移历史数据。配置字段 `initial_usdt_balance` 保留给 local paper/demo 与 real-market paper 的本地账本初始化，exchange-coupled 运行时忽略该字段作为余额真相源。回滚可恢复旧的配置本金种子行为，但会重新引入 live 余额误判风险。

## Configuration and environment isolation

live/example 配置不应再引导用户填写账户本金。真实资金规模由 OKX account snapshot 和本地 obligation 共同决定。

## Code Organization and Dependencies

新增轻量 helper 放在 portfolio service 层，避免在 bootstrap、operator replay、recovery 中重复判断 exchange 模式。无新增外部依赖。

## Documentation and Operations Manual

更新配置模板/说明，明确 `AATS_INITIAL_USDT_BALANCE` 只用于 local paper/demo 与 real-market paper 的本地账本，不作为 OKX live 可用余额来源。

## Deployment and Acceptance Criteria

验收标准：
- `rg` 不再显示 exchange live 运行路径用配置本金作为账户可用余额；
- 单测覆盖新语义；
- `ruff`、unit tests、受影响 WSL2 集成测试通过，失败需说明。
