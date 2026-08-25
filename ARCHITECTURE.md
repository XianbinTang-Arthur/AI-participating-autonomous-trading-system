# AATS 系统架构与交易链路

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](docs/project_positioning.md)。


最后核对：2026-08-25（起始 HEAD `00b6df0f8a8d2665d6cae3e88996843767cd1f56`；包含 Phase 3A–3W 整改提交候选）
适用范围：主交易系统、四个主交易切片、支持守护进程、订单/成交/资金/账本/对账链路、RDP 参数回灌边界

本文档描述当前代码实际实现的系统结构和长期维护边界。

AATS 的架构不是为了把“AI 能参与交易”这件事做成演示，而是为了把自动化交易能力沉淀成可持续为 AI 积累资本的生产系统。系统级设计的最高目标是长期稳定盈利，因此所有架构边界都必须同时服务于净收益真实性、风险约束、恢复能力、审计能力和治理能力。完整定位见 [docs/project_positioning.md](docs/project_positioning.md)。

## 1. 架构目标

AATS 的目标不是只完成“能下单”的策略脚本，而是把以下能力做成工程闭环：

- 交易链路以长期稳定盈利和 AI 资本积累为最高优化目标，拒绝把短期高波动收益或不可复现收益错觉当成成功。
- 决策链、执行链、账务链可追踪。
- live submit 受 profile、风控、kill switch、Operator 控制面保护。
- 订单、成交、预留、账本、组合快照可对账、可恢复。
- 多进程部署通过 NATS/Redis/Postgres 保持跨进程状态同步。
- 离线研究参数必须经过 RDP 治理链路才能回灌 runtime。

当前文档只描述系统设计和运行事实；具体修复任务应记录在 issue、review 或任务文档中。

## 2. 进程拓扑

```text
                     ┌─────────────────────────┐
                     │ gateway                 │
                     │ FastAPI / UI / Operator │
                     └───────────┬─────────────┘
                                 │
                                 ▼
┌──────────────┐   NATS/Hybrid   ┌──────────────┐
│ market       │ ──────────────> │ decision     │
│ OKX market   │                 │ strategy/risk│
│ features     │ <────────────── │ targets      │
└──────┬───────┘                 └──────┬───────┘
       │                                │
       │                                ▼
       │                         execution.order_intents
       │                                │
       ▼                                ▼
┌──────────────────────────────────────────────────────┐
│ execution                                             │
│ account read / order manager / OKX adapter / fills    │
│ portfolio / ledger / reconciliation / recovery        │
└──────────────────────────────────────────────────────┘
```

| 主交易 Role | 入口 | Slice | 启动的关键后台任务 |
| --- | --- | --- | --- |
| `gateway` | `apps/api_gateway/main.py` | API、Operator、只读查询、控制面代理 | FastAPI lifespan、必要的控制面 client |
| `market` | `apps/market_gateway/main.py` | OKX public market、feature producer | market WebSocket、REST fallback、stream cache flush |
| `decision` | `apps/decision_engine/main.py` | decision trigger、strategy、policy/risk | feature consumer、decision loop |
| `execution` | `apps/execution_engine/main.py` | account、execution、portfolio、ledger、reconciliation | OKX private/account WS、account refresh、execution sync、outbox flush、command processor、reconciliation |

四个交易 role 之外还有支持进程：所有标准 profile 都包含 `aats-rdp-daemon`；`derivatives-live` overlay 还会启动 `aats-liquidations-daemon` 和 `aats-microstructure-collector`。因此“四进程”只表示主交易切片数，不表示完整部署只有四个应用容器。单进程 monolith 仍用于兼容/兜底；标准 live 路径是四个主交易切片加相应守护进程。

## 3. Runtime 构建

核心 composition root：`aats/bootstrap/config.py::build_runtime()`

主要步骤：

1. `start_api.py` 或容器入口加载选定 profile 的 `.env.*`。
2. `load_settings()` 合并代码默认值、managed profile 基线、`configs/strategy_profiles/<profile>.yaml` 和允许覆盖的环境变量；managed profile 派生的身份字段即使出现在环境文件中也会被忽略并记录日志。strategy YAML 必须是 mapping，runtime defaults/YAML 任一 key 不属于 `AATSSettings.model_fields` 时在 runtime 构建前失败。
3. 构建 storage backends：
   - memory：本地开发。
   - postgres：live / 多进程。
