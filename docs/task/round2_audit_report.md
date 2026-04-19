# Round 2 Audit Report

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 1. Executive Risk Summary
本轮深审后的总体判断是：该系统在 `local_demo` 和受控 paper/demo 场景下具备较好的可观察性与回放意识，但对于任何接近生产的交易执行场景，当前实现仍处于高风险状态。最严重的问题不在策略本身，而在执行状态收敛、账户义务跟踪、跨存储一致性和多作用域状态隔离。

最严重的风险主题有四类：
- 交易所提交后或同步过程中发生瞬时查询失败时，系统会把订单直接落成终态 `FAILED`，并停止继续跟踪。这会制造“交易所仍有活动订单，本地却认为已经失败”的危险状态。
- 风控和提交门控依赖缓存的交易所快照，没有本地 reserved/frozen 资金模型。短时间内连续决策时，系统可能在本地未感知前序义务的情况下继续下单。
- 账户快照、对账和基线逻辑对多符号/多作用域存储并不完整，部分代码仍然使用全局“最新基线”而不是按 scope 取值，可能污染恢复和对账判断。
- 订单、成交、事件、审计之间没有原子提交边界。任何一步持久化或事件派发失败，都可能留下“仓储已提交但事件链不完整”或“事件已落库但下游状态未更新”的中间态。

当前最可能导致财务损失、状态损坏或重大运维事故的点是：
- 未被继续跟踪的真实/模拟交易所活动订单
- 未被本地预留模型覆盖的重复提交或超额占用
- 对账/恢复使用错误 scope 的基线或不完整账户视图
- 事件链与业务状态链分叉，导致 replay/audit 不能可靠重建事故

需要说明的是：代码中对真实资金 live trading 有结构性阻断，这降低了当前仓库的直接资金损失暴露面；但这些缺陷一旦迁移到真实撮合/真实出入金环境，会直接升级为资金与账务风险。

## 2. High-Severity Findings

### Exchange Lookup Failures Collapse Potentially Live Orders Into Terminal `FAILED`
- Classification: Confirmed defect
- Severity: Critical
- Affected module(s) / workflow(s): `OKXExecutionAdapter.submit`, `OKXExecutionAdapter.sync`, order lifecycle, recovery
- Why this matters in a trading system: 下单成功但后续查询失败是交易系统中的常见瞬时故障。如果本地把这种“不确定状态”错误地归类为终态失败，就会停止跟踪一个实际上仍在交易所活动的订单，导致重复下单、漏记成交、错误恢复和错误对账。
- Evidence from code:
  - [aats/services/execution_engine/okx_adapter.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/execution_engine/okx_adapter.py:143) 在 `place_order()` 成功后立即继续调用 [aats/services/execution_engine/okx_adapter.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/execution_engine/okx_adapter.py:161) `_load_order_detail()` 和 [aats/services/execution_engine/okx_adapter.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/execution_engine/okx_adapter.py:182) `get_fills()`
  - 任何异常都会落入 [aats/services/execution_engine/okx_adapter.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/execution_engine/okx_adapter.py:198) 的通用 `except`，返回 [aats/services/execution_engine/okx_adapter.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/execution_engine/okx_adapter.py:574) `_failed_state()`
  - `_failed_state()` 明确写入 `status="FAILED"` 且 `exchange_order_id=None`，见 [aats/services/execution_engine/okx_adapter.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/execution_engine/okx_adapter.py:584)
  - 同样的问题存在于 `sync()`，任何异常都把 open order 刷成 `FAILED`，见 [aats/services/execution_engine/okx_adapter.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/execution_engine/okx_adapter.py:355)
  - 一旦状态为 `FAILED`，仓储层就不再把它视为 open order，见 [aats/storage/execution_repo_postgres.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/storage/execution_repo_postgres.py:140)
  - 运维补救 `resolve_stuck_submission` 只允许 `CREATED`/`SUBMITTING`，明确不接受 `FAILED`，见 [aats/services/operator/query_service.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/operator/query_service.py:48) 和 [aats/services/operator/query_service.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/operator/query_service.py:1608)
- Failure scenario or exploitation path:
  - `place_order()` 成功返回 `ordId`
  - 随后的 `get_order()` 或 `get_fills()` 因网络波动、交易所暂时不可用、429/5xx 或序列一致性延迟失败
  - 本地订单被记为 `FAILED` 且丢失 `exchange_order_id`
  - 后台 `sync()` 不再追踪该订单；人工 stuck-submission 也不能处理
  - 实际订单继续在交易所成交，本地却可能继续发起新的目标单
