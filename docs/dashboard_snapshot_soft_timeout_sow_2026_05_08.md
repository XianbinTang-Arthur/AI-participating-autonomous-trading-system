# Dashboard Snapshot 软超时语义修复 SOW - 2026-05-08

## Business objectives and boundaries

目标是避免操作台后台快照刷新超过软预算时，把仍在运行中的读取误报为面板失败。边界限定在 dashboard snapshot read 语义和单元测试，不改变交易决策、下单、风控、数据库 schema 或后台 loader 的实际执行。

## Module responsibilities and domain model

`DashboardSnapshotPlane` 负责后台生成 dashboard panel 快照。`timeout_seconds` 是软预算，用于监控和防止重复刷新，不代表 loader 已经失败；真实失败应来自 loader 最终抛出的异常。

## Input/output interfaces

输入仍是 dashboard bundle 对 `read_panel(panel_key)` 的读取。输出保持 `DashboardSnapshotRead` 结构不变。变化是：当 timeout 只是仍在刷新的软超时，`meta.last_error` 可以记录该 timeout，但 `error` 不再返回 `dashboard_snapshot_refresh_failed`，`meta.status` 仍按 missing/loading 表达。

## Database schema / tables / indexes / constraints

不涉及数据库结构。

## Transactions, Consistency, Concurrency

不改变 singleflight 行为。超时后仍等待原 loader 完成，避免并发启动重复 DB 查询。锁和 inflight 生命周期保持不变。

## Authorization, Authentication, Data Security

不涉及认证或敏感信息。不会读取或输出任何密钥、密码或 token。

## Error Handling and Idempotency

软超时只表示刷新慢，不表示失败。loader 最终异常仍记录为失败并返回 panel error。loader 最终成功仍清理历史错误并写入新快照。

## State Transition and Lifecycle

missing snapshot + refreshing + soft timeout 的状态从“error”调整为“missing/loading”。这样 UI 会继续呈现加载语义，而不是把临时慢刷新显示成失败或空数据。

## Caching and Performance

不改变 TTL、stale-after、hard-expire、调度频率或并发。性能影响为零；目标是修正读侧状态语义。

## Logging, Monitoring, Auditing

保留 `dashboard_snapshot_refresh_timeout` warning 和 `meta.last_error`，方便运维继续看到慢 panel。只是避免该软超时直接升级成用户可见失败。

## Testing Strategy

更新 `test_refresh_timeout_keeps_singleflight_until_loader_settles`，断言软超时期间仍 singleflight、仍 loading/refreshing、不会返回 panel error，loader 成功后快照正常落地。

## Migration, Rollback, Compatibility

无需迁移。接口字段兼容，前端无需改动。回滚只需恢复 `read_panel` 对 timeout last_error 的原始 error 映射。

## Configuration and Environment Isolation

不新增配置，适用于 Windows 测试和 WSL2 实盘部署。

## Code Organization and Dependencies

只改 `aats/services/operator/dashboard_snapshot.py` 和对应单元测试，不新增依赖。

## Documentation and Operations Manual

本 SOW 记录语义变化。操作侧看到 snapshot timeout warning 时，应理解为后台 panel 慢刷新，不等同于面板读取失败。

## Deployment and Acceptance Criteria

验收标准：

1. 软超时期间 `read_panel` 不返回 `dashboard_snapshot_refresh_failed`。
2. `meta.last_error` 仍包含 timeout 证据，`refreshing/loading` 保持 true。
3. loader 成功后 snapshot 正常更新并清理错误。
4. 单元测试、ruff、受影响集成测试通过并部署成功。
