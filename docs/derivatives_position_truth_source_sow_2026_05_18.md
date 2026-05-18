# 衍生品决策仓位真值修复 SOW

## Business Objectives and Boundaries

目标是阻止 live 衍生品决策继续基于陈旧本地 `PortfolioSnapshot` 生成不可执行的 `close_long` / `open_short` bundle。范围仅覆盖决策上下文的仓位真值选择，以及 recovery 自动修复快照作为 trusted baseline 的资格判定；不修改 OKX 下单适配器、不清理生产数据库、不改变 public API。

## Module Responsibilities and Domain Model

- `DecisionContextBuilder`: 构造策略决策上下文，必须在 exchange-coupled derivatives runtime 中以新鲜 OKX account position 作为当前仓位真值。
- `ExecutionRecoveryService`: 启动恢复时验证 portfolio snapshot，可自动修复本地快照，但不能把非交易所导入的本地估算快照当作 trusted exchange baseline。
- `PortfolioSnapshot`: 仍保留本地账本视图和审计历史，不再在 live 衍生品决策中覆盖新鲜 exchange position。

## Input/Output Interfaces

输入为 `ExchangeAccountSnapshot.positions`、`PortfolioSnapshot.positions`、已有 execution fills 和 runtime settings。输出仍为 `DecisionContext`、recovery artifacts 和 portfolio snapshot，字段结构不变。

## Database Schema / Tables / Indexes / Constraints

不新增或修改表、索引、约束。涉及只读表包括 `portfolio_snapshots`、`execution_fills`、`execution_orders`、`order_states`。

## Transactions, Consistency, Concurrency

本次修复不新增事务。决策构造时使用已经同步到 account service 的 latest snapshot；若 account service 未 ready 或不新鲜，则沿用既有 fail-closed / fallback 逻辑，不引入跨线程状态写入。

## Authorization, Authentication, Data Security

不读取或输出任何 `.env`、API key、token。账户快照只在内存对象中使用，不打印敏感原始 payload。

## Error Handling and Idempotency

账户服务状态异常时不使用 stale exchange snapshot。recovery baseline 判定是纯函数，重复运行结果一致。

## State Transition and Lifecycle

衍生品 live 决策生命周期变更：当 exchange-coupled runtime 有新鲜账户仓位时，`current_position_qty`、long/short leg qty 和 position legs 来自 exchange snapshot；本地 portfolio snapshot 只保留为审计引用和非 exchange-coupled fallback。

## Caching and Performance

不新增 DB 查询。account snapshot 已由现有 refresh/cache 机制维护，决策构造只做内存过滤和聚合。

## Logging, Monitoring, Auditing

当 exchange position 覆盖了不同的本地 portfolio position 时记录 warning，暴露 phantom local position 或 stale portfolio snapshot。

## Testing Strategy

新增单元测试覆盖：

- stale local derivatives portfolio long 被空 exchange positions 覆盖为 flat。
- `recovery_auto_healed` 不再被视为 trusted exchange baseline。

保留既有 dual-leg portfolio snapshot 聚合测试，防止非 exchange-coupled runtime 退化。

## Migration, Rollback, Compatibility

无 schema migration。回滚代码即可恢复旧行为。public schema 不变。

## Configuration and Environment Isolation

只在 exchange-coupled derivatives runtime 且 account snapshot ready/fresh 时启用 exchange position truth。paper/local/replay 保持本地 portfolio 行为。

## Code Organization and Dependencies

复用 `instrument_position_states_from_exchange_positions`，不新增外部依赖。

## Documentation and Operations Manual

本 SOW 作为操作说明。若生产仍有历史 phantom snapshot，可在部署后通过正常 account refresh / decision cycle 验证 blocked bundle 是否停止。

## Deployment and Acceptance Criteria

验收标准：相关单元测试通过；live 决策上下文在 exchange flat、local phantom long 时输出 flat；recovery 不再选择 `recovery_auto_healed` 作为 trusted baseline。
