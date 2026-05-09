# AI Config Model Snapshot On-Demand SOW - 2026-05-09

## Business objectives and boundaries

降低 operator dashboard 后台固定读压力，避免 `aiConfigModel` 这类低频查看、偏重的 AI 配置摘要在启动预热和定时刷新中持续占用 gateway 读侧资源。边界限定在 dashboard snapshot policy 和对应守护测试，不改变交易决策、风控、下单、对账、数据库 schema 或 AI 配置真实接口语义。

## Module responsibilities and domain model

- `aats.services.operator.dashboard_snapshot`: 定义 dashboard panel 快照 TTL、软超时、优先级、启动预热和定时刷新策略。
- `aiConfigModel`: AI 配置页模型/运行参数摘要，属于 P2 operator 观察面板，不是交易执行链路的必要后台输入。
- 直接 AI 配置接口和 panel loader 继续负责构建真实数据；snapshot policy 只决定是否主动预热/定时刷新。

## Input/output interfaces

输入仍是现有 `/dashboard/bundle` 的 `panel=aiConfigModel` 或 AI 配置页触发的 panel 读取。输出 payload 结构不变。缺失快照时仍由 snapshot plane 返回默认数据并异步刷新，刷新完成后后续读取返回真实快照。

## Database schema / tables / indexes / constraints

无 schema、表、索引或约束变更。

## Transactions, consistency, concurrency

不引入写事务。并发语义继续由 `DashboardSnapshotPlane` 单 panel singleflight 和 P2 concurrency limit 控制。变化是 `aiConfigModel` 不再进入启动预热队列和 scheduler 队列，只有按需读取才触发刷新。

## Authorization, authentication, data security

不修改认证授权。AI 配置数据仍走现有 operator dashboard 认证边界。不得输出或记录密钥、token、环境变量内容。

## Error handling and idempotency

沿用 snapshot plane 现有 missing/loading/stale/error 语义。改动幂等，多次启动不会后台主动刷新 `aiConfigModel`；用户打开该 panel 时仍可触发一次 singleflight refresh。

## State transition and lifecycle

`aiConfigModel` 生命周期从 `startup + scheduler + on-demand` 调整为 `on-demand only`。该面板仍保留 P2 priority、TTL 和 hard expire，用于按需刷新后的缓存生命周期。

## Caching and performance

目标是减少 gateway 稳态后台刷新中的重型 AI 配置读取，特别是启动和 scheduler 与 P0/P1 面板争用读资源。`aiConfigModel` 软超时收紧到 10s，与其他重型 AI P2 按需面板一致。

## Logging, monitoring, auditing

沿用 `dashboard_snapshot_refresh_start/success/timeout` 日志。验收重点：

1. 部署后不再出现 `panel_key=aiConfigModel reason=startup`。
2. 部署后不再出现 `panel_key=aiConfigModel reason=scheduler`。
3. operator dashboard 健康检查和核心 P0/P1 面板保持正常刷新。

## Testing Strategy

- 单元测试锁定 `aiConfigModel` 属于重型 AI P2 按需面板，不启动预热、不定时刷新。
- 单元测试锁定重型 AI P2 按需面板 timeout 不超过 10s。
- 运行 ruff、全量 unit，以及最窄 operator API dashboard bundle integration。

## Migration, rollback, compatibility

无 migration。回滚为恢复 `aiConfigModel` 的默认 `startup_prewarm=True` 和 `scheduled_refresh=True`，以及 15s timeout。外部 API 兼容。

## Configuration and environment isolation

不新增配置项，不读取或修改环境变量。Windows 本地测试使用 `.venv\\Scripts\\python.exe`，WSL2 integration 使用 `~/aats-venv`。

## Code organization and dependencies

不新增依赖。改动限定在 `dashboard_snapshot.py`、对应单元测试和本 SOW。

## Documentation and operations manual

本 SOW 记录操作意图。后续运维可通过 gateway 日志中 `aiConfigModel` 的 refresh reason 验证该 panel 是否只在按需访问时刷新。

## Deployment and acceptance criteria

验收标准：

1. 相关测试通过。
2. 部署完成且 `/healthz` 正常。
3. 部署后监控窗口内 `aiConfigModel` 无 startup/scheduler refresh。
4. 不出现新的 dashboard snapshot timeout/error 聚集。
