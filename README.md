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
- **4 进程切片化部署**（gateway / market / decision / execution），Docker Compose 编排
- monolith 单进程模式（回退路径，本地开发用）
- Hybrid/NATS JetStream 跨进程事件总线（双流架构：MARKET 2GB + EVENTS 4GB）
- Redis 跨进程热状态缓存（仓位、订单视图、KillSwitch 同步）
- OTel 端到端分布式追踪（Jaeger）+ 结构化 JSON 日志（Loki）
- Prometheus 指标采集 + Grafana 统一看板（4 数据源、2 仪表盘、5 告警规则）
- OKX 行情、账户快照、模拟盘 / 受保护 live 运行
- RDP 日常采集和多阶段研究编排

> **推荐拓扑：** `derivatives_live` 使用 4 进程拓扑
> （gateway / market / decision / execution），由
> `docker-compose.aats.derivatives-live.yml` 启动。
> 基础设施 9 服务（Postgres / Redis / NATS / Loki / Promtail / Jaeger / Prometheus / Redis-Exporter / Grafana）
> 由 `deploy/wsl2-dev/docker-compose.yml` 独立编排，合计约 7.2GB 内存。
> monolith 单进程模式保留作回退（`docker-compose.aats.derivatives-live-monolith.yml`）。

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

RDP 负责历史数据与参数治理��核心流程是：

```text
数据采集 -> Replay / 研究 -> 归因 / 执行可行性 -> 治理 -> 决策输出 -> 参数集落地
```

治理层全链路采用 **DB-first + 文件 fallback** 双写（`governance` schema, 6 张表），
设置 `AATS_ACTIVE_PARAMETER_DB_URL` 启用 DB 模式，未设置时纯 JSON 文件模式。

## 仓库结构

```text
aats/
  api/                 FastAPI 路由、认证、前端静态资源服务
  bootstrap/           配置装配、runtime 构建、profile/env 加载、日志/OTel 初始化
  bus/                 InMemory / Hybrid / NATS JetStream 事件总线
  schemas/             领域模型
  services/            市场、特征、决策、治理、执行、对账、Operator、RDP 等核心服务
  storage/             EventStore、Postgres 仓库（35+ ORM 表）、Redis hot state、缓存
  data_platform/       RDP 研究平台（6 schema / 47 张表）
apps/
  api_gateway/         FastAPI 入口（process_role=gateway）
  market_gateway/      行情进程入口（process_role=market）
  decision_engine/     决策进程入口（process_role=decision）
  execution_engine/    执行进程入口（process_role=execution）
deploy/
  wsl2-dev/            WSL2 本地基础设施（9 服务 Docker Compose）
    docker-compose.yml   Postgres/Redis/NATS/Loki/Promtail/Jaeger/Prometheus/Grafana/Redis-Exporter
    Dockerfile           AATS 4 进程容器镜像（multi-stage, tini PID 1, 非 root）
    docker-compose.aats.*.yml  4 进程 / monolith 编排
    grafana/             数据源 + 仪表盘 + 告警规则自动注入
    loki/                Loki TSDB v13 配置
    promtail/            Docker SD 日志采集配置
    prometheus/          指标抓取配置
    nats/                JetStream 配置
    RUNBOOK.md           基础设施运维手册
scripts/
  start_api.py         API / UI 启动入口
  run_local.py         本地 in-memory paper loop
  rdp_*.py             RDP 采集、研究、治理脚本
  sync_to_wsl2.sh      Windows → WSL2 代码同步
configs/
  strategy_profiles/   策略 profile 配置
  active_parameter_sets/ RDP 活跃参数集
docs/
  audit/               基础设施组件审查报告
  operations/          运维 runbook
  rdp/                 RDP 说明文档
  configuration/       配置参考
  task/                阶段设计与任务文档
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

### 架构与部署

- 架构总览：[ARCHITECTURE.md](ARCHITECTURE.md)
- 生产发布全流程：[DEPLOYMENT.md](DEPLOYMENT.md)
- 多进程改造路线：[docs/operations/multiprocess_refactor_roadmap.md](docs/operations/multiprocess_refactor_roadmap.md)

### 基础设施

- WSL2 基础设施说明：[deploy/wsl2-dev/README.md](deploy/wsl2-dev/README.md)
- 基础设施运维手册：[deploy/wsl2-dev/RUNBOOK.md](deploy/wsl2-dev/RUNBOOK.md)
- 配置参考：[docs/configuration/managed-config-reference.md](docs/configuration/managed-config-reference.md)

### 运维

- 平台运行手册：[docs/operations/platform_runbook.md](docs/operations/platform_runbook.md)
- Operator 检查清单：[docs/operations/operator_checklist.md](docs/operations/operator_checklist.md)

### RDP 研究平台

- RDP 模块参考：[docs/rdp/module_reference.md](docs/rdp/module_reference.md)
- RDP 可靠性与排班：
  - [docs/operations/rdp_reliability_runbook.md](docs/operations/rdp_reliability_runbook.md)
  - [docs/operations/rdp_workflow_calendar.md](docs/operations/rdp_workflow_calendar.md)

### 审查报告

- Postgres 模块审查：[docs/audit/postgres_module_audit.md](docs/audit/postgres_module_audit.md)
- Loki+Promtail 管线审查：[docs/audit/loki_promtail_module_audit.md](docs/audit/loki_promtail_module_audit.md)

## 基础设施概览

多进程部署依赖以下 9 个基础设施服务（全部运行在 WSL2 Docker Compose 内，零云费用）：

| 组件 | 用途 | 端口 | 内存 |
|------|------|------|------|
| Postgres 16 | 主存储（账务、订单、策略状态） | 5432 | 2560M |
| Redis 7 | 跨进程热状态缓存 | 6379 | 512M |
| NATS 2.10 | JetStream 跨进程事件总线 | 4222/8222 | 1024M |
| Loki 3.0 | 日志聚合（7 天保留） | 3100 | 512M |
| Promtail 3.0 | Docker 容器日志采集 | — | 256M |
| Jaeger 1.57 | 分布式 trace（OTLP gRPC+HTTP） | 16686/4317 | 1536M |
| Prometheus 2.51 | 进程指标采集 | 9090 | 256M |
| Redis-Exporter | Redis 指标 → Prometheus | 9121 | 64M |
| Grafana 10.4.4 | 统一看板 + 告警 | 3000 | 512M |

合计约 7.2GB 内存。详见 [deploy/wsl2-dev/README.md](deploy/wsl2-dev/README.md)。

## 安全与运行建议

- 默认先用 `spot` / `derivatives` 做联调，不要直接从 live profile 起步。
- live profile 只应在 Operator、恢复、对账、认证、数据库和日志链路都确认正常后使用。
- 对于任何”能否下单”的问题，优先从 UI 的 `风险与恢复`、`退出任务工作台`、`账户与权限` 页面和 `/system/health`、`/system/recovery` 开始排查。
- 多进程部署前必须确认：event bus（hybrid/nats）、hot state（redis）、healthcheck、各 process role 配置一致。
- 基础设施运维参见 [RUNBOOK.md](deploy/wsl2-dev/RUNBOOK.md)，含健康检查、故障排查、备份恢复。

## 状态说明

这个仓库仍在快速演进，README 只提供当前入口、边界和文档索引。更细的设计推导、阶段性方案和历史任务说明，请查阅 `docs/task/`。