- Realistic impact:
  - 未跟踪活动订单
  - 重复开仓/平仓
  - 漏记成交和错误组合状态
  - 对账进入 hard mismatch，恢复逻辑难以自动收敛
- Recommended remediation:
  - 引入显式“不确定提交/不确定同步”状态，例如 `SUBMIT_ACK_UNKNOWN` / `SYNC_UNKNOWN`
  - `place_order()` 一旦拿到 `ordId` 或 `clOrdId`，必须保留并持久化，不得在后续查询失败时丢弃
  - `sync()` 的异常不得把 open order 直接收敛为终态 `FAILED`；应保留原状态并记录同步错误
  - 为“提交成功但查询失败”补一条恢复路径：按 `clOrdId`/`ordId` 持续重查，直到确认终态
- Fix Priority Rationale: 这是会直接制造未受控真实订单的执行层缺陷，影响资金状态和恢复正确性，应先于其他优化项修复。

### Account Snapshot Visibility Is Limited to `default_symbol`, Breaking Safety Checks for Additional Symbols
- Classification: Confirmed defect
- Severity: High
- Affected module(s) / workflow(s): `OKXAccountService.refresh`, risk gating, reconciliation, operator account visibility
- Why this matters in a trading system: 如果系统在配置层允许多个交易符号，但账户读取只采集默认符号的 open orders/fills，那么非默认符号的未完成义务将对风控、对账、恢复和运维界面不可见。
- Evidence from code:
  - 设置层明确支持多个允许交易符号，见 [aats/bootstrap/settings.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/bootstrap/settings.py:124)
  - runtime scope 也是按 `allowed_symbols` 构造的，见 [aats/services/runtime_scope.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/runtime_scope.py:32)
  - 但账户刷新只调用 `get_open_orders(symbol=self.settings.default_symbol)` 与 `get_fills(symbol=self.settings.default_symbol, ...)`，见 [aats/services/execution_engine/okx_account.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/execution_engine/okx_account.py:49) 和 [aats/services/execution_engine/okx_account.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/execution_engine/okx_account.py:50)
  - 风控与提交门控又依赖 `open_order_count(symbol=intent.symbol)`，见 [aats/services/governance_engine/risk.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/governance_engine/risk.py:56) 和 [aats/services/execution_engine/okx_adapter.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/execution_engine/okx_adapter.py:411)
- Failure scenario or exploitation path:
  - `allowed_symbols` 配置了多个符号
  - 默认符号之外的订单已在交易所挂出或成交
  - 本地账户快照未包含这些 open orders/fills
  - 风控继续认为该符号 open order 数量为 0，对账也只看默认符号的交易所视图
- Realistic impact:
  - `max_open_orders` 在非默认符号上失效
  - 对账和恢复视图漏掉真实订单/成交
  - operator API 给出错误账户状态
- Recommended remediation:
  - 如果系统只打算支持单符号，启动时强制 `allowed_symbols == (default_symbol,)`
  - 如果要支持多符号，账户刷新必须遍历 `allowed_symbols` 或拉取全量 open orders/fills，并带上分页/游标策略
  - 对账和 operator 视图应明确标识“账户视图覆盖范围”
- Fix Priority Rationale: 这是配置层已暴露能力与运行时真实能力不一致的问题，一旦多符号配置启用，会直接削弱风控和对账。

### Cached Exchange Balances and Missing Local Reservation Allow Over-Commitment
- Classification: High-confidence risk
- Severity: High
- Affected module(s) / workflow(s): risk control, order submission, balance integrity
- Why this matters in a trading system: 资金/仓位义务不能只依赖“上一次交易所快照”。在订单提交到交易所但快照尚未刷新前，本地必须先行预留资金和挂单容量，否则多个决策可以在同一份旧快照上重复通过。
- Evidence from code:
  - `PortfolioState` 只有总余额 `balances`，没有本地 `available/frozen/reserved` 结构，见 [aats/services/portfolio_service/positions.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/portfolio_service/positions.py:45)
  - 风控直接读取交易所快照的 `available` 余额，见 [aats/services/governance_engine/risk.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/governance_engine/risk.py:75) 到 [aats/services/governance_engine/risk.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/governance_engine/risk.py:99)
  - `handle_position_target()` 只做非强制刷新，见 [aats/bootstrap/config.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/bootstrap/config.py:526)
  - `OKXAccountService.refresh()` 在快照年龄小于 `okx_account_refresh_interval_seconds` 时直接返回缓存，默认 15 秒，见 [aats/services/execution_engine/okx_account.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/execution_engine/okx_account.py:41) 和 [aats/bootstrap/settings.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/bootstrap/settings.py:96)
  - 提交门控也只看 `account_service.open_order_count()` 和缓存账户状态，见 [aats/services/execution_engine/okx_adapter.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/execution_engine/okx_adapter.py:411)
