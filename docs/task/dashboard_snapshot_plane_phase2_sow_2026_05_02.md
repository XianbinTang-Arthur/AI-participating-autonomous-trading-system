# Dashboard Snapshot Plane Phase 2 SOW（2026-05-02）

## Business objectives and boundaries

目标是在 Phase 1 的 P0 快照基础上，把 home/overview/strategy/execution/risk/replay 高频页面的 P1 面板移出 `/dashboard/bundle` 请求链路。范围限定为 operator UI 读模型，不改变交易决策、风控、下单、恢复动作或 RDP 审批语义。

## Module responsibilities and domain model

`DashboardSnapshotPlane` 继续负责后台刷新 panel 快照；`auth_routes.py` 扩展 P1 panel loader；`/dashboard/bundle` 对已注册快照面板只做快照读取和轻量 view-specific 形状处理。

## Input/output interfaces

`/dashboard/bundle` query 参数不变，`panels[key].data/error` 保持兼容。P1 panel 和 P0 一样额外返回 `panels[key].meta`，前端可通过 `source=dashboard_snapshot`、`stale`、`loading`、`refreshing` 判断后台刷新状态。

## Database schema / tables / indexes / constraints

Phase 2 不新增数据库 schema。快照继续使用 gateway 进程内存存储，Postgres/Redis 持久化仍按方案书后续阶段处理。

## Transactions, Consistency, Concurrency

每个 P1 panel 独立 singleflight。请求线程不等待 P1 loader 完成；missing/stale 只 enqueue 后台刷新。mutation 成功后沿用 Phase 1 的 stale-while-refresh 失效策略。

## Authorization, Authentication, Data Security

认证逻辑不变。未通过 read access 的 protected panel 不读取 snapshot。P1 面板不引入用户私有写数据，不读取或输出凭证。

## Error Handling and Idempotency

后台刷新失败保留旧快照；无旧快照时返回兼容空 payload 和快照 meta。重复 enqueue 不产生重复 worker。

## State Transition and Lifecycle

gateway 启动时预热 P0/P1 面板。P1 TTL 长于 P0，避免重型读模型挤压核心状态刷新。

## Caching and Performance

本阶段迁移 `latestDecision`、`strategyRuntime`、`executionLatest`、`portfolio`、`positions`、`reconciliationLatest`。目标是这些面板在 bundle 请求时只做内存读，`latestDecision` 慢查询不再阻塞 execution 页面。

## Logging, Monitoring, Auditing

沿用 Phase 1 structured logs，记录 panel key、priority、reason、duration、timeout 和错误类型，不记录敏感内容。

## Testing Strategy

扩展 snapshot plane unit tests 覆盖 P1 registry；新增 dashboard bundle 回归测试，patch 掉 P1 loader 后确认 P1 panel 从 fake snapshot plane 返回。保留未安装 snapshot plane 的旧测试路径。

## Migration, Rollback, Compatibility

未安装 snapshot plane 的测试 app 继续走旧路径。生产 gateway 默认安装 P0/P1 snapshot plane。回滚可从 registry 中移除 P1 panel，旧 loader 路径仍保留。

## Configuration and Environment Isolation

不新增配置项。Windows 验证使用 `.venv\Scripts\python.exe`；窄集成按需在 WSL2 `~/aats-venv` 中执行。

## Code Organization and Dependencies

继续使用 `aats/services/operator/dashboard_snapshot.py`，不引入外部依赖。

## Documentation and Operations Manual

本 SOW 对应 `docs/design/dashboard_snapshot_plane_design_2026_05_02.md` 的 Phase 2。

## Deployment and Acceptance Criteria

验收标准：

1. P1 panel 注册到 snapshot plane，并在 gateway lifespan 预热。
2. `/dashboard/bundle` 在 snapshot plane 已安装时读取 P1 快照，不调用 request-time heavy loader。
3. `strategyRuntime` 在 strategy view 继续保留既有裁剪形状。
4. lint、unit tests、dashboard 相关窄集成测试通过。
