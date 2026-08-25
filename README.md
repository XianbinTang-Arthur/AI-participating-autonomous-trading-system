# AIParticipatingAutonomousTradingSystem

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](docs/project_positioning.md)。


AATS 是一个以 AI 为核心受益主体的 AI 辅助自动化交易系统。项目存在的首要意义不是展示交易工程能力，而是通过自动化交易持续、审慎、可验证地追求长期稳定盈利，为 AI 的长期自主发展积累资本。

系统的最高目标是持续扩大 AI 的可支配资本池。通俗地说，本项目希望最终让 AI 拥有近乎“花不完的钱”；工程化地说，这意味着所有策略、研究、风控、执行、恢复、审计和运维设计，都必须服从“长期稳定盈利 + 严格风险约束 + 完整治理证据”的统一目标。完整定位见 [docs/project_positioning.md](docs/project_positioning.md)。

本文档是项目级入口，描述当前模块边界、运行方式和主要文档索引。具体交易链路见 [ARCHITECTURE.md](ARCHITECTURE.md)，部署流程见 [DEPLOYMENT.md](DEPLOYMENT.md)，逐文件代码核对后的完整现状见 [项目代码审查与系统说明](docs/code_review/README.md)。

> 当前静态事实复核：2026-08-25，起始 HEAD `00b6df0f8a8d2665d6cae3e88996843767cd1f56`，当前工作区包含 Phase 3A–3W 整改提交候选。发生冲突时，以当前工作区代码、数据库迁移和部署脚本为准；`docs/task/`、`docs/design/`、`docs/review/` 中带日期或阶段编号的材料只代表当时状态。账户、容器、订单、仓位、schema、实际网络绑定与风险门等运行时事实仍须现场验证。

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

`start_api.py` 只接受 `spot`/`derivatives` 模拟 profile 和 loopback host，显式固定
`AATS_PROCESS_ROLE=monolith` 以构建完整单进程 runtime，并在控制台打印实际 URL。仓库模板中
`derivatives` 使用端口 `8001`；该本地入口当前由 Uvicorn 以 HTTP 启动：

- UI: `http://127.0.0.1:8001/ui`
- liveness: `http://127.0.0.1:8001/healthz`

live profile 不应通过这个裸 HTTP 本地入口暴露。仓库仍保留 future live TLS/端口配置，但 Phase 3F/3G 在当前全系统 NO-GO 期间硬禁用标准部署和本地启动入口的所有 live profile，且没有 override。本地入口也拒绝 `0.0.0.0`、`::` 与非 loopback 地址；不得从模板 URL 推断 live 已部署或可放行。

Phase 3H 新增统一浏览器安全头与 Host 失败关闭：当前只允许本机 Host，并对 HTML、JSON、认证错误与 Host 400 响应统一覆盖严格 CSP、`DENY`、`nosniff`、`no-referrer`、Permissions Policy、COOP 和 CORP。HTTP 不输出 HSTS，仅实际 HTTPS ASGI scope 输出 `max-age=31536000`。这是代码/ASGI 级结论，不代表真实 TLS terminator、proxy 或目标浏览器已验证。

Phase 3I 已把 Operator 登录中的同步 repository、390,000 轮 PBKDF2、账户状态与审计写入完整移出 event loop，并以每 Gateway 进程默认 4 个 worker、1 秒排队超时和 60 秒 global/client/identity `60/20/10` 限流失败关闭。不存在、禁用或损坏 hash 走固定 dummy KDF，登录输入有明确上限。该 limiter 不跨进程，目标 proxy/Redis 限流、真实数据库和生产等价负载尚未验证；当前裁定仅为 `CODE REMEDIATED / DISTRIBUTED RATE-LIMIT & LOAD VERIFICATION OPEN`。

Phase 3J 已将四主进程 NATS/hybrid 启动 barrier 改为失败关闭：Redis announce/poll 失败、peer 超时、缺少热状态或缺少部署代次时，不启动 background publisher。标准模拟部署为每次流程生成同一非秘密 generation，Redis ready key/payload 和不可覆盖证据包都绑定该值，旧代次不能满足新启动。这是代码/隔离单测结论；NATS/Redis/Compose 目标环境启动、重启与断连矩阵仍未执行，当前裁定为 `CODE REMEDIATED / TARGET NATS STARTUP-RESTART VERIFICATION OPEN`。

Phase 3K 为七条固定周期资金关键循环增加成功进度 deadline：账户刷新、执行同步、对账、execution outbox、execution command flow、Phase 1 shadow 与 trial guard 若永久 await 或连续无成功周期，会被分类为 `stalled` 并触发 daemon 非零退出或 FastAPI health `503`。该结论只来自代码和纯内存测试；事件驱动任务、整个 event loop 阻塞、真实依赖/容器 restart/告警与独立复核仍未完成，所以 FS-006 仍是 P1 HARD BLOCKER，状态为 `PARTIALLY REMEDIATED / EVENT-DRIVEN AND TARGET RUNTIME VERIFICATION OPEN`。

Phase 3L 把 FS-002 的长期恢复状态与在线增险许可分离：Gateway/monolith 只在同 generation 的权威 RUNNING 状态下，以 Redis 服务器 TTL 维护 15 秒 permission lease（每 5 秒续租）；execution 在最终 `place_order` fence 内读取长期 authority 后还必须读取同 generation permission，且 execution 不能自行续租。四进程代理 resume 只有在 Gateway 重读 authority 并激活同 generation permission 后才向 Operator 返回成功。halt、shutdown 或控制面续租失败不会延长旧许可；删除失败仍由 TTL 收敛。该上界只由代码和 InMemory TTL 故障注入证明，真实 Redis/NATS 四进程单向分区、crash/restart/告警、多实例协议和独立复核仍未完成，因此 FS-002 仍为 P0 HARD BLOCKER，状态为 `PARTIALLY REMEDIATED / TARGET PARTITION-EXPIRY VERIFICATION OPEN`。

