# RDP Dashboard Snapshot Control Summary Cache SOW

## Business objectives and boundaries

目标是降低 operator dashboard 中 RDP 相关 panel 的重复读取成本。当前 `rdpControl`、`rdpWorkbenchOverview`、`rdpWorkbenchItems`、`rdpWorkbenchAlerts` 在 snapshot plane 中相隔约 0.5-1 秒刷新，但都会构建同一份 RDP control summary。本次只优化 dashboard snapshot 只读路径，不改变普通 RDP API、审批、发布、回滚、交易决策或下单逻辑。

## Module responsibilities and domain model

`aats.api.auth_routes` 负责 dashboard snapshot plane 调度和构造 RDP fake request。`aats.api.rdp_control_summary` 负责读取 governance/RDP 状态并生成 control summary。新增缓存只用于 snapshot loader 生成的请求。

## Input/output interfaces

所有公开 API 返回 schema 保持不变。新增 request state marker 仅为内部实现细节：snapshot loader 请求允许使用 5 秒进程内 summary cache；普通 FastAPI 请求不使用该 cache。

## Database schema / tables / indexes / constraints

不修改数据库 schema、索引或约束。

## Transactions, Consistency, Concurrency

不新增事务。cache 使用进程内 lock 保护，返回值始终 deep copy，避免跨请求对象污染。dashboard snapshot 本身已经是 TTL/stale 语义，5 秒 summary cache 不改变交易或治理写入的一致性。

## Authorization, Authentication, Data Security

不改变鉴权。cache 中只保存已授权 snapshot loader 内部读取的 RDP summary，不包含凭证，不读取或输出 env secret。

## Error Handling and Idempotency

cache miss 或过期时走原完整构建路径。构建失败逻辑保持原样。读取幂等。

## State Transition and Lifecycle

不新增业务状态。dashboard snapshot 的 RDP panel 在同一刷新窗口复用同一 summary；普通 RDP API 每次仍重新读取。

## Caching and Performance

新增 5 秒进程内 cache，key 为 project root + runtime id。仅 `_dashboard_snapshot_rdp_request()` 标记的请求启用，目标是消除 RDP snapshot panel 同一轮刷新中的重复 DB/文件 IO，同时避免不同测试/runtime 实例之间串缓存。

## Logging, Monitoring, Auditing

不新增日志。验收通过 gateway dashboard snapshot 日志中的 RDP panel `duration_ms`、timeout/slow 日志、Postgres 长查询和容器健康判断。

## Testing Strategy

新增 unit test 覆盖 snapshot 请求跨请求复用、返回 deep copy、普通请求绕过 snapshot cache。运行 ruff、RDP control summary unit、dashboard snapshot/operator 相关测试、全量 unit、WSL2 窄集成测试。

## Migration, Rollback, Compatibility

无 migration。回滚方式是 revert 本次 commit 并按标准 deploy 脚本重新部署。公开 schema 保持兼容。

## Configuration and Environment Isolation

不新增配置。Windows 测试使用 `.venv\Scripts\python.exe`；WSL2 部署使用 `scripts/deploy.sh --profile derivatives-live --skip-commit`。

## Code Organization and Dependencies

仅修改 `aats.api.auth_routes` 和 `aats.api.rdp_control_summary`，使用标准库 `threading.Lock` 与 `time.monotonic`。

## Documentation and Operations Manual

本 SOW 作为操作记录。部署后检查容器健康、gateway `/healthz`、Postgres 长查询、gateway RDP panel 耗时。

## Deployment and Acceptance Criteria

验收标准：测试通过；commit 完成；部署成功；RDP snapshot panel 近期平均耗时下降，且核心服务无 `Traceback` / `ERROR` / `dashboard_snapshot_refresh_timeout`。
