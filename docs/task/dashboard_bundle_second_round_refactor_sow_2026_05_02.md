# Dashboard Bundle 第二轮加载重构 SOW（2026-05-02）

## Business objectives and boundaries

目标是降低 `/ui/*` 各页面首屏卡在骨架屏的概率，尤其是交易总览、风险、RDP、AI 分析、执行、Replay 与退出任务工作台。边界限定在 operator dashboard 读接口与前端 bundle request plan，不改变交易决策、下单、风控、对账或 RDP 审批语义。

## Module responsibilities and domain model

`aats/api/auth_routes.py` 负责 `/dashboard/bundle` 聚合与 panel 调度；`aats/services/operator/runtime_queries.py` 负责 `/system/runtime` 的运行时摘要；`aats/api/static/modules/store.js` 负责各页面 primary/deferred panel 分层。第二轮只调整“首屏需要什么数据”，不改变各 panel 的业务模型。

## Input/output interfaces

`/dashboard/bundle` 的 query 参数与 panel 输出格式保持兼容。`/system/runtime` 保留 `event_store_archive` 与 `replay_offsets` 顶层字段，但首屏不再同步计算归档统计和 replay offset，改为返回 deferred sentinel，并指向 `/replay/status` 作为完整真相源。

## Database schema / tables / indexes / constraints

不涉及 schema、索引、约束或数据迁移。

## Transactions, Consistency, Concurrency

只读查询保持现有 best-effort 一致性。bundle 内对 `blockers` 复用 `blocker_control` 的计算结果来源，避免同一次 bundle 中重复构建阻断视图；现有短 TTL 和 in-flight 去重继续负责跨请求去重。

## Authorization, Authentication, Data Security

不改变 `require_read_access` / `require_admin_access`。缓存 key 继续包含 identity 与 role，避免跨用户泄漏。不得打印 `.env` 或密钥。

## Error Handling and Idempotency

保持 panel-level error 结构不变。deferred panel 失败只影响对应卡片，不阻塞 primary bundle。

## State Transition and Lifecycle

不新增运行状态，也不改变交易生命周期。前端 pending panel 状态仍由 `dashboard-refresh.js` 统一管理。

## Caching and Performance

首屏性能策略：

1. `/system/runtime` 移除同步 `event_store.archive_summary()` 与 `latest_replay_offset()`。
2. bundle 内 `blockers` 从 `blocker_control` 派生，避免重复查询阻断链路。
3. 执行、Replay、AI 分析、RDP、退出任务页面的长列表和次级明细进入 deferred bundle。
4. 为慢 bundle 输出结构化 timing 日志，便于下一轮定位真实瓶颈。

## Logging, Monitoring, Auditing

新增慢 bundle 日志事件 `dashboard_bundle_slow`，包含 view、panel 列表、各 panel 耗时、总耗时、cache/dedupe 状态。日志只记录性能元数据，不记录凭证。

## Testing Strategy

更新单元测试确认 runtime 不再注册 replay/archive 重查询。更新 dashboard UI 静态/集成测试确认 request plan 的 primary/deferred 分层和 blockers 派生路径。运行 ruff、unit tests，并在 WSL2 跑 dashboard bundle 相关窄集成测试。

## Migration, Rollback, Compatibility

无迁移。回滚只需恢复本次改动文件。接口字段保留，前端旧读取路径不会因字段缺失崩溃。

## Configuration and Environment Isolation

不新增配置。Windows 使用 `.venv\Scripts\python.exe` 验证；WSL2 窄集成使用 `~/aats-venv`。

## Code Organization and Dependencies

不新增依赖。改动集中在 dashboard API、runtime query facade、前端 store 与对应测试。

## Documentation and Operations Manual

本文档记录第二轮边界。运行后若仍慢，应优先看 `dashboard_bundle_slow` 的 panel timing，而不是调大超时。

## Deployment and Acceptance Criteria

验收标准：

1. 各 view 的 primary bundle 不再包含本轮明确迁出的长尾 panel。
2. `/system/runtime` 不再同步调用 event store archive/replay offset。
3. `blockers` panel 不再单独调用 `query.blockers()`。
4. 指定 lint、unit、窄集成测试通过。
