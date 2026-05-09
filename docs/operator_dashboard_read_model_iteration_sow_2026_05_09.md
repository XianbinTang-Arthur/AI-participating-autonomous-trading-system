# Operator Dashboard Read Model Iteration SOW - 2026-05-09

## Business Objectives and Boundaries

继续完成 operator dashboard 读侧优化，目标是减少 P0/P1 首屏和自动刷新对重 DB/事件读取的依赖，并把重 P2 AI 面板从启动/周期刷新路径隔离出去。本轮只改 dashboard/API 读模型、snapshot policy 和测试守卫，不改变交易决策、风控阈值、下单、撤单、恢复写命令、审批发布或资金计算。

## Module Responsibilities and Domain Model

`aats.services.operator.query_service.OperatorQueryService` 负责 operator 读模型，新增 dashboard 专用 lightweight metrics。`aats.api.auth_routes` 负责 dashboard bundle 和 snapshot loader 的 panel 路由。`aats.services.operator.dashboard_snapshot` 负责 panel TTL、预热、周期刷新和优先级策略。

## Input/Output Interfaces

公开 `/system/metrics` 保持 full diagnostics 语义。dashboard bundle / snapshot 的 `metrics` panel 改为 dashboard summary 语义，字段保持兼容但未解析的重字段通过 `deferred_sections` 标注。AI P2 panel 继续保留 schema，但改为 on-demand snapshot refresh，首屏可返回默认 payload + loading meta。

## Database Schema / Tables / Indexes / Constraints

不修改数据库 schema、索引或约束。本轮不引入 `decision_summary_json` 落库，因为这会牵涉 audit schema、Postgres model、migration、write path 和回填策略。

## Transactions, Consistency, Concurrency

不新增事务。dashboard metrics summary 先复用 cached full metrics，未命中时只读取 runtime counters、phase1 shadow、最新 snapshot 和当前 open orders 这类轻量数据。AI P2 面板取消启动预热/周期刷新，降低与 P0/P1 的 loader 争用。

## Authorization, Authentication, Data Security

不改变鉴权。所有 dashboard protected panels 仍需既有 read access。无凭证读取或输出。

## Error Handling and Idempotency

读路径保持 best-effort。lightweight metrics 对未解析字段返回 `None` 并列入 `deferred_sections`，避免误导为完整诊断。snapshot plane 缺失 P2 数据时沿用默认 payload/loading meta。

## State Transition and Lifecycle

不新增业务状态。AI P2 面板生命周期从 startup/scheduled 改为 read-triggered on-demand refresh。直连 AI endpoints 不受影响。

## Caching and Performance

`metrics_dashboard()` 作为 dashboard read model 的第一步，避免 P0 metrics 每次冷缓存触发 `_build_metrics()` 的 13 路 parallel fetch。P2 AI panels 从 startup/scheduled 移出，避免 `aiLatest` / `aiOverview` 长查询在 gateway 启动期抢占资源。

## Logging, Monitoring, Auditing

不新增日志。验收通过 dashboard snapshot 日志、bundle timeout/slow 日志、Postgres 长查询、容器健康和 `/healthz` 判断。

## Testing Strategy

新增/更新 unit tests：dashboard metrics 不调用 full metrics；lightweight metrics 标注 deferred fields；AI P2 heavy policies are on-demand；dashboard SLO/policy guard 防止 P0/P1/P2 重面板回归。运行 ruff、全量 unit、WSL2 operator integration。

## Migration, Rollback, Compatibility

无 migration。回滚方式为 revert commit 后通过标准 deploy 脚本重新部署。公开 full metrics API 兼容；dashboard metrics 增加 `dashboard_summary_only` / `deferred_sections` metadata。

## Configuration and Environment Isolation

不新增配置。Windows 使用 `.venv\Scripts\python.exe`；WSL2 使用 `~/aats-venv`；部署使用 `bash scripts/deploy.sh --profile derivatives-live --skip-commit`。

## Code Organization and Dependencies

仅修改 operator read-side Python 模块和测试，不新增依赖。避免大规模抽象重构，先以清晰 dashboard read method 落地统一读模型的第一步。

## Documentation and Operations Manual

本 SOW 记录本轮边界。`decision_summary_json` 和 RDP aggregate loader 若后续要做，应单独出 migration/refactor SOW。

## Deployment and Acceptance Criteria

验收标准：lint/unit/integration 通过；commit 完成；标准部署成功；`/healthz` 200；核心容器 healthy；无活跃 Postgres 查询超过 5 秒；核心日志无 recurring `Traceback` / `ERROR` / `CRITICAL` / `dashboard_snapshot_refresh_timeout`。
