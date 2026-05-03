# Dashboard 参数化 Snapshot 与按需明细优化 SOW

## Business Objectives And Boundaries

目标是在不改变交易、风控、执行语义的前提下，继续降低 operator UI 刷新路径耗时：

- `recentDecisions`、`positionLifecycleAttribution` 支持按参数读取 snapshot，避免为了保持 limit 语义回退现算。
- `strategyAttribution`、`positionLifecycleAttribution`、`trialReviewSummary` 的常用报表组合由后台预热成物化 snapshot 变体。
- 深度明细改为抽屉按需加载，首屏只承载摘要。
- bundle panel 设置请求时预算，慢 panel 不再拖住整个 bundle。
- 分页/过滤只刷新受影响 panel，减少整页重拉。

本次不改数据库 schema，不新增交易状态，不改变下单、恢复、风控、RDP workflow 语义。

## Module Responsibilities And Domain Model

- `aats.services.operator.dashboard_snapshot`：支持 `panel_key + variant_key` 的 snapshot 存取、刷新、预热和 meta。
- `aats.api.auth_routes`：把 bundle 请求参数规范化为 snapshot variant；为 request-time panel 加预算。
- `aats/api/static/modules/store.js`：支持构造指定 panel 的 bundle URL，并维护 pageLimit 到 panel 的映射。
- `aats/api/static/modules/dashboard-refresh.js`：新增单 panel/少量 panel 定向刷新。
- `aats/api/static/modules/*drawer*.js` 与 `app.js`：新增报表明细抽屉入口。
- `tests/unit` / `tests/integration`：覆盖参数化 snapshot、预算、抽屉 action 和定向刷新 URL。

## Input / Output Interfaces

保持 `/dashboard/bundle` 外部结构不变：

- `panels[key].data`
- `panels[key].error`
- `panels[key].meta`
- `timing.panels[key].duration_ms`

snapshot meta 会新增兼容字段：

- `snapshot_key`
- `variant_key`
- `materialized`

前端新增本地 `data-action`：

- `inspect-decision-history`
- `inspect-strategy-attribution`
- `inspect-trial-review-details`

这些 action 只用于打开抽屉，不改变后端公开 API。

## Database Schema / Tables / Indexes / Constraints

不新增表、索引或 migration。报表物化先使用进程内 snapshot artifact，不落库。

## Transactions, Consistency, Concurrency

参数化 snapshot 沿用现有 singleflight 与 priority semaphore。`panel_key + variant_key` 独立刷新，互不覆盖。mutation 仍通过现有 invalidation 标记 snapshot stale 并后台刷新。

## Authorization, Authentication, Data Security

bundle 仍由 `require_read_access` 控制；抽屉明细使用既有 protected API，不扩大权限。不会读取或输出凭证。

## Error Handling And Idempotency

缺失或过期 snapshot 返回默认 payload + `meta.loading/refreshing` 并排队后台刷新。request-time panel 超预算时只让该 panel 返回错误，不拖垮整个 bundle。

## State Transition And Lifecycle

不改变交易状态、OrderState、恢复状态或审批状态。UI 刷新状态新增“定向 panel 刷新”，只影响 pending panels 和 shell render。

## Caching And Performance

常用报表变体在 snapshot scheduler 中预热：

- `recentDecisions(limit=8, offset=0)`
- `positionLifecycleAttribution(limit=6)` 与 `limit=8`
- `strategyAttribution(limit=200)`
- `trialReviewSummary(segment_limit=100, window_days=7, period_count=4)`

bundle 对 live fallback 设置 per-panel budget。分页和退出历史过滤只刷新相关 panel。

## Logging, Monitoring, Auditing

沿用 `dashboard_snapshot_refresh_*` 和 `dashboard_bundle_slow`。新增 timeout panel 会在 bundle timing 中体现，并以 panel error 暴露给前端。

## Testing Strategy

- 单元测试：snapshot variant 单独存取、singleflight、预热。
- 后端集成测试：不同 limit/view 使用不同 snapshot 变体；live panel timeout 不拖垮 bundle。
- 前端集成测试：指定 panel bundle URL、定向刷新方法导出、抽屉 action 注册。

## Migration, Rollback, Compatibility

无数据库迁移。旧 `read_panel("runtime")` / `seed_panel("runtime")` 调用继续工作。回滚可移除 variant 参数、前端 drawer action 和定向刷新。

## Configuration And Environment Isolation

不新增环境变量。Windows venv 与 WSL2 venv 使用同一代码路径验证。

## Code Organization And Dependencies

不新增外部依赖。新增前端 drawer helper 时复用现有 `components.js`、`formatters.js`。

## Documentation And Operations Manual

本 SOW 是本阶段操作记录；后续若要把 report artifact 落库，应另起数据库设计文档。

## Deployment And Acceptance Criteria

验收标准：

- 默认及常用参数下，慢报表从 snapshot variant 读取。
- strategy/execution 的 lifecycle limit 语义分别保持 6/8。
- 抽屉明细按需请求，不进入首屏 bundle。
- live panel 超预算时不拖住整个 bundle。
- 分页/过滤只请求受影响 panel。
- lint、unit、相关 integration 通过。
