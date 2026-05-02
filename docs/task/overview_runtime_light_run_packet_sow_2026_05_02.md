# Overview runtime 轻量运行包重构 SOW

## Business objectives and boundaries

目标是修复 `overview` 首屏卡在骨架屏的问题。当前 `/dashboard/bundle` 的主请求包含 `runtime` 面板，而 `/system/runtime` 同步构建完整 `guarded_live_run_packet`，会进一步触发 `forward_validation_report` 等重型历史诊断。在实盘库事件量较大时，这条链路会超过前端 30 秒超时红线。

边界：不放宽前端超时，不降低 `/reports/guarded-live-run-packet` 的完整诊断语义，不改变交易、风控、下单、恢复状态的真实判定。

## Module responsibilities and domain model

`RuntimeQueryFacade.build_system_runtime()` 负责首屏运行时摘要，必须保持低延迟和可安全降级。`OperatorQueryService.guarded_live_run_packet()` 负责完整小资金运行包，包含前向验证、阻断、持仓、账户和资金费等完整证据。

重构后，`system/runtime` 只输出 `guarded_live_run_packet_summary`。如果完整运行包已有 TTL 缓存，则复用缓存摘要；如果没有缓存，则由当前 runtime 已经需要的轻量信号组合摘要。

## Input/output interfaces

`/system/runtime` 继续返回 `guarded_live_preflight`、`trial_guard`、`margin_buffer_overview`、`derivatives_live_guard` 和 `guarded_live_run_packet_summary`。

`guarded_live_run_packet_summary` 保留既有字段：`status`、`summary`、`summary_metrics`、`operator_actions`。新增元数据字段：`summary_source`、`full_packet_cached`、`deferred_sections`，用于说明摘要来自完整缓存还是轻量路径。

`/reports/guarded-live-run-packet` 输出保持完整运行包结构不变。

## Database schema / tables / indexes / constraints

本次不改表结构、不新增索引、不改约束。现有慢点来自在首屏主请求中同步扫描历史诊断链路，不是缺少单一索引可以完全修复的问题。

## Transactions, Consistency, Concurrency

轻量摘要只读取当前 runtime 已经并发获取的预检、守护、恢复和保证金信号，不新增写事务。完整运行包仍通过现有 TTL singleflight 保护。读取缓存时只在 `_cache_lock` 内检查有效 TTL，不等待、不触发重型 loader，避免首屏请求成为重型计算 follower。

## Authorization, Authentication, Data Security

不改变认证、会话、权限和 API key 兼容逻辑。不读取或输出任何环境凭证。新增字段只描述摘要来源和延迟加载段落，不包含密钥、token 或账户隐私明细。

## Error Handling and Idempotency

缓存命中失败、缓存不存在或缓存为负缓存错误时，`system/runtime` 走轻量摘要，不触发完整运行包。轻量摘要由已有状态字典容错读取，缺失数值以 `None` 表示，前端继续显示现有兜底文案。

## State Transition and Lifecycle

运行状态等级保持 `ready`、`warning`、`critical`。轻量路径将自动停机、试盘守护 breached 判为 `critical`；将预检失败/告警、only-reduce、保证金告警、恢复不安全判为 `warning`。完整运行包缓存存在时，直接沿用完整包判定。

## Caching and Performance

首屏主路径移除 `guarded_live_run_packet -> forward_validation_report` 的同步依赖。完整运行包的 35 秒 TTL 缓存继续服务专用报告路径和后续摘要复用。预期效果是 `overview` 主 bundle 不再被 `forward_validation` 的 90 秒级历史诊断拖住。

## Logging, Monitoring, Auditing

本次不新增日志噪声。现有 `parallel_fetch_slow` 仍可观察主请求耗时；若后续 `system_runtime` 仍慢，应继续从 remaining panel 查询定位，而不是放宽超时。

## Testing Strategy

新增单元测试覆盖两点：`build_system_runtime` 不再直接注册 `guarded_live_run_packet` 重型查询；无完整缓存时轻量摘要不会调用完整运行包，且自动停机信号仍生成 `critical`。保留受影响集成测试验证完整运行包和 `/system/runtime` 的结构仍兼容。

## Migration, Rollback, Compatibility

无数据迁移。回滚方式是恢复 `/system/runtime` 直接读取完整运行包，但这会重新引入首屏卡顿风险。前端兼容既有四个摘要字段，新增字段为向后兼容元数据。

## Configuration and Environment Isolation

不新增配置项。Windows 本地验证使用 `.venv\Scripts\python.exe`，受影响集成验证按仓库约束在 WSL2 运行。

## Code Organization and Dependencies

代码限定在 `aats/services/operator/runtime_queries.py`、`aats/services/operator/query_service.py` 和对应测试文件。不引入新依赖，不移动公开 API。

## Documentation and Operations Manual

本 SOW 是操作说明。后续排查类似首屏卡顿时，应先看 `dashboard/bundle` 主面板与 `parallel_fetch_slow` 的 top query，避免用前端骨架屏或超时参数掩盖后端慢查询。

## Deployment and Acceptance Criteria

验收标准：`system/runtime` 不再同步调用完整 `guarded_live_run_packet`；完整运行包报告端点语义不变；lint、单元测试、受影响集成测试通过或明确记录环境性失败。
