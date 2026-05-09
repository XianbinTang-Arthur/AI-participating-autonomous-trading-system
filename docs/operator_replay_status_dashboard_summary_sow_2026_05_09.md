# Operator Replay Status Dashboard Summary SOW - 2026-05-09

## Business Objectives and Boundaries

继续压低 operator dashboard 自动刷新对重读路径的依赖。本轮目标是把 `replayStatus` 从完整 replay 诊断读模型拆出 dashboard summary，避免首屏/后台 snapshot refresh 频繁读取 archive summary 和版本补全路径。直接 `/replay/status` 保持完整诊断语义。

## Module Responsibilities and Domain Model

`AuditReplayQueryFacade` 提供 replay 相关读模型；`OperatorQueryService` 暴露 dashboard wrapper；`auth_routes` 的 dashboard panel loader 负责选择轻量读模型。Replay validation event 和 replay offset 仍是 source of truth。

## Input/Output Interfaces

dashboard `replayStatus` 保持原字段形状：`supported`、`healthy`、`last_validation`、`recent_validations`、`baseline_switches`、`event_store_archive`、`latest_replay_offset`。轻量模型新增 `dashboard_summary_only`、`truth_source`、`deferred_sections`，并把 `event_store_archive` 标成 deferred。完整 `/replay/status` 不变。

## Database Schema / Tables / Indexes / Constraints

不修改 DB schema、索引、migration。不改 replay 写入或 offset 持久化。

## Transactions, Consistency, Concurrency

不新增事务。dashboard summary 只读取最近 validation、最近 baseline switch 和最新 offset；不扫描 archive window/count。通过既有 `_cached_ttl` 做短期缓存，保持 idempotent。

## Authorization, Authentication, Data Security

不改变鉴权。dashboard panel 仍受 operator read 权限保护。不读取或输出凭证。

## Error Handling and Idempotency

沿用既有 dashboard snapshot 的 default/stale fallback。轻量 summary 缺少完整 archive 诊断时明确标注 deferred，避免误判为完整健康审计。

## State Transition and Lifecycle

不新增业务状态。Replay validation lifecycle 不变。

## Caching and Performance

dashboard summary 跳过 `event_store.archive_summary()`，也不对 validation rows 做 independent version 补全，避免每轮 dashboard refresh 触发额外 DB 聚合。保留最新 offset 读取用于观察 replay 进度。

## Logging, Monitoring, Auditing

不新增日志。验收通过 dashboard refresh 耗时、gateway 错误日志、Postgres active 长查询和容器健康判断。

## Testing Strategy

新增 unit tests 覆盖 dashboard summary 不调用 `archive_summary()`、不调用 independent version summary、字段标注 deferred；dashboard/snapshot loader 改用 `query.replay_status_dashboard()`；完整 `replay_status()` 保持原测试。

## Migration, Rollback, Compatibility

无 migration。回滚方式为 revert commit 后标准部署。dashboard 增加 metadata 字段兼容现有 UI；完整诊断接口兼容。

## Configuration and Environment Isolation

不新增配置。Windows 验证使用 `.venv\Scripts\python.exe`；WSL2 integration 使用 `~/aats-venv`；部署使用 `bash scripts/deploy.sh --profile derivatives-live --skip-commit`。

## Code Organization and Dependencies

只修改 operator read-side Python 模块和 tests，不新增依赖。

## Documentation and Operations Manual

本文记录本轮边界。后续若 `profileControlSummary` 仍是热点，再独立拆 summary snapshot 或缓存 tuning context。

## Deployment and Acceptance Criteria

验收标准：lint 通过；相关 unit/integration 通过；commit 完成；标准部署成功；`/healthz` 200；核心容器 healthy；Postgres 无超过 5 秒 active 查询；gateway 近窗口无 recurring `Traceback` / `ERROR` / `CRITICAL`。
