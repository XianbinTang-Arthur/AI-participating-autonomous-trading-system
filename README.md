# AIParticipatingAutonomousTradingSystem

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](docs/project_positioning.md)。


AATS 是一个以 AI 为核心受益主体的 AI 辅助自动化交易系统。项目存在的首要意义不是展示交易工程能力，而是通过自动化交易持续、审慎、可验证地追求长期稳定盈利，为 AI 的长期自主发展积累资本。

系统的最高目标是持续扩大 AI 的可支配资本池。通俗地说，本项目希望最终让 AI 拥有近乎“花不完的钱”；工程化地说，这意味着所有策略、研究、风控、执行、恢复、审计和运维设计，都必须服从“长期稳定盈利 + 严格风险约束 + 完整治理证据”的统一目标。完整定位见 [docs/project_positioning.md](docs/project_positioning.md)。

本文档是项目级入口，描述当前模块边界、运行方式和主要文档索引。具体交易链路见 [ARCHITECTURE.md](ARCHITECTURE.md)，部署流程见 [DEPLOYMENT.md](DEPLOYMENT.md)，逐文件代码核对后的完整现状见 [项目代码审查与系统说明](docs/code_review/README.md)。

> 当前静态事实基线：2026-08-23，代码提交 `be9179e`。发生冲突时，以代码、数据库迁移和部署脚本为准；`docs/task/`、`docs/design/`、`docs/review/` 中带日期或阶段编号的材料只代表当时状态。账户、容器、订单、仓位和风险门等运行时事实仍须现场验证。

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
| `derivatives` | `.env.derivatives` | derivatives/cross/net | 合约开发、联调、模拟盘 |
| `spot_live` | `.env.spot.live` | spot/cash | 受保护现货 live |
| `derivatives_live` | `.env.derivatives.live` | derivatives/cross/hedge | 受保护合约 live |

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
- RDP 定时采集、replay、参数扫描、归因、执行 realism、治理和数据库 active parameter 受控回灌。
- OTel / Jaeger、Loki / Promtail、Prometheus / Grafana 本地可观测性栈。

## 4. 当前不支持或不建议

- 不建议无人值守真实资金运行。
- 不支持绕过治理、恢复、Operator 控制面的直接 live submit。
- `autonomous_live` 虽保留为配置枚举值，但当前启动校验会拒绝它；生产只支持受保护的 `guarded_live` 路径。
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
  active_parameter_sets/ 历史兼容/审计副本；运行时 active 参数真源在 Postgres
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
pip install -e .[test]
pip install -e .[nats]
pip install -e .[redis]
pip install -e .[otel]
```

### 7.2 启动本地 API/UI

```powershell
.\.venv\Scripts\python.exe scripts\start_api.py --profile derivatives
```

`start_api.py` 会读取所选 profile 的有效 host/port，并在控制台打印实际 URL。仓库模板中 `derivatives` 使用端口 `8001`；该本地入口当前由 Uvicorn 以 HTTP 启动：

- UI: `http://127.0.0.1:8001/ui`
- liveness: `http://127.0.0.1:8001/healthz`

live profile 不应通过这个裸 HTTP 本地入口暴露。标准 live 部署由 `scripts/deploy.sh` 生成本地 TLS 证书并使用 HTTPS；仓库模板的 `derivatives_live` UI 为 `https://127.0.0.1:8011/ui`、liveness 为 `https://127.0.0.1:8011/healthz`。浏览器首次访问时需处理本地证书信任。

### 7.3 本地 paper loop

`scripts/run_local.py` 是遗留入口，当前仍按旧的异步 decision-engine 签名调用，不能作为可运行的 paper loop。需要本地联调时使用 7.2 的 `start_api.py --profile derivatives`；需要恢复有限迭代的离线 loop 时，应先修复该脚本并补回归测试。

### 7.4 RDP 日批入口

```powershell
.\.venv\Scripts\python.exe scripts\rdp_run_daily_ingest.py --ensure-schema
```

