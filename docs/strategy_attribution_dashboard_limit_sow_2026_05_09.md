# Strategy Attribution Dashboard Limit SOW - 2026-05-09

## Business objectives and boundaries

降低策略页按需打开时 `strategyAttribution` 重报表的后台刷新成本。当前 live 日志显示该 panel 单次刷新可接近 10 秒；它已经不参与启动预热和后台周期刷新，但策略页 deferred 打开仍会触发重计算。本轮只收紧 dashboard 常用摘要窗口，不改变交易决策、风控、下单、审计写入、数据库 schema 或完整报表接口能力。

## Module responsibilities and domain model

- `aats.api.auth_routes`: 负责 dashboard bundle panel 到 snapshot variant / request-time fallback 的映射。
- `aats/api/static/modules/store.js`: 负责前端 panel registry 的 direct fallback URL。
- `strategyAttribution`: 策略页摘要卡片使用的归因概览，适合有限窗口展示；完整历史分析继续通过报表接口参数显式请求。

## Input/output interfaces

Dashboard 常用 `strategyAttribution` variant 从 `limit=200` 调整为 `limit=100`。输出 schema 不变，仍返回 `summary`、`profitability_by_strategy_sleeve`、`sleeve_inventory_summary` 等现有字段。`/reports/strategy-attribution?limit=200` 仍可被显式访问。

## Database schema / tables / indexes / constraints

无 schema、表、索引或约束变更。

## Transactions, Consistency, Concurrency

不引入写事务。snapshot plane 仍保持单 panel singleflight 和现有 P3 concurrency。减少每次 dashboard 常用 variant 的读取/聚合窗口，降低线程池占用时间。

## Authorization, Authentication, Data Security

不修改 operator 鉴权；不读取或输出任何 env secret。该 panel 仍走现有 dashboard 权限边界。

## Error Handling and Idempotency

错误语义不变。若 snapshot 缺失，dashboard 仍按现有逻辑返回默认/loading 并异步刷新。重复刷新同一 `limit=100` variant 是幂等读。

## State Transition and Lifecycle

不改变交易或审计生命周期。P3 `strategyAttribution` 仍是按需面板，不参与 startup prewarm 或 scheduler refresh。

## Caching and Performance

目标是把策略页默认归因窗口从 200 条降到 100 条，减少按需刷新时的 aggregation 负担，同时保持 operator 首屏有足够近端样本用于快速判断。

## Logging, Monitoring, Auditing

沿用 `dashboard_snapshot_refresh_success/timeout/failed` 日志。部署后观察：

1. `panel_key=strategyAttribution` 无 timeout/failed。
2. 若用户打开策略页，`strategyAttribution` 刷新耗时低于之前约 9.5s 的单次峰值。
3. 其他 P0/P1/P2 panel 无新增失败。

## Testing Strategy

- 更新 dashboard bundle snapshot variant 测试，确保 strategy attribution 读取 `limit=100` snapshot。
- 更新 dashboard UI registry 测试，锁住前端 fallback URL。
- 运行 ruff、全量 unit、最窄 WSL2 operator/dashboard integration。

## Migration, Rollback, Compatibility

无 migration。回滚为恢复 dashboard 常用 variant 和前端 registry 到 `limit=200`。外部 API 参数兼容。

## Configuration and Environment Isolation

不新增配置。Windows 测试使用 `.venv\\Scripts\\python.exe`；WSL2 integration 使用 `~/aats-venv`。

## Code Organization and Dependencies

不新增依赖。改动限定在 `auth_routes.py`、`store.js`、相关测试和本 SOW。

## Documentation and Operations Manual

本 SOW 记录本轮 code review 后选定的低风险优化项。更深层的归因聚合重构应单独设计，不与本轮窗口收敛混合。

## Deployment and Acceptance Criteria

1. 测试通过。
2. 标准 deploy 成功，容器健康。
3. `/healthz` 正常。
4. 监控窗口内 dashboard snapshot timeout/failed 为 0。
