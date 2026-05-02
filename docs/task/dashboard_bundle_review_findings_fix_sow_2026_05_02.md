# Dashboard Bundle Review Findings 修复 SOW（2026-05-02）

## Business objectives and boundaries

修复第二轮 dashboard 加载重构审查发现的问题，目标是在保持首屏轻量化的同时，避免阻断字段兼容退化、运行包健康状态误报，以及 RDP/AI deferred 数据被误显示为空。边界限定在 operator UI 读接口、runtime 摘要和前端渲染状态，不改变交易、下单、风控、RDP 审批业务语义。

## Module responsibilities and domain model

`aats/api/auth_routes.py` 负责 dashboard bundle panel 输出兼容；`aats/services/operator/runtime_queries.py` 负责运行时轻量运行包摘要；`aats/api/static/modules/views/*` 负责区分 deferred loading 与真实空数据；`tests` 覆盖这些 UI 契约。

## Input/output interfaces

`blockers` panel 继续保留旧 `/system/blockers` 字段，包括 `recommended_action` 与 `affects_account_synchronization`。`runtime.guarded_live_run_packet_summary` 在轻量路径里增加阻断感知字段，但不恢复完整 run packet 的重查询。RDP/AI 渲染输出只增加加载态文案，不改变 API 参数。

## Database schema / tables / indexes / constraints

不涉及数据库 schema、索引或数据迁移。

## Transactions, Consistency, Concurrency

所有改动为只读查询和前端渲染。轻量 runtime 复用已有 blocker control TTL 查询，不新增写入，也不改变事务边界。

## Authorization, Authentication, Data Security

不改变认证授权。不读取或打印凭证文件。

## Error Handling and Idempotency

deferred panel 仍保持 panel-level error 语义。加载态只在 panel pending 且数据缺失时显示，deferred 成功后自动由真实数据替换。

## State Transition and Lifecycle

不新增运行状态。仅把 execution blocker 对 runtime light summary 的状态判定纳入，避免未知阻断被判为 ready。

## Caching and Performance

不恢复完整 `guarded_live_run_packet`、archive summary 或 replay offset 的首屏重查询。新增 blocker control 依赖使用已有短 TTL/in-flight 路径。

## Logging, Monitoring, Auditing

不新增日志。现有 `dashboard_bundle_slow` 继续保留。

## Testing Strategy

补充 dashboard UI 测试覆盖 blockers 字段兼容、RDP pending 态、AI pending 态。补充 runtime 单测覆盖 execution blocker 导致轻量 summary 进入 critical。

## Migration, Rollback, Compatibility

无迁移。回滚本次文件即可恢复。

## Configuration and Environment Isolation

不新增配置。Windows 使用 `.venv\Scripts\python.exe`，WSL2 窄集成继续用 `~/aats-venv`。

## Code Organization and Dependencies

不新增依赖。改动保持在现有 dashboard API、runtime query facade、前端视图和测试文件。

## Documentation and Operations Manual

本文档记录 review findings 修复边界。若后续仍慢，继续以 panel timing 日志定位，而不是调整超时掩盖问题。

## Deployment and Acceptance Criteria

验收标准：

1. `blockers` panel 输出保留旧字段。
2. 轻量 run packet 遇执行阻断不会显示 ready。
3. RDP/AI deferred 明细在 pending 时显示加载态，不显示真实空状态。
4. lint、unit、dashboard UI、WSL2 窄集成测试通过。