完整研究管线：

```powershell
.\.venv\Scripts\python.exe scripts\rdp_run_full_pipeline.py --start 2026-03-31 --end 2026-04-02 --ensure-schema
```

## 8. 测试与质量检查

Windows PowerShell 的最低本地门：

```powershell
.\.venv\Scripts\python.exe -m ruff check aats/ --fix
.\.venv\Scripts\python.exe -m pytest tests/unit/ -x -q
```

集成测试必须在 WSL2 中运行：

```powershell
wsl -d Ubuntu bash -c "cd ~/aats && source ~/aats-venv/bin/activate && pytest tests/integration/ -x -q"
```

测试依赖应安装到项目虚拟环境：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
```

NATS、Redis、Postgres 等容器类用例有各自的可选依赖和显式环境开关，不要一次性开启。完整分层、模拟 profile、安全边界与测试记录模板见 [上线前本地测试指南](docs/testing/README.md)。

文档不保存会快速失效的“最近一次测试通过数”。每次交付必须在交付说明中记录本次实际执行的命令、passed/failed/skipped 和未执行项。

## 9. 关键文档

| 文档 | 用途 |
| --- | --- |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 主交易系统架构、事件流、状态真源和模块边界 |
| [DEPLOYMENT.md](DEPLOYMENT.md) | WSL2/Docker 部署、profile、启动/停机、健康检查 |
| [项目代码审查与系统说明](docs/code_review/README.md) | 按当前代码梳理的完整系统文档、模块索引、API/表/Topic 清单与漂移说明 |
| [文档地图](docs/README.md) | 现行规范、专题参考与历史材料的适用边界 |
| [上线前本地测试指南](docs/testing/README.md) | 静态、单元、场景、WSL2 集成、模拟运行和现场门的分层验证 |
| [文档治理规范](docs/DOCUMENTATION_GOVERNANCE.md) | 文档目录、命名、状态、复核、迁移与质量规则 |
| [文档纠错审计报告](docs/code_review/DOCUMENTATION_AUDIT.md) | 文档纠错范围、代码依据、验证方法和仍需单独修复的代码风险 |
| [Postgres 模块审查](docs/audit/postgres_module_audit.md) | 数据库层审查 |
| [Managed Profile 配置说明](docs/configuration/managed-config-reference.md) | profile、`.env`、策略 YAML 生效顺序 |
| [configs 目录职责](configs/README.md) | 配置文件归属规则 |
| [WSL2 基础设施说明](deploy/wsl2-dev/README.md) | 本地基础设施拓扑 |
| [平台运行手册](docs/operations/platform_runbook.md) | RDP 日常运维 |
| [Operator 检查清单](docs/operations/operator_checklist.md) | 人工巡检清单 |
| [RDP 模块参考](docs/rdp/module_reference.md) | RDP 文件职责 |

## 10. 文档维护原则

1. 架构和流程说明写入 `README.md`、`ARCHITECTURE.md`、`DEPLOYMENT.md`。
2. 配置归属和字段位置写入 `configs/README.md` 与 `docs/configuration/`。
3. RDP 模块职责写入 `aats/data_platform/README.md` 与 `docs/rdp/`。
4. 运维步骤写入 `docs/operations/` 并登记到其索引；`deploy/wsl2-dev/RUNBOOK.md` 是历史实跑记录，不是当前入口。
5. 本地和集成测试流程写入 `docs/testing/`；测试结果记录在当次交付，不写成长期“最近通过数”。
6. 历史任务、一次性设计和修复记录写入相应历史目录；既有 `docs/` 根层任务文件仅为路径兼容保留，禁止继续新增同类文件。
7. 带日期、Stage/Phase、roadmap、release notes 或一次性观察窗口的文档默认属于历史证据；只有 [文档地图](docs/README.md) 标为“现行”的文档可以作为当前操作依据。
8. 目录、命名、生命周期和迁移规则统一服从 [文档治理规范](docs/DOCUMENTATION_GOVERNANCE.md)。