4. 启动 event bus：
   - `InMemoryEventBus`：单进程。
   - `HybridEventBus` / `NatsEventBus`：跨进程。
5. 构建 shared slice：settings、bus、storage、hot state、kill switch、runtime mode。
6. `runtime_profile_resolution()` 保持 `env_only`，不再从旧的 profile 管理控制面改写 settings。
7. 从 Postgres `governance.active_parameter_sets` 加载 active parameters，并覆盖对应策略字段；数据库不可用时 fail-soft 为空集，退化到 profile 参数，不读取 JSON fallback。
8. 按 role 构建 market / decision / execution / operator slice。
9. 执行 live startup guards、注册 event subscriptions 并启动后台任务。

这里的 active-parameter loader 以 `family + timeframe` 处理 combo 参数，不等价于 profile recommendation 的运行时激活协议。Phase 3M 后，profile recommendation 的 apply/rollback 路由都在授权与状态校验后无写入 `501`：历史 cross-DB Saga 查询的 `profile_id` 与现行 activation model 的 `active_profile_id` 不一致，且没有 execution-owned generation、目标 worker ack 或内存态 readback。approve/release 只能作为研究治理事实，不能作为运行参数已生效的证据。

live exchange runtime hardening 当前会拒绝以下配置：

- 非 OKX execution/account backend。
- 未启用 account read。
- 非 Postgres storage。
- 缺少 database URL。
- 未启用 single runtime guard。
- 缺少 OKX 凭证。
- 未启用 Operator auth。
- 启用 unsafe unauthenticated write。
- 非开发模拟环境下 session cookie 不安全。

## 4. 事件主题与主链路

| Topic | Producer | Consumer | 语义 |
| --- | --- | --- | --- |
| `market.snapshots` | market gateway | feature engine、远端 market cache | 标准化行情快照 |
| `features.snapshots` | feature engine | decision trigger | 策略特征 |
| `decision.position_targets` | decision/strategy | position target handler | 目标仓位 |
| `execution.order_intents` | target handler | order manager | 执行意图 |
| `execution.order_updates` | order manager/outbox | audit/operator/reconciliation | 订单状态 |
| `execution.fill_events` | order manager/outbox | portfolio、audit | 成交事实 |
| `portfolio.snapshots` | portfolio service | reconciliation、audit、AI/operator | 组合快照 |
| `reconciliation.reports` | reconciliation service | operator/blocker | 对账报告 |

NATS redelivery 意味着所有 critical consumers 必须幂等。当前 fill/portfolio consumer 仍需加强串行化和 DB-first 幂等。

当前 JetStream 是三条 stream，而不是旧文档中的两条：

| Stream | Retention | 默认上限 | 用途 |
| --- | --- | --- | --- |
| `AATS_EVENTS_MARKET` | limits，1 天 | 2 GiB | 高频 market/feature snapshots |
| `AATS_EVENTS` | interest，1 天兜底 | 4 GiB | 其余可由热缓存/数据库恢复的跨进程事件 |
| `AATS_EVENTS_COMMANDS` | limits，1 天 | 512 MiB | 7 类不可丢的交易/分配命令 |

`audit.records` 不进入 JetStream，而是直接持久化到 Postgres `event_store`。三条 stream 的声明上限合计 6.5 GiB，NATS server 文件存储上限为 8 GiB。

四主进程使用 NATS/hybrid 时，consumer 注册后必须通过 Redis peer readiness barrier 才能启动 publisher。Phase 3J 后该 barrier 使用部署代次键 `aats:runtime:ready:<generation>:<role>`；announce/poll 异常、peer 超时、payload role/generation 错配或缺少 generation 都会在 background task 前失败。monolith/in-memory 不需要该跨进程 barrier。ready key 表示本代次 consumer 已 provisioning，不是持续 liveness lease。

Kill Switch 的长期 Redis state 是重启恢复权威，不再单独构成增险许可。Phase 3L 后，Gateway/monolith 必须为当前 RUNNING generation 维护 `aats:hot:system:kill_switch_permission:<generation>`：TTL 固定 15 秒、每 5 秒续租；execution 最终提交同时校验长期 authority 与同 generation permission，且没有续租权。四进程代理 resume 由 execution 完成对账/长期 authority 写入后，还必须由 Gateway 重读 exact generation 并成功签发 permission 才向 Operator 返回成功。halt/shutdown 尽力撤销前一 RUNNING generation，失败则由 Redis TTL 收敛。该 permission 不参与恢复、不替代 operator resume，也不阻止验证后的 reduce-only/cancel。真实 Redis/NATS 分区与目标时间界仍未验证。

