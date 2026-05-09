# Operator Read-Side Semantics / Cache Bounds SOW - 2026-05-08

## Business Objectives and Boundaries

继续降低 operator dashboard 读侧压力，同时修复上次优化留下的只读语义偏差。边界限定为 dashboard/API 读路径：不改变交易决策、风控阈值、下单、撤单、恢复命令、审批发布或资金计算。

## Module Responsibilities and Domain Model

`aats.api.auth_routes` 负责 dashboard snapshot panel 的轻量 payload 拼装。`aats.services.operator.query_service.OperatorQueryService` 负责 operator 读模型和 TTL cache。`aats.api.rdp_control_summary` 负责 RDP control summary 读取与 dashboard snapshot 复用。

## Input/Output Interfaces

公开 API schema 保持兼容。`health.execution_summary.deferred_sections` 必须准确描述当前响应中未解析的字段。直连 `/system/blocker-history` 保持 fresh 读取；dashboard blockers panel 使用专门的短 TTL 方法。RDP snapshot cache 只改变内部容量和过期管理，不改变返回字段。

## Database Schema / Tables / Indexes / Constraints

不修改数据库 schema、索引或约束。不新增 migration。

## Transactions, Consistency, Concurrency

不新增事务。`OperatorQueryService` 继续使用既有进程内 cache lock。RDP snapshot cache 在同一 lock 下清理过期项并限制容量，返回值保持 deep copy，避免跨请求对象污染。

## Authorization, Authentication, Data Security

不改变鉴权和权限边界。不读取、不输出任何 env secret、token 或密钥。缓存仅保存已授权读路径生成的非凭证状态摘要。

## Error Handling and Idempotency

cache miss、过期或直连路径均回落到原 loader。读操作保持幂等；异常传播语义不改变。

## State Transition and Lifecycle

不新增业务状态。dashboard blockers history 的短 TTL 只影响 dashboard snapshot 生命周期；直连历史查询继续代表当前数据库读。RDP snapshot cache 自动驱逐过期和最旧条目。

## Caching and Performance

修复 `health.execution_summary` 的 deferred metadata，使 `order_count=None` 不再被误报为完整字段。新增 `blocker_history_dashboard()` 作为 dashboard 专用 10 秒 TTL 入口。RDP snapshot summary cache 增加最大条目数和过期 sweep，防止测试或 runtime 替换时无限增长。

## Logging, Monitoring, Auditing

不新增日志。验收通过 dashboard panel duration、`/healthz`、Postgres 长查询、容器健康和 gateway/core logs 判断。

## Testing Strategy

补充 unit test 覆盖 cached metrics 路径下 `order_count` 仍被标记 deferred、直连 blocker history 不缓存、dashboard blocker history 缓存、RDP snapshot cache 容量驱逐。运行 ruff、全量 unit，以及受影响的 WSL2 operator integration。

## Migration, Rollback, Compatibility

无 migration。回滚方式为 revert commit 后通过标准 deploy 脚本重新部署。公开 API 字段保持兼容，仅修正 metadata 语义。

## Configuration and Environment Isolation

不新增配置。Windows 使用 `.venv\Scripts\python.exe` 验证；部署使用 `bash scripts/deploy.sh --profile derivatives-live --skip-commit`。

## Code Organization and Dependencies

仅修改 `aats.api.auth_routes`、`aats.services.operator.query_service`、`aats.api.rdp_control_summary` 及对应 unit tests。只使用标准库，不新增依赖。

## Documentation and Operations Manual

本 SOW 作为本次读侧优化记录。发布后检查 dashboard 关键 panel 耗时、核心容器健康、Postgres 长查询和错误日志。

## Deployment and Acceptance Criteria

验收标准：相关测试通过；全量 unit 通过；commit 完成；标准部署成功；`/healthz` 200；核心容器 healthy；无活跃 Postgres 查询超过 5 秒；核心日志无新增 recurring `Traceback` / `ERROR` / `dashboard_snapshot_refresh_timeout`。
