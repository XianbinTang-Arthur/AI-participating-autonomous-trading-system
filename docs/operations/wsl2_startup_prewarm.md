# WSL2 开机保活与预热说明

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。

> 文档状态：现行操作说明。最后核对：2026-08-22（代码基线 `be9179e`）。repair 只允许复用标准部署包装器，不能形成第二套部署入口。

## 目的

Windows 重启后，AATS 跑在 Ubuntu WSL 内的独立 Docker 引擎上。单纯“预热一次然后退出”并不能保证系统持续可用，因为 WSL 可能在预热脚本结束后重新回到 `Stopped`。本机制拆成两部分：

1. 先建立 Windows 侧长期存活的 WSL keepalive 进程
2. 再执行 WSL 唤醒、Docker 就绪检查和 AATS 栈预热 / repair

这样可以避免登录页偶尔能打开，但点击登录时后端又掉回冷启动窗口，最终只看到 `Failed to fetch`。

## 组成

- `scripts/keepalive_wsl2_aats.ps1`
  - 启动、停止、检查 WSL keepalive
- `scripts/prewarm_wsl2_aats.ps1`
  - 登录后确保 keepalive 存在
  - 等待 Docker ready
  - 检查 AATS 容器和 gateway `/healthz`
  - 必要时触发一次标准 repair deploy
- `scripts/register_wsl2_aats_startup_task.ps1`
  - 注册登录计划任务

## 注册登录任务

在项目根目录执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\register_wsl2_aats_startup_task.ps1 -Profile derivatives-live
```

默认会注册一个 `AtLogOn` 任务，在登录后延迟 30 秒执行 prewarm。prewarm 会自动先启动 keepalive。

## 手工启动 / 查询 / 停止 keepalive

启动：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\keepalive_wsl2_aats.ps1 -Action Start -Profile derivatives-live
```

查询状态：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\keepalive_wsl2_aats.ps1 -Action Status -Profile derivatives-live
```

停止：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\keepalive_wsl2_aats.ps1 -Action Stop -Profile derivatives-live
```

## Dry-run

keepalive：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\keepalive_wsl2_aats.ps1 -Action Start -Profile derivatives-live -DryRun
```

prewarm：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\prewarm_wsl2_aats.ps1 -Profile derivatives-live -DryRun
```

注册脚本：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\register_wsl2_aats_startup_task.ps1 -Profile derivatives-live -DryRun
```

## 自恢复策略

prewarm 只在以下条件不满足时触发一次 repair deploy：

- Docker 未 ready
- 必需容器未全部 `running healthy`
- gateway `/healthz` 未返回 `200`

repair deploy 严格复用标准入口包装器，并固定使用：

- `-SkipSync`
- `-SkipCommit`

这表示它只修复 WSL 当前 checkout，不会把 Windows 未提交改动带进部署。

## 登录页看到“登录接口不可达，请先确认服务已启动”时先查什么

1. 先查 keepalive 是否存在：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\keepalive_wsl2_aats.ps1 -Action Status -Profile derivatives-live
```

2. 再手工跑一次 prewarm：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\prewarm_wsl2_aats.ps1 -Profile derivatives-live
```

3. 如果仍失败，再检查计划任务最近执行结果：

```powershell
Get-ScheduledTask -TaskName "AATS-WSL2-Prewarm-derivatives-live" | Get-ScheduledTaskInfo
```

## 移除登录任务

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\register_wsl2_aats_startup_task.ps1 -Profile derivatives-live -Remove
```

## 适用边界

- 适用于 Windows 登录后自动恢复本地 WSL AATS 栈
- 不替代正式 deploy
- 不自动同步 Windows 工作区代码
