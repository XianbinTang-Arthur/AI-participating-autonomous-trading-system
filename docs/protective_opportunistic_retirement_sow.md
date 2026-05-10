# protective / opportunistic 策略退役 SOW

## Business objectives and boundaries

目标是将 `protective` 与 `opportunistic` 从当前策略运行面完全退役：新决策不再生成这两个 family 的 candidate、sleeve intent、allocation 或执行腿，配置与 operator 控制面不再暴露这两个策略。边界是只删除 strategy family / overlay 策略本身，不删除 `protective_override`、`protective_execute`、`emergency_protective_exit` 等保护性风控语义。

## Module responsibilities and domain model

`directional`、`smart_arbitrage`、`spot_grid`、`dca`、`independent` 保持为可运行策略 family。`protective` 与 `opportunistic` 仅作为历史 payload 值保留在持久化 schema 的兼容读取面，不再属于 active runtime family。

## Input/output interfaces

运行时设置只接受当前有效的 family 与 `independent` overlay mode。Operator API 输出删除 retired family 的配置块与 rollout 状态。历史查询接口仍能反序列化旧 `strategy_sleeve_intents`、`strategy_execution_bundles`、`position_targets` payload。

## Database schema / tables / indexes / constraints

本次不改 Postgres schema、不写入线上库、不删除历史行。相关历史表包括 `strategy_sleeves`、`sleeve_budget_profiles`、`sleeve_budget_assignments`、`strategy_sleeve_intents`、`portfolio_allocation_decisions`、`strategy_execution_bundles`、`execution_orders`、`order_states`、`fill_events`、`fill_outcomes`。

## Transactions, Consistency, Concurrency

无数据库迁移事务。运行时一致性由 coordinator 不再注册 retired family、allocator 不再选择 retired family、settings 不再接受 retired family 配置共同保证。

## Authorization, Authentication, Data Security

不读取或打印 `.env.*`、API key、交易所凭证。只做代码与只读结构化查询验证。

## Error Handling and Idempotency

历史 payload 兼容是主要错误处理边界。删除后重复部署应保持幂等：retired family 配置键即使残留在旧 YAML 或环境里，也不会进入运行时控制面。

## State Transition and Lifecycle

生命周期从 active runtime family 迁移为 retired legacy value。新状态机不再有 retired family 的 opening / holding / closing 路径，历史状态只用于审计读取。

## Caching and Performance

移除两个 family 后，coordinator 少做两个 engine evaluate、两个 market history request、两个 sleeve intent 保存。无新增缓存。

## Logging, Monitoring, Auditing

保留历史审计数据可读。新增或更新测试覆盖 retired family 不再生成，避免 operator 误以为策略仍可启用。

## Testing Strategy

更新单元测试覆盖 settings、coordinator、allocator、rollout、operator payload 和历史 payload 兼容。删除 retired family 专属评估测试，替换为“不再注册/不再生成”的断言。受影响集成测试改为 `independent` overlay 主链或删除 retired path 断言。

## Migration, Rollback, Compatibility

无 DB 写迁移。兼容策略是在 schema 层保留 legacy literal 读取能力，在 settings/coordinator/allocator 层删除运行能力。回滚方式是恢复代码注册与配置字段后重新部署。

## Configuration and Environment Isolation

更新 `derivatives.yaml` 与 `derivatives_live.yaml`，去掉 retired family 配置和旧 overlay mode。实盘配置继续保持 overlay disabled，默认 family 为 `directional`。

## Code Organization and Dependencies

删除 `protective_family.py` 与 `opportunistic_family.py`，清理 families package export、coordinator imports、allocator branches、operator/UI 控制面和 rollout helper。

## Documentation and Operations Manual

本文档作为本次退役说明。后续如需要彻底清库，必须另起迁移设计并先备份。

## Deployment and Acceptance Criteria

验收标准：lint 通过；unit tests 通过；最窄相关集成测试通过；`rg` 不再发现 active runtime 对 `ProtectiveFamilyEngine`、`OpportunisticFamilyEngine`、`strategy_family_protective_enabled`、`strategy_family_opportunistic_enabled` 的引用；历史 payload schema 仍能接受 legacy family 值。
