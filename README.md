# AIParticipatingAutonomousTradingSystem

> AATS 是一个面向加密资产交易的事件驱动、可审计、受保护、可恢复的自动交易系统原型。

## 项目定位

这个仓库当前包含两条主线：

- **主交易系统**：行情接入、特征计算、决策、治理、执行、持仓、对账、恢复、Operator 控制面。
- **研究数据平台 RDP**：历史数据采集、回放、参数研究、归因、治理与参数推送。

它的目标不是“快速做出一个会下单的策略脚本”，而是优先解决下面这些工程问题：

- 决策链和执行链可追踪、可审计。
- 订单生命周期可恢复、可对账、可人工介入。
- live 模式具备 fail-closed 风控和显式 Operator 控制。
- 研究链路与运行链路可以通过配置和参数集对接。

这不是一个可以直接放心托管真实资金的“免维护盈利框架”。当前仓库更强调保护性、可观测性和工程闭环。

## 当前能力边界

### 支持的启动 profile

| profile | 环境文件 | 用途 |
| --- | --- | --- |
| `spot` | `.env.spot` | 现货开发 / 模拟盘 |
| `derivatives` | `.env.derivatives` | 合约开发 / 模拟盘 |
| `spot_live` | `.env.spot.live` | 现货受保护 live |
| `derivatives_live` | `.env.derivatives.live` | 合约受保护 live |

### 当前支持

- FastAPI API gateway + 内置前端控制台
- 单进程 monolith 运行
- 按 process role 切片的多进程运行（受限，见下方说明）
- 内存事件总线、Hybrid/NATS 事件总线
- memory / Redis hot state
- OKX 行情、账户快照、模拟盘 / 受保护 live 运行
- RDP 日常采集和多阶段研究编排

> **多进程拓扑限制：** `derivatives_live` 默认使用 hedge 持仓模式，
> 该模式要求 decision 与 execution slice 共处同一进程
> （`risk_engine.evaluate_leg_order` 尚无跨进程传递链路）。
> 因此 `derivatives_live` 当前仅支持 **monolith 单进程**部署；
> 4 进程拓扑适用于 `derivatives`（模拟盘，net 模式）和 `spot` 系列。
> 跨进程 leg risk 广播完成后（Stage 4）此限制解除。

### 当前不支持

- 无保护的真实资金自动交易
- 绕过治理 / 恢复 / Operator 的直接 live submit
- 把 README 当成完整生产运维手册

## 核心架构

### 主交易链路

```text
Market Gateway
  -> Feature Engine
  -> Decision Engine / AI Service
  -> Governance / Risk / Runtime Mode
  -> Strategy Coordinator
  -> Execution Planner / Order Manager
  -> Portfolio / Reconciliation / Recovery
  -> Operator API / UI / Audit
```

### 运行时形态

- **Monolith**：本地开发默认路径，依赖最少。
- **Gateway + Slice**：通过 `AATS_PROCESS_ROLE` 把 gateway / decision / execution 等职责拆开。
- **Event Bus**：
  - 默认可用 `InMemoryEventBus`
  - 可选 `HybridEventBus` / `NatsEventBus`
- **Hot State**：
  - 默认 memory backend
  - 多进程下可切 Redis backend

### RDP

RDP 负责历史数据与参数治理，核心流程是：

```text
数据采集 -> Replay / 研究 -> 归因 / 执行可行性 -> 治理 -> 决策输出 -> 参数集落地
```

## 仓库结构

```text
aats/
  api/                 FastAPI 路由、认证、前端静态资源服务
  bootstrap/           配置装配、runtime 构建、profile/env 加载
  bus/                 InMemory / Hybrid / NATS 事件总线
  schemas/             领域模型
  services/            市场、特征、决策、治理、执行、对账、Operator、RDP 等核心服务
  storage/             EventStore、Postgres 仓库、hot state、缓存
  data_platform/       RDP 研究平台
apps/
  api_gateway/         FastAPI 入口
scripts/
  start_api.py         API / UI 启动入口
  run_local.py         本地 in-memory paper loop
  rdp_*.py             RDP 采集、研究、治理脚本
configs/
  strategy_profiles/   策略 profile 配置
  active_parameter_sets/ RDP 活跃参数集
docs/
  operations/          运维 runbook
  rdp/                 RDP 说明文档
  configuration/       配置参考
```

