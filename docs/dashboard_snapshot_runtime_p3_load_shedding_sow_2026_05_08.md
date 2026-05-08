# Dashboard Runtime And P3 Load Shedding SOW - 2026-05-08

## Business objectives and boundaries

目标是继续压低 operator dashboard 首屏 P0 快照刷新延迟，避免 `runtime` panel 因同步构建完整 guarded-live 预检和策略摘要而触发 2s 软超时；同时避免 P3 复盘/归因重报表在启动和定时调度中长期占用 snapshot loader。

边界：只调整 dashboard snapshot/read summary 路径。不改变交易决策、风控阈值、订单生成、对账恢复、执行适配器、数据库 schema 或完整详情端点语义。

## Module responsibilities and domain model

- `RuntimeQueryFacade.build_system_runtime(dashboard_summary_only=True)`: 负责首屏运行时摘要，不再同步构建完整策略运行摘要和完整 guarded-live 预检矩阵。
- `RuntimeQueryFacade`: 从 runtime 已有上下文组装 lightweight preflight 摘要，供 runtime panel 和 run-packet summary 使用。
- `DashboardSnapshotPolicy`: 描述 panel 的刷新策略，新增启动预热和定时刷新开关。
- P3 报表 panel: `strategyAttribution`、`positionLifecycleAttribution`、`trialReviewSummary` 保持按需读取，但不再参与启动预热和后台周期扫表。

## Input/output interfaces

输入仍是现有 dashboard panel keys 和 `/dashboard/bundle` 参数。输出保持同名 panel payload。

`runtime.strategy_runtime_summary` 在 dashboard summary 中改为显式 deferred payload；完整策略运行信息继续由 `strategyRuntime` panel 提供。`runtime.guarded_live_preflight` 保留 `status`、`launch_ready`、`summary`、`counts`、`checks`、`operator_actions` 等基础形状，但标记为 runtime-context lightweight summary，并声明完整预检矩阵已下钻到 `guardedLivePreflight` panel。

## Database schema / tables / indexes / constraints

无数据库变更、无 migration、无索引或约束变更。

## Transactions, consistency, concurrency

本次只减少只读 fan-out，不新增写事务。P3 panel 仍可通过 request-time snapshot read 触发后台 refresh，保持单飞和 semaphore 语义。

## Authorization, authentication, Data Security

不改变认证授权。不读取或输出 `.env`、密钥、token 或交易所凭据。

## Error Handling and Idempotency

Dashboard summary 仍是只读幂等路径。P3 缺失时继续返回默认 payload + loading meta，后台按需刷新；软 timeout 语义不变。

## State Transition and Lifecycle

不改变订单、持仓、恢复、阻断或 kill-switch 状态机。新增的 deferred 标记只影响 UI 首屏读取负载。

## Caching and Performance

- `runtime` dashboard 不再调用 `strategy_runtime_dashboard()`，避免 P0 与 P1 `strategyRuntime` panel 竞争同一策略摘要构建。
- `runtime` dashboard 不再调用 `guarded_live_preflight_dashboard()`，改用同一次 runtime build 已经取得的 recovery/account/margin/live-guard/trial-guard/submit-reason 上下文组装 lightweight summary。
- `_submit_blocked_reasons_dashboard()` 不再作为 runtime P0 的嵌套 parallel_fetch 子调用，改为在同一 fan-out 内读取 mode snapshot 和 execution readiness 后本地合并。
- P3 重报表不参与 startup prewarm 和 scheduler refresh，避免未查看的历史报表占用 global loader 并挤压 P0/P1。

## Logging, Monitoring, Auditing

继续依赖 `dashboard_snapshot_refresh_success/timeout` 和 `parallel_fetch_slow`。部署后重点观察 `panel_key=runtime` 是否仍出现 2s timeout，以及 P3 是否不再在启动阶段自动刷新。

## Testing Strategy

- 单元测试确认 `system_runtime_dashboard` 不调用 full strategy/preflight/blocker，也不调用 `strategy_runtime_dashboard()` / `guarded_live_preflight_dashboard()`。
- 单元测试确认 P3 policy 不参与 startup targets 和 scheduler missing refresh，但 read-missing 仍能按需 enqueue。
- 运行 ruff、全量 unit、最窄 WSL2 operator dashboard bundle integration。

## Migration, Rollback, Compatibility

无 migration。回滚为 revert 本提交即可恢复 P3 自动预热/定时刷新和 runtime dashboard 的旧 fan-out。

## Configuration and Environment Isolation

不新增环境变量。Windows 使用 `.venv\Scripts\python.exe`；WSL2 integration 使用 `~/aats-venv`。

## Code Organization and Dependencies

不新增依赖。改动限定在 operator runtime queries、dashboard snapshot policy/tests 和本 SOW。

## Documentation and Operations Manual

本文件记录本轮负载削峰边界。完整详情仍应通过对应 detail panel/endpoint 下钻，不应把历史复盘和完整预检矩阵重新放回 P0 runtime。

## Deployment and Acceptance Criteria

部署使用 `bash scripts/deploy.sh --profile derivatives-live --skip-commit`。验收：

1. 必需 derivatives-live 容器全部 running/healthy。
2. DB active long queries 维持 0 或可解释的短暂波动。
3. 监控窗口内 `runtime` P0 快照刷新不再因为 guarded-live preflight 或 strategy runtime summary 超过 2s。
4. P3 panel 不在启动 prewarm 中自动刷新；打开对应视图时仍能按需产生快照。
