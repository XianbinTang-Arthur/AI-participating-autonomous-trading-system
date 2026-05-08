# systemRuntime dashboard summary read path SOW

## Business objectives and boundaries

目标是消除 dashboard `runtime` panel 的 P0 软超时。上一轮已经把 `strategyRuntime` panel 本身改成 summary 读路径，但 `runtime` panel 仍会在冷路径预热完整 `strategy_runtime(limit=5)`，导致每个完整 runtime TTL 周期仍可能触发 6s+ 刷新。

边界：只调整 dashboard bundle / snapshot plane 的 `runtime` panel。公开 `/system/runtime` 继续返回完整运行时诊断，不改变人工下钻语义。

## Module responsibilities and domain model

- `RuntimeQueryFacade`: 新增 dashboard summary 构建入口。
- `OperatorQueryService`: 提供 `system_runtime_dashboard()` TTL 缓存方法。
- `auth_routes`: dashboard panel 读取 `runtime` 时走 summary 入口。
- 前端继续接收同名 `runtime` panel，字段保持兼容。

## Input/output interfaces

输入仍是 dashboard bundle / snapshot plane 的 `runtime` panel key。输出保持 dict payload，不改 API envelope。

dashboard summary 保留首屏需要的运行态、symbols、auth、recovery、guarded live summary、trial/margin/live guard 等摘要字段；完整诊断继续从 `/system/runtime` 获取。

## Database schema / tables / indexes / constraints

不变更数据库 schema、索引或约束。

## Transactions, consistency, concurrency

summary 路径复用现有 `_cached_ttl` 与 dashboard 子路径，减少 full runtime / full strategy runtime 的并发冷建。无新增事务。

## Authorization, authentication, data security

不改变认证路径，不读取或输出 `.env` / secret。

## Error handling and idempotency

保持 dashboard snapshot plane 的现有 timeout / stale 语义。summary 路径是只读且幂等。

## State transition and lifecycle

不改变交易状态机、恢复状态、订单或持仓生命周期。

## Caching and performance

`system_runtime_dashboard()` 使用独立 TTL key。dashboard runtime 不再预热完整 `strategy_runtime`，改用 `strategy_runtime_dashboard`；恢复、preflight、blocker 也使用 dashboard summary 输入。

## Logging, monitoring, auditing

继续依赖 `dashboard_snapshot_refresh_success/timeout` 监控 runtime panel 时延。部署后用 gateway 日志确认 `panel_key=runtime` 不再出现 2s timeout。

## Testing strategy

- 单元测试确认 dashboard runtime 不调用 full `strategy_runtime` / `recovery_view` / `guarded_live_preflight` / `blocker_control`。
- dashboard bundle routing 测试确认 request fallback 和 snapshot loader 使用 `system_runtime_dashboard()`。
- 运行全量 unit 和最窄 WSL2 integration。

## Migration, rollback, compatibility

无 migration。回滚为恢复 dashboard `runtime` panel 调用 `query.system_runtime()`。

## Configuration and environment isolation

不新增配置项。Windows 测试使用 `.venv\Scripts\python.exe`，WSL2 integration 使用 `~/aats-venv`。

## Code organization and dependencies

不新增依赖。改动限定在 operator query facade、auth route、单元测试和本 SOW。

## Documentation and operations manual

本文件记录优化边界和验收口径。

## Deployment and acceptance criteria

部署使用 `bash scripts/deploy.sh --profile derivatives-live --skip-commit`。验收：

1. runtime panel 刷新在正常调度周期内低于 2s timeout。
2. `strategyRuntime` 继续保持上轮优化后的低延迟。
3. 必需应用容器全部 healthy。