进程运行期另有关键 task supervisor。Phase 3K 后，账户刷新、执行同步、对账、execution outbox、execution command flow、Phase 1 shadow 和 trial guard 七条固定周期任务必须在 `max(60s, 3 × interval)` 内成功完成一轮，否则分类为 `stalled`，daemon 停止 heartbeat 并非零退出，FastAPI health 返回 `503`。WebSocket、decision dispatcher 等事件驱动任务不使用统一静默阈值；其连接/freshness/queue-lag 以及 event-loop 整体阻塞和目标容器行为仍属 FS-006 未闭环范围。

## 5. 订单生命周期

```text
PositionTarget
  -> PolicyDecision
  -> RiskDecision
  -> ExecutionPlan
  -> OrderIntent
  -> OrderManager.handle_order_intent
  -> reservation / command / outbox
  -> ExecutionCommandProcessor
  -> adapter.submit
  -> OrderState + FillEvent
  -> obligation consume/release
  -> portfolio / ledger / reconciliation
```

典型状态：

| 状态 | 含义 |
| --- | --- |
| `CREATED` | 本地已接受/准备执行 |
| `SUBMITTING` | 正在提交或命令已进入队列 |
| `SUBMITTED` / `ACCEPTED` / `PARTIALLY_FILLED` | 交易所已接收或部分成交 |
| `FILLED` | 完全成交 |
| `CANCELED` / `EXPIRED` | 订单终止，未完全成交 |
| `FAILED` / `REJECTED` / `BLOCKED` | 本地/交易所/风控失败 |

维护要求：

- 每个订单状态变更都应能通过 order state、event/outbox、audit/operator 视图追踪。
- submit/cancel 命令必须具备幂等键和可恢复状态。
- terminal order、fills、obligation、ledger 的一致性应通过测试和 reconciliation 持续验证。

## 6. 成交、费用与账务语义

`FillEvent` 是 portfolio 和 ledger 的关键事实输入。

| 字段 | 语义 |
| --- | --- |
| `fill_qty` | 成交数量，Decimal |
| `fill_price` | 成交价格，Decimal |
| `fee_amount` | signed fee；正数代表费用成本，负数代表返佣 |
| `fee_currency` | 费用币种；缺失时由 fee resolver/accounting fallback 处理 |
| `liquidity_role` | maker/taker 等流动性角色 |

重要不变量：

- adapter 不得改变 fee sign。
- ledger/settlement 使用 signed fee 区分 `fill_fee_expense` 和 `fill_fee_rebate`。
- portfolio 可用余额更新必须和 ledger 结算方向一致。

维护要求：交易所适配器、portfolio、ledger 和 settlement 必须共享同一套 fee sign 语义。

## 7. 资金预留与余额一致性

当前预留路径：

```text
OrderIntent
  -> ExecutionObligationService.preview_reservation_for_intent
  -> account snapshot available balance
  -> subtract active obligations
  -> create OrderObligation
  -> persist obligation
  -> submit command/order
  -> FillEvent consumes obligation
  -> terminal state releases remaining obligation
```

当前实现的保护：

- 单进程内有 `asyncio.Lock`。
- Postgres reservation repo 的 consume/release 使用 `FOR UPDATE`。
- RiskEngine 会读取 exchange snapshot 和本地 active obligations 的部分视图。

目标状态：

```text
DB transaction
  lock(account_id, product_type, margin_mode, currency)
  read authoritative available/reserved
  create/update obligation
  create ledger reservation
  enqueue command/outbox
commit
```

## 8. Portfolio 与 Ledger

Portfolio service 当前维护热状态：

- balances
- positions
- avg entry
- realized PnL
- applied fill ids
- fill outcome
- portfolio snapshots

Ledger/settlement 当前维护审计状态：

- ledger accounts
- journals / entries
- reservations
- settlements
- fee expense/rebate
- no-reservation settlement fallback

关键边界：

