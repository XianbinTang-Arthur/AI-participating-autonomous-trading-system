## Business objectives and boundaries

- 把 `derivatives_live` 收敛成与当前 `Independent` 实盘主路径一致的显式配置，不再依赖“`directional` + overlay cutover”作为主要对外语义。
- 修复仍停留在旧阶段的运行手册描述，避免继续把 `independent` 误标成只能 `dry_run / replay_only`。
- 补齐 live PostgreSQL 缺失的 schema migration，确保当前代码依赖的执行归因列已经真实存在。
- 不在本轮顺手打开新的 live 语义开关；只显式写出当前应保持的值。

## Module responsibilities and domain model

- `configs/strategy_profiles/derivatives_live.yaml`
  - 负责 `derivatives_live` 托管 profile 的当前主策略与 `independent` 实盘参数。
- `docs/derivatives_overlay_rollout_runbook.md`
  - 负责合约 overlay 的当前运维说明，必须与仓库真实能力一致。
- `tests/unit/test_env_profiles.py`
  - 校验托管 profile 加载出的值与 live 语义一致。
- `tests/integration/test_strategy_runtime_integration.py`
  - 校验最窄运行时场景下，`derivatives_live` 确实以 `independent` 作为显式主策略运行。
- live PostgreSQL
  - 必须应用到当前 `migrations/*.sql` 的最新版本。

## Input/output interfaces

- 不修改 public API 路径。
- 只修改：
  - `derivatives_live` 托管 profile 输出值
  - 运行手册文案
  - live DB schema version / 缺失列

## Database schema / tables / indexes / constraints

- live 库当前缺少 `0003_postgres_execution_attempt_id_columns.sql`。
- 本轮要求补齐：
  - `execution_orders.execution_attempt_id`
  - `execution_fills.execution_attempt_id`
  - `fill_outcomes.execution_attempt_id`
  - 对应索引
  - `schema_migrations` 记录

## Transactions, Consistency, Concurrency

- migration 使用现有 SQL 脚本原子执行。
- 配置改动仅影响启动后的 settings 解析，不引入新的并发路径。

## Authorization, Authentication, Data Security

- 不改动任何凭证字段值。
- 不在文档或日志中输出 `.env.derivatives.live` 内的敏感信息。

## Error Handling and Idempotency

- migration 脚本使用 `IF NOT EXISTS` 语义，可安全重复执行。
- profile 值调整保持向后兼容；旧兼容逻辑仍保留，只是不再作为 `derivatives_live` 的主语义。

## State Transition and Lifecycle

- `derivatives_live` 的主策略生命周期应显式表现为 `independent`。
- `opportunistic` 与 `protective` 在该 profile 中继续保持关闭。

## Caching and Performance

- 无新增重型查询。
- startup / runtime 读取 profile 值的开销不变。

## Logging, Monitoring, Auditing

- runtime/operator 页面看到的 `strategy_family_active` 将与当前 live 主策略一致。
- migration 补齐后，执行链的 `execution_attempt_id` 审计字段可真实落库。

## Testing Strategy

- lint：本轮涉及文件
- unit：`test_env_profiles.py`
- integration：`test_strategy_runtime_integration.py`
- live DB：执行 migration 后做 schema 校验查询

## Migration, Rollback, Compatibility

- 回滚配置：恢复 `derivatives_live.yaml` 对应字段即可。
- 回滚 DB：原则上不回滚列删除；如需回滚，只停止消费 `execution_attempt_id`。
- 兼容逻辑保留：coordinator 仍支持旧的 directional overlay cutover 路径。

## Configuration and Environment Isolation

- 仅修改 `derivatives_live`。
- `derivatives`、`spot`、`spot_live` 不受影响。

## Code Organization and Dependencies

- 只改 live profile、运行手册和对应测试。
- 不做无关架构改造。

## Documentation and Operations Manual

- 更新 `docs/derivatives_overlay_rollout_runbook.md` 使其与当前仓库能力一致。
- 本文档作为本轮 SOW/收口说明。

## Deployment and Acceptance Criteria

- `derivatives_live` 加载后：
  - `strategy_family_active == "independent"`
  - `strategy_family_auto_selection_enabled == false`
  - `smart_arbitrage_enabled == false`
  - `strategy_hedge_overlay_mode == "independent"`
- live PostgreSQL：
  - 已应用 `0003_postgres_execution_attempt_id_columns.sql`
  - 三张执行表存在 `execution_attempt_id`
- lint / unit / 最窄 integration 全部通过
