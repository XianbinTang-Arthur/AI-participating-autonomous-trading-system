# Task 100 - 执行异常页 DB 归档与恢复显示修复

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../project_positioning.md)。

## Business objectives and boundaries

- 修复“委托与成交”页持续显示 `execution_sync_failed` / `db_housekeeping_failed` 的根因。
- 边界限定在后台 DB housekeeping、后台循环异常恢复记录、Operator 只读错误汇总展示。
- 不改变交易策略、下单状态机、OKX 适配器提交语义或资金风控阈值。

## Module responsibilities and domain model

- `aats/storage/housekeeping.py` 负责 event_store 热表到 archive 的批量搬运。
- `aats/bootstrap/config.py` 负责后台循环失败与恢复事件写入。
- `aats/services/operator/query_service.py` 负责 `/execution/errors` 的当前异常只读汇总。

## Input/output interfaces

- 输入：`event_store` / `event_store_archive` 行、后台循环异常对象、`execution.error_summaries` 事件。
- 输出：归档报告、恢复事件、前端消费的 `{"errors": [...]}`。
- 公共 HTTP API 字段保持兼容。

## Database schema / tables / indexes / constraints

- 不新增 schema、索引或迁移。
- 继续使用 `event_store.event_id` 与 `event_store_archive.event_id` 的唯一约束保证归档幂等。

## Transactions, Consistency, Concurrency

- Postgres 归档路径改为单条 CTE，在同一事务中完成候选选择、归档插入和热表删除。
- 每批仍是独立事务；失败时该批 rollback，热表不丢数据。
- 并发归档依赖 `ON CONFLICT DO NOTHING` 和唯一约束保持幂等。

## Authorization, Authentication, Data Security

- 不读取或输出任何 `.env` 凭证。
- 不新增权限点，不改变前端鉴权。

## Error Handling and Idempotency

- 后台循环失败仍记录 `ExecutionErrorSummary` 与 `ProcessingFailureRecord`。
- 同一子系统失败后，下一次成功写入 recovered summary；Operator 查询按子系统最新状态过滤，避免旧失败覆盖已恢复事实。
- 归档重复运行不会重复插入 archive。

## State Transition and Lifecycle

- 交易订单状态不变。
- 新增的是后台子系统健康生命周期：failed -> recovered。

## Caching and Performance

- 移除 Postgres 归档热路径中的 10000 参数 `IN (...)` 查询。
- 避免在打开事务后由 Python 处理 10k 条大 JSON，降低 `idle_in_transaction_session_timeout` 风险。

## Logging, Monitoring, Auditing

- 保留 `background_loop_failed`。
- 新增 `background_loop_recovered`，用于审计后台异常是否已经恢复。

## Testing Strategy

- 单元测试覆盖 Postgres CTE SQL 分支的执行形态和恢复事件过滤。
- 现有 housekeeping SQLite 行为继续保留。
- 最窄集成测试覆盖 `/execution/errors` 对 recovered summary 的显示语义。

## Migration, Rollback, Compatibility

- 无 DB migration。
- 回滚方式：恢复 `housekeeping.py` 的归档实现、移除恢复事件和过滤逻辑。
- HTTP 返回结构兼容，前端无需同步变更。

## Configuration and Environment Isolation

- 不新增配置。
- Windows 本地测试用 `.venv\Scripts\python.exe`，WSL2 集成测试按仓库约束运行。

## Code Organization and Dependencies

- 只修改现有 Python 模块与相关测试。
- 不新增第三方依赖。

## Documentation and Operations Manual

- 本文档记录现场原因、修复边界和验收口径。
- 运行手册中的部署入口保持 `bash scripts/deploy.sh --skip-commit`。

## Deployment and Acceptance Criteria

- `db_housekeeping` 不再生成巨型 `event_store_archive.event_id IN (...)` 查询。
- `execution_sync` 因 DB housekeeping 连接冲击而报错的风险降低。
- 后台子系统成功恢复后，委托与成交页不再继续用旧失败判定“执行子系统存在异常”。
- lint、单元测试、最窄相关集成测试通过。