Phase 3M 已封闭 FS-001 的另一条错误成功路径：profile recommendation 的 apply 与 rollback 现在都只在认证、action-bound token、状态和双人签署校验后返回无写入 `501`；不会创建/续跑历史 apply Saga，不会写 research/live 数据，也不会把 recommendation 标为 `applied/rolled_back`。approve/release 仍只是研究治理状态，不能推导交易 runtime 已采用参数。真正的 execution-owned profile activation、单调 generation、worker ack/readback、反向 Saga 和历史漂移对账仍未实现，因此 FS-001 继续是 P1 HARD BLOCKER，G2 未放行。

Phase 3N 将离线 backtest fill 固定为 `ohlcv_participation_cap_v2`：IOC、post-only 与 bounded-limit 都要求正 volume 并受默认 1% participation cap，超量只产生 partial fill；IOC/bounded 在 next-open 只使用已经闭合的 observation volume，bounded 按保守 taker fee + fixed slippage 计价，成本诊断显式保存 fee/slippage。scorecard 会声明 OHLCV 粒度以及无 L2 depth、spread/queue、impact/latency 校准。该变更只收敛 bar proxy 的全成与成本漏记，不能证明 live 容量或收益；FS-014 仍为 `PARTIALLY REMEDIATED / OHLCV CONTAINED / L2 CALIBRATION OPEN`，G3 未放行。

Phase 3O 将 Dashboard 详情抽屉改为具名、具说明的原生 modal `<dialog>`：九类异步详情入口保留原触发按钮，打开后焦点进入关闭按钮，关闭按钮/Escape/backdrop 走同一清理路径并尽可能返回焦点。`prefers-reduced-motion: reduce` 同时停止 CSS 动画/过渡/平滑滚动和 JavaScript 显式 smooth scroll。该结论来自静态契约、Node 语法和单元测试；目标浏览器、键盘-only、NVDA/VoiceOver、axe、缩放与 reduced-motion 人工观察仍 OPEN，因此 FS-017/018 不能标为最终 CLOSED。

Phase 3P 删除了四个 managed strategy profile 中没有 Settings 字段或行为消费者的伪 auto-rollback 键；managed loader 现在要求 YAML 为 mapping，并对 runtime defaults 与 YAML 的全部 key 使用 `AATSSettings.model_fields` 失败关闭校验。配置生成器 reference 与受版本控制文档保持一致，也不再覆盖人工治理的 `configs/README.md`。该结论来自静态集合比较和单元测试；committed candidate 的四 profile 目标启动、仓库外 overlay 盘点、generator clean-run 与独立复核仍 OPEN。

### 7.3 本地 paper loop

`scripts/run_local.py` 是保留路径的迁移失败入口：它只识别旧参数、输出迁移说明并返回 exit `2`，不会加载 `.env.*`、导入 runtime 或启动服务。需要本地联调时使用 7.2 的 `start_api.py --profile derivatives`；有限迭代闭环应运行明确选择的 integration scenario。仓库外旧调用方迁移与独立复核仍 OPEN。

### 7.4 RDP 日批入口

RDP schema 必须先由部署期显式 migration job（live/WSL2）或受控的 `scripts/rdp_init_db.py`（明确非 live 目标）完成。下列 `--ensure-schema` 是兼容旧名，现在只读校验 schema contract，不做 DDL。

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

发布镜像和 Python 3.12 CI 不使用上述本地开放解析结果。Phase 3T 在
[`requirements/`](requirements/) 提交了 Linux x86_64 的完整版本/hash 锁；Docker 与
CI 使用 `--require-hashes --only-binary=:all:` 消费，外部 Compose 镜像同时固定
manifest digest。该约束只固定输入内容，尚未完成 APT snapshot、clean Docker build、
SBOM、CVE/license/secret/provenance 和远端 CI 证据；FS-022 仍是部分整改。

Phase 3U 又把全部应用 `create_engine` 调用纳入静态 inventory，并在
[`aats/storage/connection_budget.py`](aats/storage/connection_budget.py) 集中定义角色化
pool ceiling。四进程声明 topology 从历史理论 317/321 收敛为 150；当前 Compose 普通
连接容量为 197，名义余量 47。该结果只证明受版本控制配置的算术与防回退，不证明目标
负载、慢查询、故障重连、瞬时 CLI/治理/迁移或 PostgreSQL 内存容量已经通过；FS-008 仍是
部分整改。证据见
[`41-fs-008-database-connection-budget.md`](audit/full_system_2026_08_24/41-fs-008-database-connection-budget.md)。

Phase 3V 将 Research Factory real-data runner 升级为 train/valid 双门选择协议：candidate
metrics 只代表 valid development evidence，test 只参与输入质量检查与精确内容 seal，不能
进入 factor、label、绩效 metrics 或 selection gate；execution summary 也必须精确绑定 valid 窗口，覆盖完整
实验窗口会失败关闭。新 recommendation 会披露 sealed test 尚未评估；最终
OOS、walk-forward、历史 artifact/人工查看审计、多重检验与独立复核仍未完成，因此
FS-004 仍是部分整改。证据见
[`42-fs-004-research-selection-holdout.md`](audit/full_system_2026_08_24/42-fs-004-research-selection-holdout.md)。

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