- Failure scenario or exploitation path:
  - 第一个决策在旧快照上通过，并提交订单
  - 交易所余额/挂单尚未反映到下一次账户刷新
  - 第二个决策在同一份缓存快照上再次通过
  - 本地对 quote/base 资金和 open-order budget 没有任何先行占用
- Realistic impact:
  - 重复占用可用余额
  - 短时间内超过 `max_open_orders`
  - spot 下 overspend，derivatives 下 margin over-commit
  - 之后只能依靠交易所拒单或对账来兜底
- Recommended remediation:
  - 在本地建立明确的 reservation/frozen 模型，并在 `OrderIntent -> OrderState(FILLED/CANCELED/FAILED)` 生命周期内维护
  - 风控检查应基于“交易所可用余额 + 本地未结义务”的统一视图
  - 对受 exchange-coupled 模式，提交前刷新可保持，但不能替代本地 reservation
- Fix Priority Rationale: 这是资金义务控制层的核心缺口；即使交易所最终拒单，也会制造本地决策与实际账户状态脱节。

### Order, Fill, Event, and Audit Persistence Are Not Atomic
- Classification: High-confidence risk
- Severity: High
- Affected module(s) / workflow(s): persistence, replay, auditability, recovery
- Why this matters in a trading system: 订单状态、成交记录、事件流和审计链必须要么一起成功，要么一起失败。否则系统会得到“数据库状态”和“事件历史”不一致的半提交状态，回放和事故调查都不可靠。
- Evidence from code:
  - `save_order_state()` 和 `save_fill()` 各自独立 `session.commit()`，见 [aats/storage/execution_repo_postgres.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/storage/execution_repo_postgres.py:76) 和 [aats/storage/execution_repo_postgres.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/storage/execution_repo_postgres.py:110)
  - `OrderManager` 先持久化仓储，再发布事件，见 [aats/services/execution_engine/order_manager.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/execution_engine/order_manager.py:170) 到 [aats/services/execution_engine/order_manager.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/execution_engine/order_manager.py:213)
  - event store 自己也单独 `commit()`，见 [aats/storage/event_store_postgres.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/storage/event_store_postgres.py:44)
  - event bus 是“先落事件，再顺序调用 handler”；后续 handler 抛异常时，没有补偿或重试隔离，见 [aats/bus/memory_bus.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/bus/memory_bus.py:23) 到 [aats/bus/memory_bus.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/bus/memory_bus.py:42)
- Failure scenario or exploitation path:
  - 订单状态已成功提交到 `order_states`
  - 事件落库失败、后续 handler 失败，或者进程在两者之间崩溃
  - 最终出现“订单存在，但 `ORDER_UPDATES` 事件缺失”或“`FILL_EVENTS` 已存在，但 portfolio/reconciliation 未跟上”
- Realistic impact:
  - replay 无法完整重建
  - 审计链断裂
  - 恢复逻辑依赖的最近 reconciliation/audit 可能与仓储真实状态脱节
- Recommended remediation:
  - 对关键 financial state 采用 outbox/inbox 模式或单事务写入
  - 明确区分“source of truth”与“derived projections”
  - 对 subscriber failure 引入重试队列或 dead-letter 机制，而不是把当前调用栈直接撕裂
- Fix Priority Rationale: 当前系统高度依赖 event store 和 replay 来解释状态，一旦链路分叉，恢复和审计能力会一起失效。

