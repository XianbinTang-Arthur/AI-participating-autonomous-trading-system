# Task 160: Postgres 幂等写入收口与 Independent Expectancy Fail-Closed

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## Business objectives and boundaries
- 把执行命令、inbox、fill 的 Postgres 幂等写入从“先查后插”升级成数据库冲突收口，降低并发竞态风险。
- 把 `independent` 的 expectancy 解析失败从 directional fallback 改成 fail-closed，不再让新风险继续走正常准入流程。
- 本轮不改 public API，不扩展到其它 strategy family，不做无关重构。

## Module responsibilities and domain model
- `execution_command_repo_postgres.py`
  - 负责 `execution_commands` 的幂等落库与重复写入收口。
- `inbox_repo_postgres.py`
  - 负责 `external_event_inbox` 的去重落库与重复递送收口。
- `execution_fill_repo_v2_postgres.py`
  - 负责 `execution_fills` 的重复 fill 去重与幂等落库。
- `independent_family.py`
  - 负责 `independent` 书级 expectancy 解析与 fail-closed 准入/持仓管理语义。

## Input/output interfaces
- 保持现有 repo 方法签名不变：
  - `enqueue_command(...)`
  - `enqueue_command_in_session(...)`
  - `save_incoming(...)`
  - `save_fill(...)`
  - `save_fill_in_session(...)`
- 保持 `evaluate_independent_books(...)` 和 family candidate 对外结构不变。

## Database schema / tables / indexes / constraints
- 不新增 schema。
- 继续依赖现有唯一约束：
  - `execution_commands.command_id`
  - `execution_commands.idempotency_key`
  - `external_event_inbox.inbox_id`
  - `external_event_inbox.dedupe_key`
  - `execution_fills.fill_id`
  - `execution_fills(source_system, venue_fill_id)`

## Transactions, Consistency, Concurrency
- 改成数据库原生 `ON CONFLICT DO NOTHING`。
- 冲突后回查现有记录并按幂等成功收口，而不是让应用层暴露唯一键异常。
- 不再依赖 check-then-insert 作为并发保护主手段。

## Authorization, Authentication, Data Security
- 本轮不改鉴权和安全边界。

## Error Handling and Idempotency
- 如果 DB 冲突发生但回查不到现存记录，抛显式运行时错误，避免静默吞掉异常状态。
- `independent` expectancy 解析失败时：
  - 不再回退 directional fallback signal/cost/net edge
  - 新开仓/加仓 fail-closed
  - 既有持仓仍允许按非 expectancy 的退出/收缩路径管理

## State Transition and Lifecycle
- `independent` 新风险：
  - expectancy 失败 -> `blocked`
- 既有持仓：
  - expectancy 失败不再自动映射成 directional fallback thesis
  - 仍可按 stale / execution health / liquidity / score close-threshold 进入管理路径

## Caching and Performance
- 仓储层减少一次或多次前置查询，改为由数据库唯一约束直接裁决冲突。
- 在冲突路径上做一次回查，保持行为可解释。

## Logging, Monitoring, Auditing
- 本轮不新增事件日志主题。
- 依赖既有 blocked reason：
  - `independent_{leg}_book_expectancy_resolution_failed`

## Testing Strategy
- Postgres integration:
  - 重复 `enqueue_command` 不报错且只保留 1 条命令
  - 重复 `save_fill` 不报错且只保留 1 条 fill
- Postgres-backed storage test:
  - 重复 `save_incoming` 不报错且只保留 1 条 inbox row
- family unit:
  - expectancy 失败时新开仓 fail-closed
  - expectancy 失败时不再沿用 directional fallback net edge 关闭既有持仓

## Migration, Rollback, Compatibility
- 无 schema migration。
- 回滚只需回退 repo insert 策略与 independent resolver 语义。

## Configuration and Environment Isolation
- 本轮不新增配置项。

## Code Organization and Dependencies
- 仅修改目标模块与直接相关测试。
- 使用 SQLAlchemy PostgreSQL dialect 的 `insert(...).on_conflict_do_nothing()`。

## Documentation and Operations Manual
- 本文档记录本轮幂等与 fail-closed 变更边界。

## Deployment and Acceptance Criteria
- lint 通过
- 相关 unit tests 通过
- 最窄 Postgres integration tests 通过
- verify 脚本若失败，需要明确说明是否为环境中 `bash.exe` 不可访问
