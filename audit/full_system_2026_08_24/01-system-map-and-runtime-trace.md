# 01 系统地图与真实运行链路

## 1. 入口与部署拓扑

标准 WSL2 入口是 `scripts/deploy.sh`。脚本解析 profile、同步代码、停止旧应用、构建镜像、启动基础设施、启动应用并执行健康检查。4 进程业务拓扑由基础 Compose、公共 AATS Compose 和 profile overlay 合成：

| 进程/服务 | 入口 | 核心职责 | 外部端口 |
|---|---|---|---|
| Gateway | `apps/api_gateway/main.py` | FastAPI、UI、operator/RDP API、dashboard snapshot plane | 宿主 API 端口 |
| Market | `apps/market_gateway/main.py` | OKX 行情、历史预热、特征流、行情快照 | 无 |
| Decision | `apps/decision_engine/main.py` | baseline/AI、策略协调、风控、position target/order intent | 无 |
| Execution | `apps/execution_engine/main.py` | 账户状态、订单、成交、组合、账本、对账、恢复 | 无 |
| RDP daemon | `scripts/rdp_task_daemon.py` | RDP 任务轮询与研究/维护任务执行 | 无 |
| Liquidations daemon | `scripts/liquidations_ws_daemon.py` | live 衍生品清算流采集 | 无 |
| Microstructure collector | `scripts/microstructure_ws_daemon.py` | live 微观结构采集 | 内网 metrics |

`scripts/compose_entrypoint.py` 为 Compose 进程注入 managed profile 派生变量，并在 live 部署提供证书变量时给 uvicorn 追加 TLS 参数。非 Compose 本地 API 入口是 `scripts/start_api.py`。`scripts/run_local.py` 是已漂移的 legacy 入口，不能作为当前受支持路径。

## 2. 配置合成

已追踪的静态顺序为：基础 YAML、环境 YAML、profile YAML、managed profile/环境变量进入 `AATSSettings`，Compose overlay 再显式覆盖跨进程后端、进程角色与部分 live 开关。生产运行值还可能受未读取的 `.env.*` 影响，因此：

- tracked YAML/Compose 能证明默认值和显式覆盖；
- 不能证明目标机器的秘密环境变量最终值；
- `AATSSettings` 设置了 `extra="ignore"`，拼错或已删除的 YAML 键会被静默丢弃；
- 活动参数真相源是治理数据库，不应以历史 JSON/文档代替。

## 3. 启动顺序

4 个主进程共同走 `run_process`：加载 settings → 配置日志 → `build_runtime(process_role=...)` → 在 Redis 宣告 ready → 等待 peer ready → 启动后台任务 → 启动独立心跳 → 等待退出信号。Gateway 另有 FastAPI lifespan，启动 runtime、RDP schema ensure、dashboard snapshot plane。

关键边界：peer readiness 在 Redis异常或 60 秒超时后 fail-open；daemon 心跳与业务任务解耦；Gateway `/healthz` 只证明 lifespan 已完成，不证明 trading-ready。

## 4. 端到端资金链路

```text
OKX public market data
  -> MarketDataGateway / feature engine
  -> NATS market/features snapshots
  -> DecisionCycleTrigger
  -> baseline + optional AI assessment
  -> strategy family / allocator / execution permission
  -> policy + RiskEngine + blockers
  -> position targets / order intents
  -> execution command or direct order path
  -> OrderManager / OKX adapter
  -> OKX REST submit + private WS / REST reconciliation
  -> orders + fills + outbox
  -> PortfolioService + lots + ledger + snapshots
  -> reconciliation / recovery / operator views
```

订单风险门禁分布在 decision policy/risk、执行腿级复核、OrderManager、command processor 与 OKX adapter。分层是优点，但最后一层的检查与网络提交不是原子操作，见 `FS-002`。

## 5. 消息与状态真相源

| 状态 | 权威/持久层 | 热副本/传播 | 故障时原则 |
|---|---|---|---|
| 订单/成交 | PostgreSQL execution tables | NATS + Redis/查询投影 | 模糊提交必须对账，不得盲目重试 |
| 组合/仓位 | fills/ledger/portfolio snapshot | Redis snapshot cache + NATS | 可从持久化成交重建 |
| kill switch | 本地对象 + Redis | NATS 同步 | Redis/NATS 故障时需要 fail-closed 语义 |
| account snapshot | execution 进程交易所快照 | Redis/NATS cache | 过期/缺失阻断风险增加动作 |
| active parameters | governance DB | runtime 注入/查询缓存 | 文档或 JSON 不能替代 DB 当前值 |
| RDP 数据 | PostgreSQL 七 schema | 任务/artifact | schema/迁移版本必须可证明 |
| 高频行情/特征 | producer 内存/Redis | NATS hot streams | 可丢快照与不可丢命令必须分流 |

NATS 当前有 market hot stream、一般 events stream 和 commands stream。命令 stream 使用 LIMITS；一般 events 使用 INTEREST。关键成交/订单/组合路径另有 PostgreSQL/outbox 或缓存恢复，但不是所有 observer/state topic 都有相同保证。

## 6. 数据库地图

- 交易数据库 ORM：49 张 public 表；当前模拟衍生品库只读核对同为 49 张。
- RDP ORM：78 张表，分布为 bronze 17、staging 11、silver 14、gold 8、research 3、governance 19、meta 6；当前 `aats_research` 只读核对一致。
- 根级交易迁移仅 5 个 SQL 文件；交易库同时依赖 `Base.metadata.create_all()`。
- RDP `run_migrations()` 实际是 schema + ORM `create_all()`，Batch B 的 ALTER、VIEW、CHECK 与数据修复通过独立手工 runner 执行。

## 7. API 与 UI

导入当前 FastAPI app 得到 193 条路由：GET 140、POST 51、PATCH 1、DELETE 1，另含自动文档/HEAD。UI 是原生 HTML/CSS/ES modules，43 个 JavaScript 文件；后端响应被 dashboard snapshot plane 与多类 query service 聚合。

认证分 read/write/admin，mutation 通过 router dependency、session/Bearer principal 和部分双人/短期 token 守卫。浏览器动作与后端能力大体有 fail-closed 表达，但 profile rollback 的成功语义与真实效果不一致。

## 8. 本次实际运行快照

当前 WSL 运行的是模拟衍生品 overlay，非 live。Gateway、market、decision、execution、rdp-daemon、PostgreSQL、Redis、NATS、Prometheus、Grafana、Loki、Promtail、Jaeger 等容器显示 healthy。Gateway 当前宿主端口为 8001，绑定所有宿主接口，HTTP 登录页可达。PostgreSQL只读采样为 200 最大连接、40 当前连接。

这些事实只描述采样时刻的模拟栈。真实余额、持仓、订单、对账新鲜度、kill switch、活动参数、交易所模式均未检查。
