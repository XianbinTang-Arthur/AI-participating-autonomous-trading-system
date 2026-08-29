# 持续采集周期保活加固任务书

> 文档状态：实施中任务书 / 历史证据
> 最后核对：2026-08-29（起始代码基线 `main@d34b01c38f31`）
> 核对范围：Windows 计划任务、WSL keepalive、标准 derivatives 模拟部署入口、采集容器只读现场状态
> 当前操作入口：[`../operations/wsl2_startup_prewarm.md`](../operations/wsl2_startup_prewarm.md)

## 业务目标与边界

- 把现有“登录时预热一次”升级为登录后持续周期检查，确保 derivatives 模拟栈中的
  `aats-rdp-daemon`、`aats-liquidations-daemon` 和
  `aats-microstructure-collector` 不会因单次进程/容器故障长期无人发现。
- 保留 Windows 侧 WSL keepalive，使 Ubuntu 与独立 Docker Engine 不因无前台客户端退出。
- 所有自动恢复继续只调用标准部署包装器；不得直接 `docker start/restart`、手工 Compose、
  rsync 或 live profile。
- 标准部署已协调停止全部应用时视为安全保持状态，周期检查不得擅自撤销。当前一个未归属
  NATS durable 仍需真人 owner/release review；本任务不删除、ACK、重建或忽略它。
- 不访问账户私有接口，不触发订单，不应用 recommendation/参数，不读取或输出凭证。

## 模块职责与领域模型

- `scripts/register_wsl2_aats_startup_task.ps1`：注册登录触发和无限期周期触发；同一任务忽略重叠实例。
- `scripts/prewarm_wsl2_aats.ps1`：确保 WSL keepalive、检查 Docker/必需容器/Gateway；识别全应用协调停止；
  对部分故障在冷却门允许时调用标准 repair deploy。
- `scripts/keepalive_wsl2_aats.ps1`：继续只负责低开销 WSL 常驻，不承担容器恢复。
- 本地 repair 状态：只保存 profile、distro、尝试时间、结果和退出码，不保存命令输出或连接信息。

## 输入与输出接口

- 注册脚本新增：
  - `-MonitorIntervalMinutes`：周期检查间隔；
  - `-RepairCooldownMinutes`：失败或刚完成 repair 后的最短重试间隔。
- prewarm 新增 `-RepairCooldownSeconds`，直接手工执行默认不启用冷却；计划任务显式传入。
- 输出继续使用 `[startup-task]`、`[startup-prewarm]` 和 `[wsl-keepalive]` 前缀；非零退出表示本轮未能证明健康。

## 数据库、表、索引与约束

- 不修改 PostgreSQL schema、表、索引或业务数据。
- 健康检查只读取 Docker/Gateway 状态；repair 的数据库行为仍完全由标准部署及其 schema 门管理。

## 事务、一致性与并发

- 计划任务使用 `MultipleInstances IgnoreNew`，避免同一周期任务重叠。
- repair 仍受 `/tmp/aats-standard-deploy.lock`、lease、active marker、七容器 quiescence 和两次 NATS
  preflight 约束；周期检查不创建第二把锁。
- 全部必需应用同时处于 `exited/dead` 时，不自动 repair，避免把标准部署失败关闭、人工维护或
  未决 release review 误判为普通进程故障。

## 鉴权、认证与数据安全

- 计划任务以当前 Windows 用户运行，不提升权限。
- 不读取 `.env.*` 内容；API 端口沿用现有非敏感 profile 字段读取逻辑。
- repair 状态文件不得包含密码、token、数据库 URL、NATS inbox 或完整异常输出。

## 错误处理与幂等

- 已有计划任务由注册脚本原名替换，重复注册幂等。
- 周期触发无结束时长；系统错过触发后由 `StartWhenAvailable` 补跑。
- Docker/WSL 短暂未就绪继续按现有超时处理。
- repair 尝试前持久化尝试时间；冷却期内只报告失败，不重复部署。
- 标准 deploy 返回非零时保留失败状态，禁止改报健康。

## 状态迁移与生命周期

1. 登录触发先执行一次 prewarm；独立周期触发随后按固定间隔持续运行。
2. prewarm 启动或复用 WSL keepalive，再等待 Docker。
3. 栈健康时直接成功退出。
4. 全应用协调停止时进入 `operator_review_required`，不自动启动。
5. 部分不健康且不在冷却期时，调用标准 repair deploy；成功后再次核验完整健康。
6. repair 失败进入 cooldown，后续周期仍做只读检查，冷却结束后才可再次尝试。

## 缓存与性能

- 默认每 5 分钟执行一次短健康检查；健康时只调用少量 `docker inspect` 与一次 loopback `/healthz`。
- 默认 repair 冷却 30 分钟，避免持久阻断导致构建风暴。
- 不新增常驻 Python 服务、数据库轮询或高频网络采集。

## 日志、监控与审计

- 计划任务本身保留最近运行结果；本地 repair 状态记录最后一次尝试/完成时间和退出码。
- Docker restart count、collector heartbeat、Silver freshness 与治理告警仍由现有运行证据负责；
  周期任务成功不等于数据完整、研究合格或 trading-ready。

## 测试策略

- 静态单元测试验证双触发、无限期 repetition、忽略重叠、冷却参数和协调停止门。
- PowerShell dry-run 验证不注册任务、不触发部署。
- 运行 ruff 与完整 unit suite。
- 窄集成仅在 WSL2 中执行不触发真实订单的部署/启动脚本测试；现场标准 derivatives deploy 仍必须通过
  NATS preflight，不能因本任务绕过。

## 迁移、回滚与兼容性

- 复用既有任务名 `AATS-WSL2-Prewarm-<profile>`，注册一次即可原位升级。
- 旧 `-Profile/-TaskName/-DelaySeconds/-Remove/-DryRun` 参数保持兼容。
- 回滚可用 `-Remove` 删除任务；WSL keepalive 可独立 `Stop`，不会删除数据库或 Docker volume。

## 配置与环境隔离

- 默认只允许 `spot` 与 `derivatives`；所有 live profile 在任何 repair 副作用前失败。
- 默认 distro 为 `Ubuntu`；不改变 `.env.wsl2` 或 profile env 的位置与内容。
- Windows 脚本运行于 Windows；容器与部署命令继续通过受管 WSL2 入口执行。

## 代码组织与依赖

- 只修改 `scripts/` 下现有 PowerShell 运维入口、对应单元测试和文档。
- 不新增第三方依赖，不改变交易、策略、风控、订单或参数代码。

## 文档与运行手册

- 更新现行 [`../operations/wsl2_startup_prewarm.md`](../operations/wsl2_startup_prewarm.md)，说明周期、冷却、
  协调停止、查询与移除方式。
- 在本目录索引登记本任务；任务书不替代现行运行手册和现场只读验证。

## 部署与验收标准

- 注册脚本 dry-run 明确显示登录触发、周期分钟数和 repair 冷却。
- 导出的任务定义同时包含登录触发与无结束时长的周期触发，且 `MultipleInstances=IgnoreNew`。
- 健康栈不触发 deploy；部分故障只通过标准包装器 repair；全应用协调停止不自动恢复。
- lint、完整单元测试和最窄 WSL2 集成通过后本地提交。
- 当前 NATS 未归属 durable 未经真人处置前，现场应用恢复保持阻断；不能把任务已注册写成采集已恢复。