### Baseline and Recovery Logic Use Global Latest Baselines Instead of Scoped Baselines
- Classification: Confirmed defect
- Severity: High
- Affected module(s) / workflow(s): reconciliation, recovery, rebaseline history, multi-scope persistence
- Why this matters in a trading system: baseline 是恢复和对账的信任锚。如果 spot、derivatives、不同 symbol 组或不同 runtime profile 共用存储，而 baseline 取值是“全局最新”，系统就可能拿错误的基线去解释当前作用域。
- Evidence from code:
  - `ReconciliationService._accepted_exchange_fill_ids()` 直接取 `event_store.latest(topics.ACCOUNT_BASELINES)`，见 [aats/services/reconciliation_service/repair.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/reconciliation_service/repair.py:220)
  - `OperatorQueryService.latest_account_baseline()` 同样直接取全局最新 baseline，见 [aats/services/operator/query_service.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/operator/query_service.py:147)
  - `rebaseline()` 的 `previous_baseline_ref` 也是全局最新 baseline，见 [aats/services/operator/query_service.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/operator/query_service.py:1258)
  - 但 `AccountBaselineSnapshot` schema 本身没有 `product_type` / `margin_mode` 字段，见 [aats/schemas/exchange.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/schemas/exchange.py:88)
- Failure scenario or exploitation path:
  - 同一数据库中先后保存 spot 与 derivatives 的 baseline
  - 当前 derivatives runtime 执行 reconciliation/rebaseline
  - 代码读取到的是最近的 spot baseline
  - `accepted_exchange_fill_ids`、`previous_baseline_ref`、operator 视图都建立在错误 baseline 上
- Realistic impact:
  - 错误地接受/忽略 exchange fills
  - 错误的 baseline lineage
  - 恢复/对账报告误导 operator
- Recommended remediation:
  - 给 `AccountBaselineSnapshot` 增加 scope 字段：`product_type`、`margin_mode`、`allowed_symbols` 或 `scope_id`
  - 所有 baseline 读取必须改为 scoped lookup
  - 旧数据迁移时补 scope 推断或强制隔离存储
- Fix Priority Rationale: baseline 是恢复与人工决策的信任根，不能继续以全局 latest 这种弱条件驱动。

## 3. Medium- and Lower-Severity Findings

### `resolve_stuck_submission` Bypasses the Bus and Leaves Audit Linkage Incomplete
- Classification: Confirmed defect
- Severity: Medium
- Affected module(s) / workflow(s): operator recovery, auditability, replay
- Why this matters in a trading system: 敏感恢复动作不仅要改状态，还要进入完整审计链。否则 incident reconstruction 无法解释“为什么订单突然从 pre-submit 变成 FAILED”。
- Evidence from code:
  - `resolve_stuck_submission()` 直接调用仓储并通过 `_append_event()` 追加 `ORDER_UPDATES`，见 [aats/services/operator/query_service.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/operator/query_service.py:1173) 到 [aats/services/operator/query_service.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/operator/query_service.py:1178)
  - `_append_event()` 只写 event store，不走 bus，见 [aats/services/operator/query_service.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/operator/query_service.py:1567)
  - 正常 audit linkage 依赖 `ORDER_UPDATES` 经过 bus 触发 `audit_service.handle_order_update`，见 [aats/bootstrap/config.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/bootstrap/config.py:514) 和 [aats/services/decision_engine/audit.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/decision_engine/audit.py:72)
  - replay 审核又要求 `order_state_refs` 对应 `ORDER_UPDATES`，见 [aats/services/reconciliation_service/replay.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/reconciliation_service/replay.py:638) 到 [aats/services/reconciliation_service/replay.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/reconciliation_service/replay.py:645)
- Failure scenario or exploitation path:
  - operator 执行 stuck submission resolution
  - 仓储和 event store 中有新状态
  - 但 decision audit record 不会追加新的 `order_state_ref`
  - replay/audit detail 对该次恢复动作的呈现不完整
- Realistic impact:
  - 事故回放缺链
  - operator 操作后的状态跳变难以审计
- Recommended remediation:
  - 统一通过 `OrderManager` 或专门 recovery publisher 走 bus 发布 `ORDER_UPDATES`
  - 对 operator recovery 动作补专门 audit linkage 和 replay case

### Financial State Uses Binary Floating Point End-to-End
- Classification: Design weakness
- Severity: Medium
- Affected module(s) / workflow(s): accounting, risk math, persistence, reconciliation
- Why this matters in a trading system: 二进制浮点在高频累加、小数 lot size、费用折算和 PnL 重建中会引入不可控误差；对账阈值越小，误报和漏报概率越高。
- Evidence from code:
  - 核心持仓与余额状态全是 `float`，见 [aats/services/portfolio_service/positions.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/portfolio_service/positions.py:20) 到 [aats/services/portfolio_service/positions.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/portfolio_service/positions.py:47)
  - snapshot 与 total equity 计算也是 `float`，见 [aats/services/portfolio_service/snapshots.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/portfolio_service/snapshots.py:68)
  - 数据库存储列使用 SQL `Float`，见 [aats/storage/sqlalchemy_models.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/storage/sqlalchemy_models.py:46) 到 [aats/storage/sqlalchemy_models.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/storage/sqlalchemy_models.py:97)