## 快速开始

### 1. 环境准备

- Python `>= 3.11`
- PostgreSQL
- Windows PowerShell 或类 Unix Shell
- 如需多进程 / JetStream / Redis / OTel，再安装对应 optional extras

### 2. 安装依赖

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e .
```

如果需要额外能力：

```powershell
pip install -e .[nats]
pip install -e .[redis]
pip install -e .[otel]
```

### 3. 选择 profile

启动脚本会按 profile 读取对应的 `.env.*` 文件：

- `.env.spot`
- `.env.derivatives`
- `.env.spot.live`
- `.env.derivatives.live`

建议先从非 live profile 开始联调。

> **RDP 配置：** `.env.research` 供 Research Data Platform 子系统使用，
> 通过独立的配置路径加载，不属于上述 `--profile` 机制。

### 4. 启动 API 和前端

```powershell
.\.venv\Scripts\python.exe scripts\start_api.py --profile derivatives
```

常用可选参数：

```powershell
.\.venv\Scripts\python.exe scripts\start_api.py --profile derivatives --host 127.0.0.1 --port 8011
```

启动后可访问：

- UI: `http://127.0.0.1:8011/ui`
- API liveness: `http://127.0.0.1:8011/healthz`

### 5. 本地单进程 paper loop

```powershell
.\.venv\Scripts\python.exe scripts\run_local.py --profile derivatives --iterations 100
```

`run_local.py` 只支持 `spot` 和 `derivatives`，不用于 live profile。

## RDP 快速入口

### 日批采集

```powershell
.\.venv\Scripts\python.exe scripts\rdp_run_daily_ingest.py --ensure-schema
```

### 研究全流程编排

```powershell
.\.venv\Scripts\python.exe scripts\rdp_run_full_pipeline.py --start 2026-03-31 --end 2026-04-02 --ensure-schema
```

如果只想先看执行计划：

```powershell
.\.venv\Scripts\python.exe scripts\rdp_run_full_pipeline.py --start 2026-03-31 --end 2026-04-02 --dry-run
```

## 测试与质量检查

常用命令：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit -q
.\.venv\Scripts\python.exe -m pytest tests\integration\test_dashboard_ui.py -q
.\.venv\Scripts\python.exe -m ruff check .
```

如果要针对某个脚本确认参数，直接用 `--help`：

```powershell
.\.venv\Scripts\python.exe scripts\start_api.py --help
.\.venv\Scripts\python.exe scripts\rdp_run_full_pipeline.py --help
```

## 关键文档索引

- 生产发布全流程：[DEPLOYMENT.md](DEPLOYMENT.md)
- 架构总览：[ARCHITECTURE.md](ARCHITECTURE.md)
- 配置参考：[docs/configuration/managed-config-reference.md](docs/configuration/managed-config-reference.md)
- 平台运行手册：[docs/operations/platform_runbook.md](docs/operations/platform_runbook.md)
- Operator 检查清单：[docs/operations/operator_checklist.md](docs/operations/operator_checklist.md)
- RDP 模块参考：[docs/rdp/module_reference.md](docs/rdp/module_reference.md)
- RDP 可靠性与排班：
  - [docs/operations/rdp_reliability_runbook.md](docs/operations/rdp_reliability_runbook.md)
  - [docs/operations/rdp_workflow_calendar.md](docs/operations/rdp_workflow_calendar.md)
- 多进程改造路线：[docs/operations/multiprocess_refactor_roadmap.md](docs/operations/multiprocess_refactor_roadmap.md)

## 安全与运行建议

- 默认先用 `spot` / `derivatives` 做联调，不要直接从 live profile 起步。
- live profile 只应在 Operator、恢复、对账、认证、数据库和日志链路都确认正常后使用。
- 对于任何“能否下单”的问题，优先从 UI 的 `风险与恢复`、`退出任务工作台`、`账户与权限` 页面和 `/system/health`、`/system/recovery` 开始排查。
- 若使用多进程部署，请同时确认 event bus、hot state、healthcheck 和各 process role 的配置一致性。

## 状态说明

这个仓库仍在快速演进，README 只提供当前入口、边界和文档索引。更细的设计推导、阶段性方案和历史任务说明，请查阅 `docs/task/`。