- portfolio 是交易运行热状态，不应成为最终账务真源。
- ledger/settlement 应成为可审计财务真源。
- reconciliation 用于发现偏差，不能代替交易路径原子性。

维护要求：任何 fill replay、redelivery 或恢复流程都必须保持 portfolio 与 ledger 幂等。

## 9. Reconciliation 与 Recovery

Reconciliation 输入：

- portfolio snapshots
- local order/fill state
- ledger/reservation/settlement
- exchange account snapshot
- exchange open orders / fills

Recovery 输入：

- startup persisted state
- execution command states
- exchange open orders
- recent terminal local orders
- reconciliation reports
- blocker/kill switch state

关键行为：

- stale `SENT` submit 不盲目重放，避免重复下单。
- startup recovery 会对 stuck sent submit 触发 blocker/halt。
- cancel command 可重试。

维护要求：ambiguous submit/cancel 状态必须进入可审计的人工恢复路径，不能静默忽略。

## 10. Operator API 与安全边界

认证模型：

- session cookie：HMAC-SHA256 签名、过期时间、session version。
- API key：读 key / 写 key，live 写权限受限制。
- Operator role：viewer / operator / admin；具体读写权限由认证依赖按端点执行。
- 登录的同步 repository、PBKDF2、账户状态和审计链在有界 thread worker 中执行，不占用 event loop；每进程默认 concurrency 4、queue timeout 1 秒。
- 登录前有每进程 global/client/identity 滑动窗口限流，默认 60 秒内 60/20/10；client 只使用 ASGI socket peer，不信任 forwarding header。
- 不存在/禁用用户与损坏 hash 执行固定 dummy PBKDF2；username/password 分别限 128/1024 字符，密码由 `SecretStr` 承载。

live hardening：

- exchange-coupled runtime 必须启用 Operator auth。
- unsafe unauthenticated write 在 live hardening 中被拒绝。
- `/healthz` 是 liveness，不代表 trading ready。
- 写操作和 admin 操作通过 dependency 保护。

网络边界：

- 容器内 Gateway 监听 `0.0.0.0` 仅用于 Docker network；宿主 published port 固定到 `127.0.0.1`。
- 本地 `start_api.py` 只允许模拟 profile 与 loopback host，不能作为 live 或远程裸 HTTP 入口。
- Gateway 最外层 user middleware 仅接受 `127.0.0.1`、`localhost`、`::1` 和测试主机；未信任/畸形 Host 在路由前返回 400。
- 同一中间件覆盖 CSP、frame、MIME sniffing、referrer、permissions、COOP/CORP 头；HSTS 只依据实际 HTTPS ASGI scope，不信任客户端 `X-Forwarded-Proto`。
- 远程 Operator 访问需要另行设计受控 proxy/VPN/mTLS；当前没有用静态绑定替代目标主机防火墙、证书或路由验证。

当前未确认发现 live auth bypass。需要继续保持测试覆盖。

上述登录保护只属于单进程代码契约，不是生产分布式控制。多 Gateway worker、进程重启、
trusted proxy client identity、Redis/proxy 集中限流、慢 DB/连接池耗尽和目标 p95/p99/
event-loop lag 仍需隔离生产等价验证；未完成前 FS-019 保持 OPEN。

## 11. RDP 与 Active Parameters

RDP 与主交易链路分离：

```text
historical data
  -> replay / research
  -> attribution / execution realism
  -> governance / recommendation
  -> pre-apply gate
  -> active parameter set（Postgres）
  -> build_runtime 加载参数
```

离线 backtest 有两个必须同时记录的模型契约：`next_bar_event_v2` 约束完整 K 线
决策只能在下一可交易事件解析，`ohlcv_participation_cap_v2` 用已知 bar volume 的
默认 1% participation cap 约束 IOC/post-only/bounded-limit 并把 fee 与 fixed
slippage 纳入成本。后者只是一种 OHLCV proxy，不建模 L2 depth、spread、queue
position、真实 latency 或 market impact；它不能支撑 live 容量/收益外推。

关键边界：