- Failure scenario or exploitation path:
  - 多次 partial fill、费用折算、反向开仓和重建后累计浮点误差
  - 对账报告在 1e-9 / 1e-12 阈值附近摇摆
- Realistic impact:
  - 误判 divergence
  - PnL、余额和 margin 使用率的边界值失真
- Recommended remediation:
  - 将价格、数量、费用、余额迁移到 `Decimal`
  - 数据库存储改为定点数/字符串精度字段
  - 统一量纲与舍入策略，按 symbol/instrument 精度执行

### Reconciliation Mismatch Repair Is a Stub
- Classification: Design weakness
- Severity: Medium
- Affected module(s) / workflow(s): reconciliation, incident response
- Why this matters in a trading system: 系统已经把 reconciliation 设计成关键安全机制，但 mismatch 后没有自动 remediation，意味着所有实际处置都依赖人工 operator 判断。
- Evidence from code:
  - `ReconciliationRepairService.repair()` 仅是 TODO stub，见 [aats/services/reconciliation_service/repair.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/reconciliation_service/repair.py:35)
- Failure scenario or exploitation path:
  - 系统检测到 mismatch
  - 只保存报告并继续等待人工处理
  - 如果 operator 未及时介入，系统只能保持 blocked/halted，无法自动收敛
- Realistic impact:
  - 恢复流程长时间人工化
  - 夜间无人值守时恢复能力差
- Recommended remediation:
  - 把 repair 明确分成“只读诊断”“自动可安全修复”“必须人工确认”三类动作
  - 至少为“刷新账户视图、重查订单、重建快照”建立自动修复步骤

### Portfolio Snapshot Economics Assume a Narrow Margin/Collateral Model
- Classification: Verification gap
- Severity: Low
- Affected module(s) / workflow(s): total equity, derivatives valuation
- Why this matters in a trading system: 当前快照逻辑把 `total_equity` 建立在 `USDT` 余额、spot marked value 和 derivatives unrealized PnL 之上。若未来接入多 collateral、非 USDT quote、组合保证金，该公式很可能不再成立。
- Evidence from code:
  - `total_equity = balances.get("USDT", 0.0) + spot_marked_value + derivatives_unrealized_pnl`，见 [aats/services/portfolio_service/snapshots.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/portfolio_service/snapshots.py:68)
- Failure scenario or exploitation path:
  - 引入非 USDT collateral 或更复杂账户模型
  - equity 与 risk numbers 失真
- Realistic impact:
  - 风险暴露指标误导
  - reconciliation false positives/negatives
- Recommended remediation:
  - 在扩展产品前先定义正式 collateral/equity 模型，并让 snapshot builder 显式按账户模式分支

## 4. Design and Maintainability Weaknesses
- `Financial invariants are implicit rather than encoded.` 资金守恒、义务预留、终态不可回退、baseline 作用域这些关键约束主要靠调用顺序和 replay 检查，而不是靠类型、事务或领域模型直接保证。
- `The system mixes source-of-truth state and derived projections.` `order_states`、`fill_events`、`portfolio_snapshots`、`reconciliation_reports`、`audit_records` 都在持久化，但没有清晰声明哪一个是主状态、哪一些是可重建投影。
- `Scope discipline is uneven.` 大部分 execution/portfolio/reconciliation 查询已经引入 `runtime_scope`，但 baseline 相关对象仍是全局视图，说明“多作用域持久化”没有被端到端贯彻。
- `Operational recovery logic is spread across many modules.` `okx_adapter`、`recovery`、`repair`、`operator/query_service`、`replay` 共同决定恢复行为，状态枚举与分支多，后续修改非常容易破坏 incident handling。
- `Accounting logic is centralized in mutable in-memory state.` [aats/services/portfolio_service/positions.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/portfolio_service/positions.py) 承担了过多金融语义，且没有账本层与 reservation 层分隔，导致测试和演进都较脆弱。

