# Dashboard Snapshot Plane Phase 3 SOW（2026-05-02）

## Business objectives and boundaries

目标是把 risk/AI/RDP 页面中最重的 P2 报告面板移出 `/dashboard/bundle` 请求链路，避免单个 20-80 秒报告拖住页面进入可读状态。范围限定为 operator UI 读模型，不改变交易决策、风控、下单、恢复动作或 RDP 审批/发布语义。

## Module responsibilities and domain model

`DashboardSnapshotPlane` 继续按 panel key 后台生产快照；`auth_routes.py` 扩展 P2 loader；RDP summary 继续复用现有 builder，通过内部轻量 request 适配器提供 `app.state.runtime` 和 per-build `state` 缓存。

## Input/output interfaces

`/dashboard/bundle` query 参数不变，`panels[key].data/error` 保持兼容。P2 panel 和 P0/P1 一样额外返回 `panels[key].meta`，用于标识 stale/loading/refreshing/last_error。

## Database schema / tables / indexes / constraints

Phase 3 不新增数据库 schema。快照仍为 gateway 进程内存存储，后续再按方案书升级为 Redis/Postgres 组合。

## Transactions, Consistency, Concurrency

P2 面板使用 `priority=p2`，后台并发预算低于 P0/P1，避免重型报告挤压核心状态。请求线程只读快照并 enqueue，不等待 P2 loader 完成。

## Authorization, Authentication, Data Security

认证逻辑不变。未通过 read access 的 protected panel 不读取 snapshot。RDP/AI/risk 快照不读取或输出凭证。

## Error Handling and Idempotency

后台刷新失败保留旧快照；无旧快照时返回兼容空 payload 和 meta。重复 enqueue 对同一 panel 幂等。

## State Transition and Lifecycle

gateway 启动时预热 P0/P1/P2；P2 TTL/stale window 明显长于 P0/P1。mutation 成功后沿用 stale-while-refresh，保留旧 P2 快照可读并后台刷新。

## Caching and Performance

本阶段迁移 `trialGuard`、`guardedLivePreflight`、`guardedLiveRunPacket`、`replayStatus`、`aiOverview`、`aiLatest`、`aiShadowLatest`、`profileControlSummary`、`aiConfigModel`、`rdpControl`、`rdpWorkbenchOverview`、`rdpWorkbenchItems`、`rdpWorkbenchAlerts`、`rdpTuningOverview`、`rdpTuningProposals`。

## Logging, Monitoring, Auditing

沿用 snapshot structured logs，记录 panel key、priority、reason、duration、timeout 和错误类型，不记录敏感内容。

## Testing Strategy

扩展 registry unit tests 覆盖 P2；新增 dashboard bundle 回归测试，patch 掉 P2 heavy loader 后确认 P2 panel 从 fake snapshot plane 返回。

## Migration, Rollback, Compatibility

未安装 snapshot plane 的测试 app 继续走旧路径。生产 gateway 默认安装 P0/P1/P2 snapshot plane。回滚可从 registry 移除 P2 panel，旧 loader 路径仍保留。

## Configuration and Environment Isolation

不新增配置项。Windows 验证使用 `.venv\Scripts\python.exe`；窄集成按需在 WSL2 `~/aats-venv` 中执行。

## Code Organization and Dependencies

继续使用 `aats/services/operator/dashboard_snapshot.py`，不引入外部依赖。

## Documentation and Operations Manual

本 SOW 对应 `docs/design/dashboard_snapshot_plane_design_2026_05_02.md` 的 Phase 3。

## Deployment and Acceptance Criteria

验收标准：

1. P2 panel 注册到 snapshot plane，并按 `priority=p2` 低并发刷新。
2. `/dashboard/bundle` 在 snapshot plane 已安装时读取 P2 快照，不调用 request-time heavy loader。
3. RDP summary 快照复用现有 builder 且不依赖真实 HTTP Request。
4. lint、unit tests、dashboard 相关窄集成测试通过。
