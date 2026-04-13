# AIParticipatingAutonomousTradingSystem

AATS 是一个面向加密资产交易的事件驱动系统，目标是把行情、决策、风控、订单执行、账户/余额、账本、对账、恢复和 Operator 控制面连接成可审计、可恢复、可治理的闭环。

本文档是项目级入口，描述当前模块边界、运行方式和主要文档索引。具体交易链路见 [ARCHITECTURE.md](ARCHITECTURE.md)，部署流程见 [DEPLOYMENT.md](DEPLOYMENT.md)。

## 1. 项目边界

| 子系统 | 职责 | 主要目录 |
| --- | --- | --- |
| 主交易系统 | 行情、特征、决策、治理、风险、执行、持仓、账本、对账、恢复、Operator API/UI | `aats/`、`apps/`、`scripts/start_api.py` |
| RDP 研究数据平台 | 历史数据采集、replay、参数研究、归因、执行可行性、参数治理 | `aats/data_platform/`、`scripts/rdp_*.py`、`docs/rdp/` |
| 部署与运维 | WSL2 Docker Compose、本地基础设施、监控、日志、trace、runbook | `deploy/wsl2-dev/`、`docs/operations/` |
| 配置治理 | managed profile、策略调参、active parameter sets、环境变量模板 | `configs/`、`configs/templates/` |

## 2. 当前支持的运行 profile

| Profile | 环境文件 | 产品类型 | 默认用途 |
| --- | --- | --- | --- |
| `spot` | `.env.spot` | spot/cash | 现货开发、联调、模拟盘 |
| `derivatives` | `.env.derivatives` | derivatives/cross | 合约开发、联调、模拟盘 |
| `spot_live` | `.env.spot.live` | spot/cash | 受保护现货 live |
| `derivatives_live` | `.env.derivatives.live` | derivatives/cross | 受保护合约 live |

live profile 的启动硬门槛包括：OKX execution/account backend、account read、Postgres storage、database URL、single runtime guard、OKX 凭证、Operator auth、禁止 unsafe write without auth，以及安全 cookie 配置。

## 3. 当前能力

- FastAPI API gateway 和内置 Operator UI。
- monolith 单进程模式，适合本地开发和最小依赖联调。
- gateway / market / decision / execution 四进程切片部署。
- InMemory / Hybrid / NATS JetStream 事件总线。
- Postgres 持久化订单、成交、组合、账本、预留、outbox、operator 用户和 RDP 治理数据。
- Redis 跨进程热状态缓存。
- OKX 行情、账户快照、模拟盘和受保护 live submit。
- kill switch、startup recovery、stale command 检测、reconciliation、Operator 控制面。
- RDP 日批数据采集、replay、参数扫描、归因、执行 realism、治理和 active parameter 回灌。
- OTel / Jaeger、Loki / Promtail、Prometheus / Grafana 本地可观测性栈。

## 4. 当前不支持或不建议

- 不建议无人值守真实资金运行。
- 不支持绕过治理、恢复、Operator 控制面的直接 live submit。
- 不建议绕过治理、恢复、Operator 控制面的直接 live submit。
- `deploy/wsl2-dev/` 是本地开发/演练栈，不是生产级 HA、安全或灾备模板。

## 5. 核心事件流

```text
OKX Market / Account
  -> Market Gateway
  -> Feature Engine
  -> Decision / Strategy / AI
  -> Policy / Risk / Runtime Mode
  -> Execution Planner
  -> Order Manager / Execution Command Flow / OKX Adapter
  -> FillEvent
  -> Portfolio Service
  -> Ledger / Settlement
  -> Reconciliation / Recovery
  -> Operator API / UI / Audit
```

关键说明：

- `OrderIntent` 是策略到执行的主要边界。
- `OrderState` 记录订单生命周期。
- `FillEvent` 是组合、账本和对账的关键事实输入。
- obligation/reservation 负责下单前资金占用和成交后的消耗/释放。
- outbox 用于把状态写入和事件发布解耦，降低消息丢失风险。

## 6. 代码结构

