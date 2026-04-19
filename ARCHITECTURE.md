# AATS 系统架构与交易链路

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](docs/project_positioning.md)。


最后更新：2026-04-13
适用范围：主交易系统、四进程运行时、订单/成交/资金/账本/对账链路、RDP 参数回灌边界

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

| Role | 入口 | Slice | 启动的关键后台任务 |
| --- | --- | --- | --- |
| `gateway` | `apps/api_gateway/main.py` | API、Operator、只读查询、控制面代理 | FastAPI lifespan、必要的控制面 client |
| `market` | `apps/market_gateway/main.py` | OKX public market、feature producer | market WebSocket、REST fallback、stream cache flush |
| `decision` | `apps/decision_engine/main.py` | decision trigger、strategy、policy/risk | feature consumer、decision loop |
| `execution` | `apps/execution_engine/main.py` | account、execution、portfolio、ledger、reconciliation | OKX private/account WS、account refresh、execution sync、outbox flush、command processor、reconciliation |

单进程 monolith 仍用于开发和兜底，但多进程是 live 推荐拓扑。

## 3. Runtime 构建

核心 composition root：`aats/bootstrap/config.py::build_runtime()`

主要步骤：

1. 加载 profile `.env.*` 和 settings。
2. 合并 managed profile、strategy profile YAML、active parameters、环境变量。
3. 构建 storage backends：
   - memory：本地开发。
   - postgres：live / 多进程。
4. 启动 event bus：
   - `InMemoryEventBus`：单进程。
   - `HybridEventBus` / `NatsEventBus`：跨进程。
5. 构建 shared slice：settings、bus、storage、hot state、kill switch、runtime mode。
6. 按 role 构建 market / decision / execution / operator slice。
7. 执行 live startup guards。
8. 注册 event subscriptions。
9. 启动后台任务。

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
- Operator role：read / write / admin。

live hardening：

- exchange-coupled runtime 必须启用 Operator auth。
- unsafe unauthenticated write 在 live hardening 中被拒绝。
- `/healthz` 是 liveness，不代表 trading ready。
- 写操作和 admin 操作通过 dependency 保护。

当前未确认发现 live auth bypass。需要继续保持测试覆盖。

## 11. RDP 与 Active Parameters

RDP 与主交易链路分离：

```text
historical data
  -> replay / research
  -> attribution / execution realism
  -> governance / recommendation
  -> pre-apply gate
  -> active parameter set
  -> build_runtime 加载参数
```

关键边界：

- 主交易行情不读取 RDP Bronze/Silver/Gold。
- RDP 参数只有 apply 后才影响 runtime。
- active parameter DB 优先，JSON 文件 fallback。

生产参数变更必须能追踪 recommendation、gate、release、actor、apply history 和 rollback target。

## 12. 数据库与迁移

当前优点：

- 金融数量/金额/价格使用 `Numeric(36,18)`。
- order state 有 optimistic `row_version`。
- fill、journal、reservation、execution command 有关键唯一约束。
- ledger repo 使用 advisory lock。
- reservation repo 的 consume/release 使用行锁。

目标：所有主交易表结构变更都应有版本化 migration，并在启动时验证关键表、列、索引、唯一约束、row version 和数值精度。

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
| 配置归属 | `configs/README.md`、`docs/configuration/managed-config-reference.md` |
| RDP 模块 | `aats/data_platform/README.md`、`docs/rdp/module_reference.md` |
| Operator 运维 | `docs/operations/operator_checklist.md` |
| WSL2 基础设施 | `deploy/wsl2-dev/README.md`、`deploy/wsl2-dev/RUNBOOK.md` |