- 主交易行情不读取 RDP Bronze/Silver/Gold。
- RDP 参数只有通过受保护的 apply/release 路径写入数据库后才影响 runtime。
- `governance.active_parameter_sets` 是 runtime active parameter 的唯一真源；`configs/active_parameter_sets/` 只保留兼容/审计用途，runtime 不做 JSON fallback。
- `release_cycle` 与 `decision_cycle` 的调度配置当前禁用，其中 `release_cycle` 还被任务队列显式冻结；不得把历史文档中的自动发布描述当成当前行为。
- 缺少上述两个 model version 或现实性限制元数据的旧回测 artifact 不是当前资本证据，必须失效并重跑。

生产参数变更必须能追踪 recommendation、gate、release、actor、apply history 和 rollback target。

## 12. 数据库与迁移

当前优点：

- 金融数量/金额/价格使用 `Numeric(36,18)`。
- order state 有 optimistic `row_version`。
- fill、journal、reservation、execution command 有关键唯一约束。
- ledger repo 使用 advisory lock。
- reservation repo 的 consume/release 使用行锁。

当前 Phase 3E 工作区已将 schema 写入所有权移到部署期一次性 job：主交易 root migration 使用 `schema_migrations`，RDP Batch B 使用 `governance.rdp_schema_migrations`，两者都保存 version/checksum；managed 应用启动只读校验，不执行 DDL。Gateway 在任何 readiness 或后台任务前校验 RDP contract。

Phase 3U 又把 SQLAlchemy pool ceiling 集中到 `aats/storage/connection_budget.py`。四进程主
storage 按 gateway/market/decision/execution 分别限制为 32/8/10/16；加上当前声明的
RDP、collector、governance、orderbook 和 startup pools 后，完整声明 topology ceiling
为 150。Compose 的普通连接容量为 `200 - 3 = 197`，名义余量 47，并由静态 verifier/CI
阻止裸 pool 数字、新增未归类 engine 和容量算术漂移。

这不是全局运行时 semaphore，也不是容量验收。governance transient、并行 `NullPool`
命令、迁移/恢复/admin、仓库外进程、慢查询/泄漏/重连峰值和 `work_mem` 联合内存风险仍需
在生产等价隔离栈验证，因此 FS-008 保持部分整改。

Phase 3V 将 Research Factory real-data candidate 的开发选择固定为 train/valid 两段独立
评估且同时通过，valid 是 metrics benchmark。test 仍参与 dataset integrity/quality gate，
但不进入 factor、label、绩效 metrics 或 candidate selection gate；它以 `rfseg_` 内容 seal
和 `sealed_not_evaluated` 状态进入 evidence
lineage；外部 execution summary 必须精确绑定 valid segment，不能以全窗口间接消费 test。
该架构边界防止当前 v2 runner 用 test 选候选，但没有实现最终一次性 OOS、
holdout access ledger、purged walk-forward 或历史污染审计，不能据此放行生产参数。

仍未完成的架构门禁：空库、历史克隆和部分失败库尚未导出并比较完整 table/column/type/default/index/constraint/view/function manifest，也没有经验证的 app+schema 一致回滚。因此不得把 ledger 与单元测试通过等同于生产 schema 已一致。

## 13. 架构不变量

以下不变量应被代码、测试和 runbook 同时维护：

1. `FillEvent.fee_amount` 保留 signed fee 语义。
2. 同一 `intent_id` 和 client order id 只能产生一个有效执行链。
3. 下单前资金预留必须跨进程原子化。
4. 任意 fill 重放必须幂等。
5. portfolio 热状态必须按 scope 串行应用 fill。
6. terminal order 与 fills/ledger/obligation 不得永久不一致。
7. kill switch/risk/cooldown block 必须有 durable audit record。
8. live profile 必须 fail closed。
9. RDP 参数变更必须有 approval、gate、actor 和 history。
10. reconciliation 发现偏差后，应阻断或降级后续自动执行。

## 14. 维护索引

| 主题 | 文档 |
| --- | --- |
| 部署与运行 | `DEPLOYMENT.md` |
| 配置归属 | `configs/README.md`、`docs/configuration/README.md` |
| RDP 模块 | `aats/data_platform/README.md`、`docs/rdp/README.md` |
| Operator 运维 | `docs/operations/README.md`、`docs/operations/operator_checklist.md` |
| 测试与上线前验证 | `docs/testing/README.md` |
| WSL2 基础设施 | `deploy/wsl2-dev/README.md`；`deploy/wsl2-dev/RUNBOOK.md` 仅为历史实跑记录 |