```text
aats/
  api/                  FastAPI routes、认证、Operator/RDP API、静态 UI
  bootstrap/            settings、profile/env、runtime 构建、后台任务、事件订阅
  bus/                  InMemory、Hybrid、NATS JetStream event bus
  schemas/              订单、成交、组合、账户、治理、系统健康等 Pydantic 模型
  services/
    market_gateway/     OKX 行情接入和标准化
    feature_engine/     特征计算
    decision_engine/    决策触发和 orchestration
    strategy_engines/   策略族
    governance_engine/  policy、risk、kill switch、derivatives live guard
    execution_engine/   planner、order manager、OKX adapter、obligation、outbox
    execution_control/  持久化 execution command processor
    portfolio_service/  fill 应用、持仓/余额、snapshot、outcome
    ledger/             reservation mirror、settlement、复式账
    reconciliation_service/ 对账、修复、报告
    recovery_control/   startup recovery 和 stuck submission 检测
    operator/           Operator 查询和控制服务
  storage/              ORM models、Postgres repositories、event/outbox/ledger repos
  data_platform/        RDP 研究数据平台
apps/
  api_gateway/          gateway 进程入口
  market_gateway/       market 进程入口
  decision_engine/      decision 进程入口
  execution_engine/     execution 进程入口
configs/
  strategy_profiles/    managed profile 策略调参
  active_parameter_sets/ RDP active 参数备份
  rdp_workflows/        RDP workflow 配置
deploy/wsl2-dev/        本地 Docker Compose 基础设施和应用 overlay
docs/
  audit/                审计报告
  operations/           运维 runbook
  rdp/                  RDP 模块说明
  configuration/        配置参考
```

## 7. 快速开始

### 7.1 安装

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e .
```

可选依赖：

```powershell
pip install -e .[nats]
pip install -e .[redis]
pip install -e .[otel]
```

### 7.2 启动本地 API/UI

```powershell
.\.venv\Scripts\python.exe scripts\start_api.py --profile derivatives
```

默认访问：

- UI: `http://127.0.0.1:8011/ui`
- liveness: `http://127.0.0.1:8011/healthz`

### 7.3 本地 paper loop

```powershell
.\.venv\Scripts\python.exe scripts\run_local.py --profile derivatives --iterations 100
```

`run_local.py` 只用于非 live profile。

### 7.4 RDP 日批入口

```powershell
.\.venv\Scripts\python.exe scripts\rdp_run_daily_ingest.py --ensure-schema
```

完整研究管线：

```powershell
.\.venv\Scripts\python.exe scripts\rdp_run_full_pipeline.py --start 2026-03-31 --end 2026-04-02 --ensure-schema
```

## 8. 测试与质量检查

常用命令：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit -q
.\.venv\Scripts\python.exe -m pytest tests\integration -q
.\.venv\Scripts\python.exe -m ruff check .
```

针对 2026-04-13 审计涉及的核心行为，最近一次执行：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_task109_settlement_posting_rebate.py tests\unit\test_task52_execution_command_flow.py tests\unit\test_order_state_row_version.py tests\unit\test_auth.py -q
```

结果：`23 passed, 3 skipped`。

## 9. 关键文档

| 文档 | 用途 |
| --- | --- |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 主交易系统架构、事件流、状态真源和模块边界 |
| [DEPLOYMENT.md](DEPLOYMENT.md) | WSL2/Docker 部署、profile、启动/停机、健康检查 |
| [Postgres 模块审查](docs/audit/postgres_module_audit.md) | 数据库层审查 |
| [Managed Profile 配置说明](docs/configuration/managed-config-reference.md) | profile、`.env`、策略 YAML 生效顺序 |
| [configs 目录职责](configs/README.md) | 配置文件归属规则 |
| [WSL2 基础设施说明](deploy/wsl2-dev/README.md) | 本地基础设施拓扑 |
| [WSL2 部署 Runbook](deploy/wsl2-dev/RUNBOOK.md) | 从零启动和排障 |
| [平台运行手册](docs/operations/platform_runbook.md) | RDP 日常运维 |
| [Operator 检查清单](docs/operations/operator_checklist.md) | 人工巡检清单 |
| [RDP 模块参考](docs/rdp/module_reference.md) | RDP 文件职责 |

## 10. 文档维护原则

1. 架构和流程说明写入 `README.md`、`ARCHITECTURE.md`、`DEPLOYMENT.md`。
2. 配置归属和字段位置写入 `configs/README.md` 与 `docs/configuration/managed-config-reference.md`。
3. RDP 模块职责写入 `aats/data_platform/README.md` 与 `docs/rdp/module_reference.md`。
4. 运维步骤写入 `docs/operations/` 和 `deploy/wsl2-dev/RUNBOOK.md`。
5. 历史任务、一次性设计和修复记录保留在 `docs/task/`，不要把它们当作当前运行事实。