## 5. Test and Verification Gaps
- 未看到覆盖“`place_order()` 成功，但 `get_order()` / `get_fills()` 失败”的测试；这正是本轮发现的最高风险执行缺陷。
- 未看到覆盖“`sync()` 期间暂时性网络异常不能把 live order 终态化”的测试。
- 未看到针对多符号 `allowed_symbols` 的 OKX account refresh、risk gating、reconciliation 集成测试。当前相关测试基本只围绕 `BTC-USDT` 或单一 symbol。
- 未看到“本地 reservation/frozen 义务”相关测试，因为实现本身缺失。
- 未看到“仓储已提交、事件发布失败、subscriber 失败、进程中途崩溃”这些部分失败场景的测试。
- 未看到针对 Decimal/精度边界的回归测试；现有测试多是小样本 float happy path。
- replay/audit 测试较强，但没有专门覆盖 operator recovery 动作经由非 bus 写入后的 audit completeness。

优先应补的测试：
1. `submit_success_then_lookup_failure_keeps_order_trackable`
2. `sync_transient_failure_does_not_terminalize_live_order`
3. `multi_symbol_account_snapshot_covers_all_allowed_symbols`
4. `pending_local_obligation_blocks_second_order_before_exchange_refresh`
5. `order_state_repo_commit_then_event_publish_failure_is_detected_and_recoverable`
6. `resolve_stuck_submission_updates_audit_linkage`

## 6. Unresolved Uncertainties
- 无法从仓库内确认 OKX `trade/fills` 接口在真实运行中的窗口、分页和一致性语义，因此“fill history 截断”风险无法完全量化。
- 仓库没有真实撮合、清算、出入金或内部转账模块；因此对完整账务闭环的审计结论仅限当前实现边界。
- 本地环境缺少 `orjson` 和 `fastapi`，本轮未能在当前机器上执行完整测试套件；结论主要来自代码检查与测试内容审阅，而非全量自动化执行结果。

## 7. Prioritized Remediation Plan
1. immediate fixes for financial/security/state-corruption risk
   - 修复 OKX adapter：提交后与同步后的查询失败必须进入“未确认/待重试”状态，而不是终态 `FAILED`
   - 在 order state 中持久保存 `ordId`/`clOrdId` 一旦可得就不可丢失
   - 禁止多符号配置，直到账户刷新与 reconciliation 真正支持多符号；或立即实现全量多符号账户采集
2. fixes for concurrency/idempotency/recovery hazards
   - 引入本地 reservation/frozen 资金与 open-order budget
   - 将关键状态写入改造成单事务 + outbox，或显式 source-of-truth/event-projection 分层
   - 给 baseline 增加 scope 字段，并把 baseline 查询全部改为 scoped lookup
3. fixes for observability and auditability gaps
   - 统一 operator recovery 动作经由 bus 进入 `ORDER_UPDATES`/audit linkage
   - 为“未确认订单”“同步失败”“局部提交失败”增加专门指标、日志和 operator views
   - replay 校验中显式检查 operator-induced state transitions
4. refactors for long-term maintainability and correctness
   - 用 `Decimal` 替换核心财务数值
   - 把 portfolio accounting 从单一 mutable object 拆成 ledger/reservation/snapshot 三层
   - 把 recovery/rebaseline/reconciliation 状态机收敛成单一明确的领域状态模型

## 8. Top 10 Things Most Likely to Break in Production
1. 交易所已经接受订单，但本地因为后续查询失败把它记成 `FAILED`，然后再也不跟踪它。
2. 两个相近时间的决策在同一份缓存账户快照上通过，重复占用同一笔可用余额。
3. 非默认 symbol 的挂单和成交根本没被账户快照看到，导致风控和对账误判为安全。
4. 订单状态已经写入数据库，但对应事件或审计链没有写成功，回放时出现断链。
5. `sync()` 一次瞬时异常就把仍在交易所活动的订单终态化。
6. 基线事件跨 spot/derivatives 作用域串用，恢复和 rebaseline lineage 被污染。
7. 多次 partial fill 和费用折算后的 float 误差把 reconciliation 推到错误分支。
8. operator 通过恢复接口改了订单状态，但 audit record 没有链接到该次变更。
9. 一旦 mismatch 发生，系统缺少自动 repair，只能依赖人工判断与手工恢复。
10. 如果未来扩展到非 USDT collateral 或更复杂保证金模型，当前 snapshot/equity 公式会先坏掉。
