# Recent Decisions Independent Diagnostics Reuse SOW - 2026-05-09

## Business objectives and boundaries

继续降低 operator dashboard `recentDecisions` 快照刷新延迟，但不关闭该面板的启动预热或定时刷新，避免复现“决策记录无数据/过旧”的可见性问题。边界限定在 dashboard recent decisions 读侧内部复用，不改变交易决策、风控、下单、审计写入、数据库 schema、公开 API 字段或前端展示语义。

## Module responsibilities and domain model

- `OperatorQueryService._build_recent_decisions()`: 构建策略页最近决策列表的 dashboard summary。
- `_independent_expected_vs_realized_summary()`: 计算 independent 策略的 expected vs realized 诊断摘要。
- `DecisionAuditRecord`: 仍只作为 refs 入口，本轮不新增 summary 持久化，不触碰 audit 写路径。

## Input/output interfaces

输入仍是 `/decision/recent` 和 `/dashboard/bundle` 的现有参数：`limit`、`offset`。输出 JSON schema 不变，`recentDecisions.decisions[].independent_expected_vs_realized_summary` 继续存在于 independent 行。

## Database schema / tables / indexes / constraints

无 schema、表、索引或约束变更。不新增 migration。

## Transactions, Consistency, Concurrency

不引入写事务。`_build_recent_decisions()` 已经按页批量解析 target refs；本轮只把同一页已解析的 independent target payloads 和页内相关 fill outcomes 传给诊断摘要，减少同一 request/snapshot refresh 内的重复 repo/payload/fill 扫描。线程安全仍由现有 request-local 对象和 query service cache 保障。

## Authorization, Authentication, Data Security

不修改 operator 鉴权。不得读取或输出任何 env secret。诊断摘要只使用已经允许展示在 operator dashboard 的决策和成交诊断字段。

## Error Handling and Idempotency

错误语义不变；若诊断摘要没有足够 metric flags，仍返回 `None`。改动是纯函数式复用，重复调用对外结果一致。

## State Transition and Lifecycle

无交易状态迁移。`recentDecisions` snapshot lifecycle 保持原策略：仍允许 startup prewarm 和 scheduler refresh，以保留最近决策可见性。

## Caching and Performance

优化点：

1. independent 行不再为了单行诊断重新通过 `audit_repo.get(decision_id)` 取 target ref。
2. independent 行优先使用本页已加载的 target payload。
3. fill outcomes 先按本页 decision ids 过滤一次，再传给每个单行诊断摘要，避免每行扫描全 scope outcomes。

## Logging, Monitoring, Auditing

沿用 `dashboard_snapshot_refresh_success/timeout/failed` 日志观察 `panel_key=recentDecisions` 延迟。验收重点是部署后不出现 timeout/failed，且 `recentDecisions` 平均耗时下降或至少不再扩大。

## Testing Strategy

- 单元测试确认 recent decisions dashboard 会把已解析 target payload 和页内 independent fill outcomes 传入 independent 诊断摘要。
- 保留已有 page-level refs batch read 测试。
- 运行 ruff、全量 unit、最窄 WSL2 operator API integration。

## Migration, Rollback, Compatibility

无 migration。回滚为恢复 `_independent_expected_vs_realized_summary()` 只自行加载 target/fill outcomes 的旧路径。响应字段兼容。

## Configuration and Environment Isolation

不新增配置项。Windows 本地测试使用 `.venv\\Scripts\\python.exe`；WSL2 integration 使用 `~/aats-venv`。

## Code Organization and Dependencies

不新增依赖。改动限定在 `aats/services/operator/query_service.py`、对应单元测试和本 SOW。

## Documentation and Operations Manual

本 SOW 记录本轮 read-side 优化边界。后续如果要进一步压缩 `recentDecisions`，应单独设计 `decision_summary_json` 持久化、migration 和回填方案，不混入本轮。

## Deployment and Acceptance Criteria

1. 相关测试通过。
2. 标准 deploy 成功，容器健康。
3. `/healthz` 正常。
4. 监控窗口内 `dashboard_snapshot_refresh_failed=0`，`recentDecisions` 没有新增 timeout。
