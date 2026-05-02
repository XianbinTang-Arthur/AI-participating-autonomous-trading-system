# Dashboard Snapshot Plane Phase 1 SOW（2026-05-02）

## Business objectives and boundaries

目标是让 operator UI 的共享 P0 panel 不再在 `/dashboard/bundle` 请求链路中同步执行重型查询。边界限定在 gateway dashboard 读模型，不改变交易决策、风控、下单、恢复或 RDP 审批语义。

## Module responsibilities and domain model

新增 `DashboardSnapshotPlane` 负责后台刷新 panel 快照；`auth_routes.py` 负责把 P0 panel 从 bundle 请求路径切到快照读取；`apps/api_gateway/main.py` 负责在 FastAPI lifespan 中启动/停止 snapshot plane。

## Input/output interfaces

`/dashboard/bundle` 的 query 参数和 `panels[key].data/error` 结构保持兼容。P0 panel 额外返回 `panels[key].meta`，包含 `source=dashboard_snapshot`、`snapshot_age_ms`、`stale`、`loading`、`refreshing`、`last_error` 等字段。

## Database schema / tables / indexes / constraints

Phase 1 不新增数据库 schema。快照先使用 gateway 进程内存存储，验证请求链路合同；Postgres/Redis 持久化按方案书后续阶段落地。

## Transactions, Consistency, Concurrency

每个 panel 独立 singleflight，同一时刻只允许一个后台刷新任务。请求线程只读快照并在 missing/stale 时 enqueue，不等待 loader 完成。mutation 成功后把已有快照标记为 stale 并触发后台刷新，避免重型面板短暂退回空态。

## Authorization, Authentication, Data Security

认证逻辑不变。未通过 `require_read_access` 的 protected panel 不读取 snapshot，仍返回原有 auth error。Phase 1 快照只服务同一个 gateway 进程，不读取或输出凭证。

## Error Handling and Idempotency

后台刷新失败时保留旧快照；无旧快照时返回默认空 payload 和 `dashboard_snapshot_refresh_failed`。重复 enqueue 对 in-flight panel 幂等。

## State Transition and Lifecycle

gateway lifespan 启动时预热 P0 panel；停止时取消调度任务。UI 页面打开时如果 snapshot missing，页面进入可读状态并由后台刷新补齐；已有快照失效时继续展示旧数据和 stale meta。

## Caching and Performance

P0 panel 包括 `runtime`、`health`、`mode`、`systemRecovery`、`blockerControl`、`blockers`、`aiRuntime`、`metrics`、`accountState`。目标是 bundle request-time 读取这些 panel 时只做内存读，耗时毫秒级。

## Logging, Monitoring, Auditing

新增 snapshot refresh start/success/failed/timeout/stale 相关 structured logs，记录 panel key、duration、reason，不记录敏感信息。

## Testing Strategy

新增 unit tests 覆盖 snapshot plane missing/stale/singleflight 行为。新增 dashboard bundle 回归测试，patch 掉重型 loader 后确认 P0 panel 仍从 fake snapshot plane 返回。

## Migration, Rollback, Compatibility

未安装 snapshot plane 的测试 app 和本地临时 FastAPI app 继续走旧路径，降低一次性迁移风险。生产 gateway lifespan 默认安装 snapshot plane。回滚可删除 lifespan 安装和 auth_routes 中 snapshot 分支。

## Configuration and Environment Isolation

不新增配置项。Windows 验证使用 `.venv\Scripts\python.exe`；需要 WSL2 的集成验证按 CLAUDE.md 执行。

## Code Organization and Dependencies

新增 `aats/services/operator/dashboard_snapshot.py`，不引入外部依赖。

## Documentation and Operations Manual

本 SOW 对应 `docs/design/dashboard_snapshot_plane_design_2026_05_02.md` 的 Phase 1。后续应继续迁移 P1/P2 panel，并把 in-memory store 升级为 Redis/Postgres 组合。

## Deployment and Acceptance Criteria

验收标准：

1. gateway lifespan 启动 snapshot plane。
2. P0 panel 在已安装 snapshot plane 时不从 `/dashboard/bundle` 请求路径调用重型 loader。
3. mutation 成功后触发 snapshot refresh。
4. lint、unit tests、dashboard 相关窄集成测试通过。
