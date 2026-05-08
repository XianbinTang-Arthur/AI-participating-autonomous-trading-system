# RDP Workbench Overview Summary Read Optimization SOW

## Business objectives and boundaries

目标是降低 operator dashboard 中 `rdpWorkbenchOverview` 的刷新耗时，让 RDP 治理页首屏继续稳定显示当前任务、待审批数量、阻断状态和操作按钮。边界是只优化只读摘要路径，不改变 RDP 研究、审批、发布、回滚、交易决策或下单逻辑。

## Module responsibilities and domain model

`aats.api.rdp_control_summary` 负责把 RDP governance DB、运行时健康、研究快照、推荐项、发布记录和任务队列整理成 operator UI payload。`rdpWorkbenchOverview` 是摘要面板，只需要数量、状态、行动按钮和健康摘要；`rdpWorkbenchItems` 和 detail/evidence 端点才需要每个组合的完整证据链。

## Input/output interfaces

输入保持为 FastAPI `Request` 和现有 RDP 数据源。`build_rdp_workbench_overview()` 输出 schema 保持兼容：`overall_status`、`headline`、`subheadline`、`primary_action`、`secondary_actions`、`blockers`、`summary_counts`、`current_execution`、`next_queue`、`health`。

## Database schema / tables / indexes / constraints

不修改数据库 schema、索引或约束。本次只减少 overview 路径对 phase3/phase4 明细构建和 per-item evidence 的重复读取。

## Transactions, Consistency, Concurrency

不引入新事务。overview 继续使用当前请求内的 RDP control summary memoization。读取之间仍是最终一致的 dashboard snapshot 语义，不提升或降低治理动作的一致性要求。

## Authorization, Authentication, Data Security

不改变鉴权。`rdpWorkbenchOverview` 仍通过 dashboard bundle 和 `/rdp/workbench/overview` 的既有 read access 控制返回数据。不读取或输出任何 env secret。

## Error Handling and Idempotency

错误处理保持 fail-closed：RDP phase 或健康读取异常仍通过 alerts 阻断审批信号向 UI 展示。overview 不执行 mutation，天然幂等。

## State Transition and Lifecycle

不新增状态，不改变 workflow lifecycle。running/pending/latest task lane 仍来自 `build_rdp_control_summary()` 的任务摘要。

## Caching and Performance

核心优化有两部分：

1. 避免 overview 调用 `_build_workbench_items_payload()`，因为该函数会再次读取 phase3/phase4 并为每个 combo 生成 detail/evidence。overview 改为直接从 pending recommendation 和 alerts 计算 `pending_items` / `integrity_blocked_items`。
2. `_build_observation_queue()` 先筛选当前 active release，再读取 observation/effectiveness。非当前 active 的历史 release 不会进入 queue，提前跳过可以减少被丢弃 release 的文件/DB IO。

## Logging, Monitoring, Auditing

不新增日志字段。验收通过 gateway dashboard snapshot 日志中的 `panel_key=rdpWorkbenchOverview duration_ms=...` 和 timeout/slow 日志判断。

## Testing Strategy

新增/更新 unit test，覆盖 overview 不构造完整 items 明细、phase3/phase4 只读取一次、待处理数量和阻断数量不变，以及非当前 active release 不读取 observation/effectiveness。运行 ruff、相关 unit、相关 integration、全量 unit。

## Migration, Rollback, Compatibility

无 migration。回滚方式是 revert 本次 commit 并按标准 deploy 脚本重新部署。输出字段保持兼容。

## Configuration and Environment Isolation

不新增配置。Windows 本地测试使用 `.venv\Scripts\python.exe`；WSL2 部署使用项目标准 `scripts/deploy.sh --profile derivatives-live --skip-commit`。

## Code Organization and Dependencies

仅在 `aats.api.rdp_control_summary` 内新增小 helper，避免引入新依赖或跨模块抽象。

## Documentation and Operations Manual

本 SOW 作为操作记录。部署后检查容器健康、gateway `/healthz`、Postgres 长查询、gateway 慢面板日志。

## Deployment and Acceptance Criteria

验收标准：测试通过；commit 完成；部署到 derivatives-live 成功；`rdpWorkbenchOverview` 最近刷新耗时明显下降且无 `dashboard_snapshot_refresh_timeout` / `parallel_fetch_slow`；核心容器 healthy。
