# WSL2 持续保活与预热说明

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../project_positioning.md)。

> 文档状态：现行操作说明
> 最后核对：2026-08-29（起始 HEAD `d34b01c38f31` + 持续采集周期保活工作区；以本文档所在最终 HEAD 为准）
> 核对范围：Windows 计划任务、WSL keepalive、Docker/Gateway 健康检查与标准 repair 入口；不证明当前数据新鲜、研究合格或 trading-ready

repair 只允许复用标准部署包装器，不能形成第二套部署入口；当前只允许
`spot`/`derivatives` 模拟栈，live profile 会在任何 repair 副作用前失败。

## 目的

Windows 重启后，AATS 跑在 Ubuntu WSL 内的独立 Docker 引擎上。单纯“预热一次然后退出”既不能
保证 WSL 持续运行，也不能发现登录后发生的采集进程故障。本机制分为三层：

1. 建立 Windows 侧长期存活的 WSL keepalive 进程；
2. Windows 登录时立即执行一次 WSL、Docker 和 AATS 栈预热；
3. 登录后每 5 分钟持续重复健康检查，部分故障按 30 分钟冷却复用标准 repair deploy。

周期任务成功只证明本轮进程健康检查通过，不证明 trades、BBO、books5、OI/funding 或 liquidation
数据无缺口。

## 组成

- `scripts/keepalive_wsl2_aats.ps1`
  - 启动、停止、检查低开销 WSL keepalive；
- `scripts/prewarm_wsl2_aats.ps1`
  - 登录后及每个周期启动或复用 keepalive；
  - 等待 Docker ready；
  - 检查 AATS 必需容器和 Gateway `/healthz`；
  - 部分故障且不在冷却期时触发一次标准 repair deploy；
  - 全部必需应用已协调停止时保持安全停止并要求人工 review；
- `scripts/register_wsl2_aats_startup_task.ps1`
  - 注册登录触发和无限期周期触发；
  - 使用 `MultipleInstances IgnoreNew` 防止同一任务重叠。

## 注册或升级持续任务

在 Windows PowerShell、项目根目录执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\register_wsl2_aats_startup_task.ps1 -Profile derivatives
```

默认使用原任务名 `AATS-WSL2-Prewarm-derivatives` 原位注册两个 trigger：

- Windows 登录后延迟 30 秒执行；
- 从注册后约 30 秒起，每 5 分钟无限期执行。

周期和 repair 冷却可以显式调整：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\register_wsl2_aats_startup_task.ps1 `
  -Profile derivatives -MonitorIntervalMinutes 5 -RepairCooldownMinutes 30
```

不要把 repair 冷却设为 0 用于长期周期任务；持久安全阻断会造成反复完整构建。

注册后只读核对 trigger、动作和最近结果：

```powershell
$task = Get-ScheduledTask -TaskName 'AATS-WSL2-Prewarm-derivatives'
$task.Triggers | Format-List CimClass,StartBoundary,Delay,Repetition
$task.Actions | Format-List Execute,Arguments
$task | Get-ScheduledTaskInfo | Format-List LastRunTime,LastTaskResult,NextRunTime
```

## 手工启动、查询或停止 WSL keepalive

```powershell
# 启动
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\keepalive_wsl2_aats.ps1 -Action Start -Profile derivatives

# 查询
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\keepalive_wsl2_aats.ps1 -Action Status -Profile derivatives

# 停止
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\keepalive_wsl2_aats.ps1 -Action Stop -Profile derivatives
```

## Dry-run

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\keepalive_wsl2_aats.ps1 -Action Start -Profile derivatives -DryRun
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\prewarm_wsl2_aats.ps1 -Profile derivatives -RepairCooldownSeconds 1800 -DryRun
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\register_wsl2_aats_startup_task.ps1 -Profile derivatives -DryRun
```

Dry-run 不注册任务、不启动进程、不触发部署。

## 自恢复策略

每个周期依次检查：

- WSL keepalive 是否存在；
- Docker 是否 ready；
- 必需容器是否全部 `running healthy`；
- Gateway `/healthz` 是否返回 `200`。

栈已健康时立即退出。若只是部分应用不健康，且不在 repair 冷却期，prewarm 通过标准包装器固定使用：

- `-SkipSync`；
- `-SkipCommit`；
- `-AssumeYes`。

这表示它只修复 WSL 当前 clean checkout，不会把 Windows 未提交改动带进部署。标准 deploy 自身继续
执行全局锁、七容器 quiescence、schema 和两次 NATS durable preflight。

如果全部必需应用都处于 `exited/dead`，prewarm 返回
`coordinated_application_stop_requires_operator_review`，不会自动启动。该状态可能来自标准部署的
NATS/schema/ownership 失败关闭或人工维护，只有真人完成证据审查后才能重新执行标准部署。

repair 尝试结果写入 `%LOCALAPPDATA%\AATS\startup-prewarm\repair-<profile>-<distro>.json`。
文件只含 profile、distro、时间、状态和退出码。默认计划任务在一次 repair 后等待 30 分钟再允许
下一次尝试；冷却不影响每 5 分钟的只读健康检查。

## 当前已知安全保持边界

2026-08-29 只读现场核验显示七个应用容器均已协调停止，PostgreSQL、Redis 与 NATS 仍健康。最近一次
标准 derivatives 流程的第一次 NATS preflight 发现一个未归属 durable
`aats-codex_manual_resume-system_operator_command_responses` 并按设计阻断；周期守护不得删除、忽略或
自动 ACK 该 consumer，也不得手工启动应用绕过。该事实会漂移，处理前必须重新只读核验。

## 故障排查

1. 查询 WSL keepalive：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\keepalive_wsl2_aats.ps1 -Action Status -Profile derivatives
```

2. 手工执行一次 prewarm；直接手工调用默认不启用 repair 冷却：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\prewarm_wsl2_aats.ps1 -Profile derivatives
```

3. 查询计划任务结果：

```powershell
Get-ScheduledTask -TaskName 'AATS-WSL2-Prewarm-derivatives' | Get-ScheduledTaskInfo
```

如果返回 `coordinated_application_stop_requires_operator_review`，不要反复手工运行 prewarm；应先查看
最近标准部署证据并完成阻断项的 owner/release review。

## 移除任务

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\register_wsl2_aats_startup_task.ps1 -Profile derivatives -Remove
```

移除计划任务不会停止已经存在的 WSL keepalive；需要时另行执行 keepalive `Stop`。

## 适用边界

- 适用于 Windows 登录后的持续 WSL 与模拟采集栈健康检查；
- 适用于部分故障经标准 deploy 自动 repair；
- 不替代正式 deploy，不自动同步 Windows 工作区代码；
- 不支持 live 自动预热/repair；显式 live profile 在任何 repair 副作用前失败；
- 不把容器健康等同于数据连续性，仍须检查 collector freshness、gap、drop 和 Silver eligibility；
- 不自动撤销全部应用的协调停止状态。
