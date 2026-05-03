# Dashboard Heavy Panel Snapshot Follow-up SOW

## Business Objectives And Boundaries

目标是把用户刷新路径上仍然昂贵的 dashboard panel 从 `/dashboard/bundle` 请求时计算链路移出，避免策略页、执行页被历史决策和归因报表拖慢。范围仅限：

- `recentDecisions`
- `positionLifecycleAttribution`
- `strategyAttribution`
- `trialReviewSummary`
- `exitExecutionActionHistoryPage` bundle 映射

本次不重构交易、风控、执行语义，不修改数据库 schema，不改变下单/恢复状态机。

## Current Behavior

`/dashboard/bundle` 对已注册在 `DASHBOARD_SNAPSHOT_POLICIES` 的 panel 只读 snapshot；未注册 panel 会在请求线程中调用 `OperatorQueryService`。实测慢点集中在：

- `recentDecisions`：策略页 primary 中现算，可能超过 15 秒。
- `positionLifecycleAttribution`、`strategyAttribution`、`trialReviewSummary`：deferred 中现算，可能被前端 45 秒 deferred timeout abort。
- `exitExecutionActionHistoryPage`：前端会请求该 bundle panel，但后端没有映射，返回 `dashboard_bundle_panel_not_found`。

## Module Responsibilities And Domain Model

- `aats.services.operator.dashboard_snapshot`：注册慢 panel 的 snapshot policy。
- `aats.api.auth_routes`：后台 snapshot loader 生产慢 panel 快照；bundle 请求只读快照；补齐 exit history panel 映射。
- `aats/api/static/modules/store.js`：把退出任务历史分页/过滤参数写入 bundle URL，保证 bundle 映射能拿到与原独立 endpoint 一致的查询语义。
- `tests/integration`：覆盖慢 panel 不再在 bundle 请求时现算，覆盖 exit history bundle 映射和 URL 参数。

## Input / Output Interfaces

保留 `/dashboard/bundle` 结构：

- `panels[key].data`
- `panels[key].error`
- `panels[key].meta`
- `timing.panels[key].duration_ms`

新增 bundle query aliases：

- `exitExecutionHistoryLimit`
- `exitExecutionHistoryOffset`
- `exitExecutionHistoryAction`
- `exitExecutionHistoryParent`
- `exitExecutionHistoryActor`
- `exitExecutionHistoryWindowHours`

这些参数只影响 `exitExecutionActionHistoryPage`。

## Database Schema / Tables / Indexes / Constraints

不新增表、索引或 migration。所有数据仍从现有 repository/query service 读取。

## Transactions, Consistency, Concurrency

慢 panel 快照由现有 `DashboardSnapshotPlane` 后台 worker 生产，沿用 priority semaphore 与 singleflight。bundle 请求不直接执行慢 query。mutation 后仍由现有 middleware invalidate bundle cache 并 enqueue snapshot refresh。

## Authorization, Authentication, Data Security

所有 protected panel 继续受 `require_read_access` 保护；`operatorUsers` 仍需要 admin。不会读取或输出凭证。退出任务历史仍通过已有 query service 读取，不扩大权限边界。

## Error Handling And Idempotency

snapshot 缺失或过期时返回默认 payload + `meta.loading/refreshing`，并排队后台刷新。`exitExecutionHistoryAction` 非允许值时，该 panel 返回明确错误，不影响其他 panels。

## State Transition And Lifecycle

本次不改变交易状态、恢复状态、OrderState 或 RDP workflow 生命周期。只改变 dashboard 数据生产时机。

## Caching And Performance

默认首屏 `recentDecisions` 只读 snapshot。若用户把 `recentDecisions` limit 改大，当前 snapshot plane 尚不支持参数化 key，保守回退到现算以保持结果数量正确。后续可把 snapshot key 扩展为 `panel + normalized params`。

## Logging, Monitoring, Auditing

继续使用现有 `dashboard_snapshot_refresh_*` 与 `dashboard_bundle_slow` 日志。验收标准是目标 panel 不再出现在 request-time slow panel timings 中，除非用户主动加载非默认参数。

## Testing Strategy

- 扩展 snapshot plane 集成测试，patch 慢 query 方法，证明 bundle 请求只读 seed snapshot。
- 新增 exit history bundle 映射测试，证明参数透传到 `OperatorQueryService.exit_execution_action_history`。
- 新增前端 URL 构造测试，防止过滤/分页状态未进入 bundle URL。

## Migration, Rollback, Compatibility

无数据库迁移。回滚只需移除新增 policies、loader 分支和 exit history bundle 分支。默认 payload 形状保持前端兼容。

## Configuration And Environment Isolation

不新增环境变量。生产、测试、WSL2 使用相同代码路径。

## Code Organization And Dependencies

不新增依赖。沿用现有 FastAPI、frontend ESM、snapshot plane、OperatorQueryService。

## Documentation And Operations Manual

本 SOW 记录变更边界。若后续做抽屉页，应另写 UI SOW，避免和本次性能修复混在一起。

## Deployment And Acceptance Criteria

验收：

- 默认策略页 primary 不再 request-time 调用 `recent_decisions`。
- 归因/试盘慢 panel 不再 request-time 调用重报表。
- 风险页和退出任务工作台的 `exitExecutionActionHistoryPage` 不再返回 `panel_not_found`。
- 相关 tests 与 lint 通过。
