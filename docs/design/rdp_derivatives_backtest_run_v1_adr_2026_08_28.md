# ADR：RDP `derivatives-backtest-run/v1` 首个衍生品回测纵向切片

> 文档状态：设计提案（Proposed）；未实施、未批准、不可作为运行或晋级依据
> 最后核对：2026-08-28（代码基线 `main@96c772010767`；以本文档所在 HEAD 为准）
> 核对范围：静态代码、测试契约、现行 LF-B 任务书；未执行回测、未验证目标数据、未部署
> 待决策人：待实名任命的 RDP Owner、Independent Risk Reviewer、Data Lineage Reviewer
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO；CAPITAL PROMOTION: NO-GO**

关联任务书：[`../task/rdp_derivatives_phase2_promotion_evidence_producer_p1_sow_2026_08_28.md`](../task/rdp_derivatives_phase2_promotion_evidence_producer_p1_sow_2026_08_28.md)。

## 1. 决策摘要

新建独立的 `derivatives-backtest-run/v1` 内部回测内核，不在现有 SPOT
`backtest-run/v2` harness 上增加 derivatives 分支，也不复用其 SPOT 成交、仓位或权益状态机。
首版只接受以下单一作用域：

| 维度 | v1 冻结值 |
| --- | --- |
| instrument | `BTC-USDT-SWAP` |
| contract | linear、USDT-settled perpetual |
| margin | isolated；单一隔离资金池 |
| position | single-position/net；同一时刻只能 long、short 或 flat |
| strategy combo | `independent_15m` |
| time window | UTC、半开区间 `[start_ts, end_ts)` |
| execution | `next-tradable-event/v1`；禁止 same-bar/same-timestamp fill |
| accounting | Decimal-only、USDT 单币种、逐事件可重算 |
| promotion | 所有 v1 成功结果固定 `capital_promotion_eligible=false` |
| exposure | 仅 Python 内部 API；无 HTTP、CLI、workflow 或 live 接口 |

该切片的目标是先证明时间因果、永续合约记账、崩溃恢复和不可变证据链能够自洽；它不是交易所清算引擎的完整复制，也不是收益、容量或实盘安全证明。只有后续独立 qualification round 能把 exact Step 2/3 候选、source-sealed 数据、instrument snapshot、position-tier/MMR/liquidation-fee schedule snapshot 与一个或多个 child run 锚定；本 ADR 本身不解锁 Phase 6。

## 2. 背景与当前真实边界

当前公共回测 manifest 是 `backtest-run/v2`，其 validator 明确要求 SPOT contract；其
`FillSimulator`、`PositionTracker`、`EquityBuilder` 和风险指标均建立在 SPOT/OHLCV proxy
边界上。把 `SWAP` 作为一个条件分支塞入该 harness，会让 funding、mark/index、保证金、强平和
结算币种继续依赖错误抽象，并可能使 SPOT 证据被误用于 SWAP 晋级。

截至本 ADR 核对时，当前仓库/数据还存在以下硬阻断：

1. 当前 Gold replay bars 不能作为本 ADR 的完整 source seal：已有构建路径会把 funding rate 对齐到 bar，
   但 v1 要求精确 funding settlement event、独立 mark/index event、next-tradable event 及其各自内容身份。
2. 当前 `backtest-run/v2` manifest 的 contract lineage 固定为
   `calculation_contract_only_unverified`；instrument snapshot 的观察来源、生效区间和 source registry
   锚点尚未形成可供资格轮接受的完整证据。
3. 当前 mark/index 数据的完整窗口、原始来源内容封存、时间连续性与 freshness 尚未取得本切片要求的
   managed immutable 证明；close 价格不得代替 mark 或 index。
4. 历史 source content seal 仍可能触发 `historical_gold_source_content_unsealed`；instrument lineage 仍可能触发
   `instrument_snapshot_observation_evidence_unverified`。这些是阻断，不是可忽略 warning。
5. 当前没有 `phase2_qualification_rounds/<round_id>` managed immutable 父锚点，也没有可消费本 v1 child
   manifest 的 Phase 6 exact-chain 实现。
6. 当前 `InstrumentContract` 只定义合约单位算术，不能证明历史时点适用的 position tier、MMR deduction、
   leverage ceiling 或 liquidation fee；当前尚无可接受的 immutable risk-schedule snapshot。仅有 contract
   fingerprint 不能启动本 v1。

因此，即使本内核通过合成 golden vectors，也只能证明代码合同；在上述数据与治理阻断关闭前，不得声称
“可晋级”“可部署”“可产生真实收益”或“符合 OKX 实盘清算”。

## 3. 目标、非目标与安全不变量

### 3.1 目标

- 为一个精确 SWAP/combo 建立独立、确定性、可恢复、逐事件可重算的回测内核。
- 把输入数据、instrument、候选参数、时间窗、政策版本和所有输出绑定到不可歧义的身份。
- 明确定义 fee、funding、PnL、IMR、MMR 与简化强平的单位、公式和符号。
- 生成 manifest-last child artifact，供后续 qualification round 外部锚定。
- 对缺失、陈旧、乱序、重复、非有限值、类型漂移和 lineage 不完整统一失败关闭。

### 3.2 非目标

- 不支持 ETH、SPOT、MARGIN、inverse、dated futures、options、cross margin 或 hedge/long-short mode。
- v1 只执行 source snapshot 明确证明的 tier-1；不跨 tier，不模拟更高 tier、风险准备金、ADL、破产价撮合、
  强平阶梯、组合保证金、期权 Greeks 或交易所私有算法。
- 不声称 L2 queue position、market impact 或真实可成交容量；v1 只消费显式、已封存的 tradable event。
- 不新增 CLI/HTTP/前端入口，不从运行中网络或数据库自行“补数据”。
- 不直接写 recommendation、active parameters、Phase 6 或 live runtime。

### 3.3 不变量

1. 任何成功 run 都必须是 `BTC-USDT-SWAP + linear + USDT + isolated + single_position + independent_15m`。
2. 输入和输出所有金额、价格、比率、数量均为有限 `Decimal`；`float`、`bool`、NaN、Infinity 一律拒绝。
3. 任何决策只能在 observation bar 完整关闭后产生；成交只能发生于严格晚于 decision timestamp 的
   第一条合格 tradable event。
4. 缺 mark/index/funding 或 freshness/continuity 不合格时，整个 run 为 blocked；不得用 proxy、插值或
   forward-fill 生成“成功”结果。
5. v1 的 `capital_promotion_eligible` 在 request 中不可配置，在 result、metrics 与 manifest 中必须恒为
   `false`；读到 `true` 视为 artifact corruption。
6. final output directory 不可覆盖；没有完整且校验通过的最后一个 `manifest.json` 就不是可消费 child run。
7. Instrument contract 与 position-tier/MMR/liquidation-fee schedule 是两个强制、独立、不可变的 parent；
   任一缺失、失效或不覆盖整个窗口都在第一条经济事件前阻断。

## 4. 组件边界与代码组织

建议新增包 `aats/data_platform/replay/derivatives_backtest/`：

| 模块 | 唯一职责 |
| --- | --- |
| `contracts.py` | frozen request/event/result/ledger 类型及严格验证 |
| `event_source.py` | exact `derivatives-event-set/v1` 的稳定读取、预检、cursor 重放与流式身份复核 |
| `event_merge.py` | 单调输入流校验与确定性 k-way merge |
| `engine.py` | 纯 `reduce_event(state,event)`、队列和 single-position 状态迁移；不做 I/O |
| `accounting.py` | Decimal fee/funding/PnL/margin/liquidation 纯函数 |
| `freshness.py` | mark/index/funding schedule、age、gap 门禁 |
| `metrics_v2.py` | 从 hash-bound 明细确定性重算 metrics |
| `orchestrator.py` | 两遍事件集验证、流式执行、checkpoint 与 completed/blocked 分流 |
| `publisher.py` | staging sink、rolling hash/size、manifest-last、原子发布与独立 diagnostic 发布 |
| `recovery.py` | exact checkpoint 验证与恢复；禁止 latest fallback |

允许复用 `InstrumentContract`、`InstrumentContractSnapshot`、
`canonical_decimal_identity()`、typed JSON identity 及纯 strategy adapter 接口。禁止导入现有 SPOT
`backtest.harness`、SPOT `FillSimulator`、`PositionTracker`、`EquityBuilder` 或其 manifest writer。
共享只允许发生在无产品语义的 primitive 层；若日后提取公共 hash/file 工具，必须先以字节兼容测试证明
不会改变既有 SPOT schema。

## 5. 内部 API 与严格类型

纯领域内核只暴露单事件 reducer：

```python
def reduce_derivatives_event(
    state: DerivativesEngineStateV1,
    event: DerivativeReplayEventV1,
) -> DerivativesReductionV1: ...
```

资源、流式落盘和恢复由唯一 orchestrator 显式协调：

```python
def execute_derivatives_backtest(
    request: DerivativesBacktestRequestV1,
    *,
    event_source: RestartableDerivativeEventSourceV1,
    staging_sink: DerivativesStagingSinkV1,
    resume_ref: DerivativesCheckpointRefV1 | None = None,
) -> CompletedDerivativesRunV1 | BlockedDerivativesDiagnosticV1: ...
```

`RestartableDerivativeEventSourceV1` 不是任意一次性 `Iterable`：它必须由一个已稳定验证的 exact
`derivatives-event-set/v1` 构造，支持按每条流的精确 cursor 重放；`staging_sink` 逐行写 ledger、维护 rolling
hash/size/计数并原子写 checkpoint，orchestrator 不把完整事件或 ledger 留在内存。完成路径由 sink 最后写正式
manifest 并发布；blocked 路径销毁正式 child staging 后，只能发布第 5.4 节的独立 diagnostic。

以上均为内部 API；不得注册到 `aats` 公共 CLI、FastAPI 或 RDP workflow allowlist。内核不创建 DB session、
不访问网络、不读 ambient settings、不读取 `.env.*`；event source 和 sink 不得隐藏网络、数据库或配置读取，
所有父身份、路径和容量政策均由 request 中的不可变引用显式传入。

### 5.1 `DerivativesBacktestRequestV1`

必须是 frozen、extra-forbid 的严格类型，至少包含：

- `schema_version="derivatives-backtest-request/v1"`、`run_id`、`request_created_at`；
- 固定 scope 字段：symbol、instrument type、contract type、settle currency、margin/position mode、family/timeframe；
- exact `InstrumentContractSnapshotRef`：snapshot ID、digest、source registry ID、`effective_from`、`effective_to`；
- exact `PositionTierScheduleSnapshotRef`：snapshot ID、digest/size、source registry ID、每段 effective window、
  tier ID、notional lower/upper bound、max leverage、MMR、maintenance-margin deduction、liquidation fee rate；
  v1 要求整个窗口仅命中一个 `tier_id=1`，任何 mark 或 projected fill 越出 tier-1 upper bound 都以
  `position_tier_out_of_v1_scope` 终止，而不是套用下一档或继续近似；v1 同时要求该档
  `maintenance_margin_deduction=0`，非零时以 `position_tier_deduction_out_of_v1_scope` 阻断，不得忽略或近似；
- exact `ExecutionFeeScheduleSnapshotRef`：maker/taker rate、fee asset=`USDT`、effective window 与不可变身份；
- exact `DerivativesEventSetRefV1`：`derivatives-event-set/v1` manifest 的 ID、路径、digest、size、
  artifact-set fingerprint、总 event-set fingerprint 与每条必需流的 schema/count/canonical event digest、
  coverage、dataset version、transform policy/version 及父 raw partition hashes；
- `start_ts`、`end_ts`、warmup boundary；窗口必须非空且为 UTC 15 分钟边界；
- Step 2/3 round ID、candidate artifact digest/size、parameter typed fingerprint；这些仅是 child lineage，
  不等于 qualification；
- `OpeningAccountStateV1`：`free_cash_before_transfer_usdt`、一次性 `isolated_transfer_in_usdt`、固定 flat
  carry-in position、无 open order；以及 start 前最后一条 source-sealed mark/index cursor；
- `leverage`；MMR、maintenance deduction、liquidation fee 和 maker/taker fee 必须从上述 exact schedule
  snapshot 解析，不允许 request 用自由字段覆盖；不得存在隐藏默认值；
- 固定 policy IDs：event ordering、execution、accounting、freshness、metrics、engine version。

严格解析器必须拒绝未知/缺失 key、重复 JSON key、非 canonical UUID/SHA-256、naive 或非 UTC 时间、
不在有效区间内的 instrument/risk/fee snapshot、非法小数、tick/lot/min 不一致、tier-1 范围错误以及 request
与 source/contract/schedule scope 不一致。

### 5.2 事件封闭联合类型

`DerivativeReplayEventV1` 只允许：

- `ContractTierEffectiveEventV1(ts, event_id, source_sequence, contract_snapshot_ref, tier_schedule_ref,
  execution_fee_schedule_ref)`；三个引用必须在同一 phase 一致生效，不能只切换其中一项；
- `IndexPriceEventV1(ts, event_id, source_sequence, price, source_ref)`；
- `MarkPriceEventV1(ts, event_id, source_sequence, price, source_ref)`；
- `FundingSettlementEventV1(ts, event_id, source_sequence, rate, schedule_id, observed_at_ts, source_ref)`；
- `TradableEventV1(ts, event_id, source_sequence, reference_price, available_contracts, liquidity_role, source_ref)`；
- `BarCloseEventV1(ts, event_id, source_sequence, bar_start_ts, bar_end_ts, ohlcv, feature_ref)`。

公共 liquidation-order 市场流不是账户强平指令，v1 不接收该流。下文“liquidation”是内核在每个
accounting-sensitive timestamp 执行的自有 isolated-position 强平检查。`FundingSettlementEventV1` 必须是
真实 settlement event；bar 上的 as-of/aligned funding feature 只能供策略观察，永远不能产生资金现金流。

每个 source stream 必须按 `(ts, source_sequence)` 严格递增；event ID 全局唯一。内核不得悄悄排序、去重、
修正时间或覆盖冲突记录。序列化时间统一为带 `Z` 的 UTC RFC3339 微秒格式；内部使用 aware `datetime`。

### 5.3 `CompletedDerivativesRunV1`

正式 child result 只允许状态 `succeeded|liquidated`，至少包含 semantic request/event-set/contract 指纹、
稳定 completion reason、最终账户状态、处理游标、ledger 计数、可由 ledger 重算的经济摘要、metrics v2 与固定
`capital_promotion_eligible=false`。它不得引用自身 byte hash、artifact-set fingerprint、manifest fingerprint
或 semantic run fingerprint，避免身份自引用。`liquidated` 是完整、可审计的研究结果，但永远不可资本晋级。

### 5.4 `BlockedDerivativesDiagnosticV1`

任何 preflight、资源、freshness、continuity、scope 或恢复失败均生成独立 draft 类型，不得伪装成 completed
result。orchestrator 先关闭并隔离/销毁正式 child staging，再把最小 diagnostic 发布到
`artifacts/research/derivatives_backtest_diagnostics/<run_id>/<attempt_id>`；该目录使用独立
`derivatives-backtest-diagnostic/v1` manifest-last 合同，只允许 request/event-set 指纹、稳定 reason/subreason、
失败 phase、最后可信 cursor 和非经济资源计数。它不得包含 promotion metrics、收益指标或可被 qualification
consumer 接受的 child schema。

diagnostic 目录同样不可覆盖：相同 `run_id + attempt_id` 仅允许 exact byte retry，任何漂移 conflict；修复输入后
必须使用新 attempt，改变业务 request/event-set 时必须使用新 run ID。qualification reader 固定拒绝 diagnostics
根和 diagnostic schema，不得通过 artifact index 或目录扫描混入正式 child run。

## 6. Decimal、canonical identity 与时间窗口

- 算术使用独立 `decimal.Context(prec=50, rounding=ROUND_HALF_EVEN)`；对 invalid、division-by-zero、overflow、
  underflow 开 trap，不依赖调用线程 ambient context。
- 所有经济字段进入 JSON 前先转成 `canonical_decimal_identity()` 字符串：零统一为 `"0"`，去除无意义尾零，
  以确定性 coefficient/exponent 表示；hash 身份不得使用 display 格式或 binary float。
- 数量和价格的可交易离散化只通过 `InstrumentContract` 的 lot/tick 规则；不得先 Decimal rounding 再猜测合法性。
- 合约张数先对绝对值按 lot 向下取整再恢复符号，即始终朝零离散化；不得通过四舍五入扩大风险敞口。
  对限价/强平边界采用偏保守 tick：买入或回补价格向上，卖出或做空价格向下。账本始终保留精确 Decimal；
  展示层量化不得回写经济状态、身份字段或哈希输入。
- request 的评价窗口严格为 `[start_ts, end_ts)`。只有 `event.ts >= start_ts and event.ts < end_ts` 的事件进入
  评价 ledger；warmup 事件必须单独标识，不能计入 metrics 或成交。
- `BarCloseEvent.ts == bar_end_ts`；只有 close timestamp 位于半开窗口内的 bar 才产生 decision fact。
  `event.ts == end_ts` 永远属于下一窗口。
- start carry-in 是强制边界：v1 只接受 `q=0`、`A=null`、无 queued order，但必须携带 start 前最后一条
  source-sealed mark/index cursor、当时有效 contract/tier/fee schedule 与动态 funding schedule cursor；carry-in
  只建立 freshness/continuity 上下文，不计入事件数量、PnL 或策略 feature。
- end valuation 使用 `end_ts` 前最后一条、age `<=60s` 的 source-sealed mark，并同时要求有效 index；该
  `end_mark_ts < end_ts`，只形成最终盯市记录，不触发成交、funding 或 bar-close decision。缺 end mark/index
  时 run blocked，禁止用最后成交价或 bar close 补值。

## 7. 事件顺序与因果成交

全局事件键固定为 `(ts, phase_priority, source_sequence, event_id)`；同一 timestamp 的 phase 顺序不可配置：

1. `05` — contract/tier/fee schedule 生效切换并验证 tier-1；
2. `10` — index 更新；
3. `20` — mark 更新；
4. `30` — funding settlement，严格使用该 timestamp 的 `q(t-)`（同 timestamp fill 之前的仓位）；
5. `40` — funding 后、fill 前的第一次自有账户 liquidation check/forced close；
6. `50` — 既有 queued order 在 tradable event 成交，依次记 fill、fee、PnL、tier 与 margin；
7. `55` — post-fill 第二次 liquidation check，阻止费用/滑点或新仓使账户越过强平线；
8. `60` — 15m bar-close strategy decision，并把新订单加入队列。

若第一次强平触发，则立即取消全部 queued order，不再执行 phase 50/55/60；若第二次强平触发，则完成 forced
close 后终止该 run。funding 的 `q(t-)` 必须在 funding ledger 显式保存，不能用同 timestamp fill 后仓位回算。

同一 phase 内 `source_sequence` 相同或倒退即失败。一个 timestamp 的 bar-close decision 创建的订单不能回看
该 timestamp 已处理的 tradable event。`next-tradable-event/v1` 定义为：订单提交后，满足 scope、价格 freshness、
数量及执行约束且 `tradable.ts > decision.ts` 的第一条事件。不存在合格事件时记录
`expired_no_next_tradable_event`；禁止 fallback 到 bar close、下一 bar close 或窗口外事件。

queued order 在 single-position 模式下按 FIFO 处理；v1 每次 bar close 最多产生一个 live order。反向成交必须
确定性拆为“先平旧仓、再开新仓”两个 accounting legs，共享一个 exchange fill identity，费用按两个 leg 的绝对
contract 数量比例分摊，分摊余数归 closing leg。任何 self-cross、并存 long/short 或多订单竞态均失败关闭。

## 8. 数据完整性与 freshness 门禁

v1 不允许 freshness tolerance 随 run 任意调大。政策固定为：

- mark/index 必须来自各自 source-sealed 流；在 funding、liquidation、fill 和 bar-close 阶段使用的最新事件不得
  晚于当前事件，且 age 不得超过 60 秒；任一缺失或 age `> 60s` 分别返回
  `mark_price_missing|stale`、`index_price_missing|stale`。
- mark/index 价格必须正、有限、符合各自 schema；basis 可记录但不得用一个流合成另一个流。
- funding schedule 必须来自有效 instrument/source snapshot，并允许 cadence 随 effective segment 动态变化；
  禁止硬编码“每 8 小时”。窗口内每个动态 schedule timestamp 必须有且仅有一条
  `FundingSettlementEventV1`；缺失、重复、窗口错位均 blocked。`observed_at_ts <= ts`，且
  `ts - observed_at_ts` 不得超过该 timestamp 所属 schedule segment 的 cadence；否则
  `funding_event_stale`。schedule 切换点与 settlement 同时发生时，先应用 phase 05 的新 schedule，再验证 event。
- Gold 中“最近 funding rate 对齐到每根 bar”的行不能转换成 settlement event，除非原始、已封存 funding
  记录能够证明 exact effective timestamp、rate、schedule 和内容 digest。
- `derivatives-event-set/v1` 必须逐流绑定 schema、canonical event count/digest、raw partition hashes、dataset
  version、coverage `[start,end)`、transform policy/version、gap/duplicate 结果和 instrument/tier/fee source
  reference。只绑定一个聚合 row count、调用方声明的 `source_ref` 或普通路径不合格。
- orchestrator 在任何经济状态变化前完成第一遍 preflight：用稳定、有界 reader 验证 event-set manifest 与所有
  stream 的 byte hash/size、strict schema、count、canonical event digest、coverage 和父 seal；通过后从同一 immutable
  snapshot 重新打开第二遍执行。执行期间逐流重算 count/digest 和 total event-set fingerprint，结束时与 preflight
  typed-exact 比较。调用者不能注入任意 event object；两遍之间或恢复期间的 path/content identity 漂移立即转为
  `event_set_identity_changed` diagnostic，绝不发布正式 child run。

任何门禁失败都在第一次经济状态变更前阻断；运行中发现后续 gap 时销毁未发布成功结果并发布独立 failure
diagnostic（不可带 promotion metrics）。不得 forward-fill、插值、使用 close proxy 或把 UNKNOWN 变成零值。

## 9. 资金、费用、PnL、保证金与简化强平

令：

- `q`：signed contract position；long 为正、short 为负；
- `f = contract_value * contract_multiplier`：每张合约对应的 BTC 数量；
- `Q = q * f`：signed BTC exposure；
- `A`：当前平均入场价；`M`：当前 mark；`P`：fill price；
- `F`：isolated 之外的 free cash；`B`：isolated balance，包含转入资金及已实现 PnL，扣除费用和 funding；
- `T0`：转入前总 USDT；`I0`：start 时从 `F` 转入 `B` 的金额；初始化为 `F=T0-I0`、`B=I0`；
- `L`：leverage；`imr=1/L`；`mmr`、maintenance deduction `d` 与 liquidation fee rate `r_l`
  均来自当时有效的 immutable tier-1 schedule；v1 只接受 `d=0`，非零立即阻断；
- `r_t`：signed fee rate（正为成本，负为 rebate）；`r_f`：signed funding rate；
- `r_force=max(taker_fee_rate,0)+r_l`；`rho=mmr+r_force`。

冻结公式如下，所有量均为 Decimal/USDT（除明确标注者）：

1. `notional(q, p) = abs(q) * f * p`。
2. 成交费用 `fee_amount = notional(delta_q, P) * r_t`；正值为 debit、负值为 rebate，
   `B_after = B_before - fee_amount`。linear v1 fee asset 固定 USDT。
3. 未实现 PnL：`U(M) = q * f * (M - A)`。
4. 减仓 `c` 张（`0 < c <= abs(q_before)`）的已实现 PnL：
   `R_delta = c * f * sign(q_before) * (P - A)`；随后 `B += R_delta - fee_amount`。
5. 加仓沿用按 BTC exposure 加权的平均价；平到零后 `A=null`；反转先按第 7 节拆 leg。
6. funding：以 settlement phase 的 `q(t-)` 计算
   `funding_payment = q(t-) * f * M * r_f`；正 funding 下 long 支付、short 收取；
   `B_after = B_before - funding_payment`。零仓仍记录 event，但 payment 为零；as-of funding feature 不入账。
7. `isolated_equity = B + U(M)`；`account_equity = F + isolated_equity`；
   `net_pnl = account_equity - T0`。free cash `F` 不参与 isolated 强平，也不得自动补充 `B`。
8. `initial_margin_required = notional(q,M) * imr`；
   `maintenance_margin = max(0, notional(q,M) * mmr - d)`；
   `isolated_free_collateral = isolated_equity - initial_margin_required`。
9. 新增风险的 fill 先构造 post-fill state，再要求 `isolated_equity_post >= initial_margin_required_post`；
   同时要求 `L <= tier.max_leverage` 且 marked/projected notional 留在 tier-1；不满足 margin 时记录拒绝，
   越 tier 则整个 run blocked。reduce-only close 不因 IM 拒绝。
10. `liquidation_fee = notional(q,M) * r_l`；强平预计总成本为
    `forced_close_cost = notional(q,M) * r_force`。简化触发为
    `isolated_equity <= maintenance_margin + forced_close_cost`。触发后在当前 mark 全量 forced close，分别记录
    taker fee 与 liquidation fee，状态终止为 `liquidated`；不再产生策略决策。

在 `q`、`A`、`B` 不变的两事件之间，简化 liquidation price 为：

- long (`q>0`)：`P_liq = (A - (B+d)/(q*f)) / (1-rho)`；分子 `<=0` 时为 `null`；有效值向上取 tick；
- short (`q<0`)：`P_liq = (A + (B+d)/(abs(q)*f)) / (1+rho)`；有效值向下取 tick。

必须满足 `T0>=I0>0`、`L>=1`、`L<=tier.max_leverage`、`0<mmr<imr<=1`、`d=0`、`r_l>=0`、
`0<rho<1`，且整个 marked/projected notional 始终位于 immutable tier-1 有效范围。v1 只允许 start 时一次
`F -> B` 转移，且该 transfer 必须从 request 已绑定的 `free_cash_before_transfer_usdt` 精确扣除并写入首条
account ledger；之后禁止手工或自动 top-up、运行中转入/转出，free cash 不得用于避免强平。fee、funding 与
realized PnL 直接改变 `B`；平仓释放的 initial-margin capacity 只改变可用能力，不形成第二次现金转移。窗口结束
仍有仓位时，不做虚构平仓，只报告 `open_position_at_end=true`、最后 source-sealed mark 下的
`isolated_equity` 和 unrealized PnL。该公式是可重算、偏保守的 v1
研究模型，不等同于 OKX 分层 MMR、账户权益、强平费、风险准备金或 ADL 算法；这种模型风险正是
`capital_promotion_eligible=false` 的永久 v1 原因之一。

## 10. 子制品、manifest-last 与身份

成功 child run 的固定 artifact set：

1. `request.json`；
2. `source_snapshot.json`；
3. `event_ledger.jsonl`；
4. `decision_facts.jsonl`；
5. `orders.jsonl`；
6. `fills.jsonl`；
7. `funding_ledger.jsonl`；
8. `position_ledger.jsonl`；
9. `equity_curve.jsonl`；
10. `liquidation_ledger.jsonl`；
11. `phase2_promotion_metrics.json`；
12. `result.json`；
13. 最后写入的 `manifest.json`。

每个非 manifest 文件必须在 manifest 中有规范化相对路径、精确 byte size、lowercase SHA-256 与语义 schema。
禁止 symlink、路径逃逸、重复路径、额外未声明文件。身份必须形成以下单向、无环 DAG：

1. `semantic_request_fingerprint` 只对业务 scope、Step 2/3 candidate、event-set、contract/tier/fee snapshot、
   opening state 和 policy IDs 求 canonical typed SHA-256，明确排除 `run_id`、`request_created_at`、本地路径和
   publisher 时间；
2. request/source/各 ledger/metrics/result 依次写入并取得独立 byte hash；`result.json` 只引用已完成 subordinate
   ledger 的语义摘要，不引用自身、artifact set、semantic run 或 manifest；
3. `artifact_set_fingerprint` 最后对按路径排序的全部非 manifest
   `[{path,size,sha256,schema}]` canonical typed JSON 求 SHA-256；它是字节发布身份，因 `request.json` 包含
   `run_id/request_created_at`，不同运行实例可以不同；
4. `result_semantic_fingerprint` 对最终经济状态、ledger 计数与 canonical metrics 值求摘要；
   `semantic_run_fingerprint` 仅由 semantic request、event-set、engine/policy IDs 和 result semantic fingerprint
   组成，不包含 artifact-set 或任何生成时间；
5. 最后写的 manifest 单向引用上述 fingerprint 与非 manifest hash。manifest 自身 digest/size 仅由后续
   qualification round 计算并固化，不写回 child manifest。

任何实现若出现 A 的 hash 直接或间接依赖 A、result 依赖 manifest、semantic run 依赖 byte artifact set，均为
schema 错误而非可接受的序列化细节。

publisher 只写同父目录的私有 staging directory，逐文件 flush/fsync，完成 hash/size 复核后最后写 manifest、
fsync directory，再原子 rename 到从未存在的 final directory。consumer 只接受 final directory 中 exact schema、
`complete=true` 且全量复核通过的 manifest。任何原位覆盖、追加或“补 manifest”都视为冲突。

所有 JSON 使用 UTF-8、无 BOM、strict JSON、key 字典序与无多余空白；所有 JSONL 固定为每行一个 strict JSON
object、canonical Decimal string、LF 换行且包含最终换行。重复 key、JSON number 承载经济 Decimal、非有限值、
CRLF/平台本地换行或不完整末行均失败关闭。manifest 的 byte size 与 SHA-256 针对上述规范化后的原始字节，
consumer 不得解析后重新序列化来替代原始字节校验。

资源边界由不可被环境变量或 request 覆盖的 `derivatives-backtest-resource-policy/v1` 冻结：单条 JSONL
记录不超过 1 MiB，request/source/result/manifest 单文件不超过 4 MiB，checkpoint 不超过 8 MiB，单个 JSONL
不超过 512 MiB，完整 child artifact set 不超过 2 GiB；总输入事件不超过 10,000,000，任一 source stream
不超过 5,000,000，live queued order 上限为 1。实现阶段若验证后需要改变数值，必须发布新的 policy ID 并更新
golden tests，不得静默放宽。超过边界返回稳定 `resource_limit_exceeded` 子原因并保持未发布状态；禁止截断、
抽样后声称成功或依赖进程 OOM 作为门禁。

## 11. `phase2-promotion-metrics/v2` 可重算契约

metrics v2 不接受生产者自报。consumer 必须从 hash-bound `decision_facts`、fill/position/funding/equity ledgers
独立重算并逐字段 exact 比较。核心定义：

- `total_bars`：评价窗口内 bar-close fact 数；每个合格 bar 必须恰好一条 fact；
- `opening_count`：position ledger 中 `flat -> nonzero` 的 distinct decision ID 数；反转的 opening leg 计一次；
- 每条 decision fact 必须分别记录 gross expected edge、expected fee、slippage、funding 与
  `cost_adjusted_edge_bps`；后者必须由前四项按固定 `metrics_policy_version` 重算，不能由策略自报；
- `positive_edge_count`：`cost_adjusted_edge_bps > 0` 的 decision fact 数；
- `positive_edge_ratio = positive_edge_count / total_bars`；空窗口非法，不返回 0 冒充结果；
- `selectable_ratio = selectable_count / total_bars`；
- `execution_compatible_ratio = execution_compatible_count / total_bars`；
- `mean_expected_edge_bps = sum(cost_adjusted_edge_bps) / total_bars`；metrics 同时写
  `cost_adjusted_edge_mean` 且必须与该值 typed-exact 相等、有限且非空；字段名与现行 Phase 4 投影对齐，
  但 wire type **不与当前 v1 consumer 兼容**；
- gross/realized/unrealized PnL、fee、funding、final equity、max drawdown 均从 ledger 重放计算。

metrics 还必须绑定 symbol、contract/snapshot fingerprint、family/timeframe、dataset/source fingerprints、窗口、
parameter fingerprint、event/accounting/execution policy IDs、ledger digests及 reason codes。所有 ratio/mean 使用
canonical Decimal string，不写 JSON float。`capital_promotion_eligible=false` 和
`capital_promotion_reason="derivatives_backtest_v1_not_capital_qualified"` 为必填常量；hard gate 即使数值通过也不能
改为 true。

现行 `phase2-promotion-metrics/v1` validator 与 shared promotion policy 只接受 JSON `int|float`；不得把 v2
Decimal string 转成 binary float 后塞入旧 gate，也不得为了接入本 child 放宽 v1 schema。LF-B3 必须原子新增
v2 consumer、v2 shared policy 与 Decimal parser，并使 validator、candidate selector、readiness、Decision 及
锁内 apply qualification 使用同一政策实现；任何一个消费者仍走 v1 numeric gate 时，v2 child 保持
`unsupported_metrics_schema`/hold。该集成完成前，字段同名不代表可被现行 Phase 4 或 Phase 6 消费。

## 12. 崩溃、一致性与恢复

- 每个 run 使用 `run_id` 精确锁；并发 exact retry 只能有一个 writer。已存在 final directory 时，只有 request、
  source、policy 和 manifest fingerprint 全相同才返回 existing success；任一差异为 conflict。
- checkpoint 只能存在 staging directory，采用原子 replace，包含 exact semantic request/event-set preflight/
  instrument/tier/fee 指纹、各输入流 restart cursor、最后 global event key、账户状态、queued order、ledger byte
  size/rolling SHA-256、资源计数和 checkpoint fingerprint。cursor 必须由 formal event source 解释，普通 iterable
  偏移或“已处理 N 条”不能作为恢复身份。
- 恢复必须显式给出 run ID/staging path；禁止扫描 `latest`、artifact index 或相似目录。恢复前逐项复核 checkpoint
  与已写 ledger，并重新执行完整 event-set preflight；随后从每条流的 exact cursor 重放，重算前缀/后缀身份。
  不一致则隔离 staging 并从头用新 attempt 运行，不能跳过事件或接受调用方替换的 event iterable。
- crash before manifest：目录不可消费；crash after manifest/before rename：复核全部字节后仅允许完成原子发布；
  crash after rename：final immutable，exact retry 只读验证。
- 给定 byte-identical request（包括相同 run metadata）和同一 event-set，从零运行与任意合法 checkpoint 恢复必须
  得到逐字节相同的全部正式 child artifact。不同 `run_id/request_created_at` 可产生不同 byte artifact set，但相同
  业务输入与经济输出必须得到相同 semantic request/result/run fingerprint；publisher 时间不得写入任何正式 child
  payload。diagnostic attempt 身份与正式 child 身份完全分离。

## 13. 测试与 golden vectors

最低验收包括：

1. strict schema：extra/missing key、bool-as-number、float、NaN/Infinity、非 canonical decimal/time/hash 全部拒绝；
2. 时间：start flat carry-in、end mark、同 timestamp 八阶段顺序、funding 使用 `q(t-)`、same-timestamp fill
   禁止、无 next event 正确 expire；
3. 数据：mark/index 缺失或 age `>60s`、funding 缺/重/陈旧、gap/乱序、event-set 两遍间漂移、实际消费
   count/digest 与 manifest 不同、非 restartable source 全部 blocked；
4. 记账：long/short、加仓、部分平仓、全平、反转、maker rebate、正负 funding、1x leverage 边界、margin reject、强平；
5. 证据：每个 artifact 的 hash/size、路径逃逸、额外文件、manifest 非最后、修改任一字节、指纹自引用/DAG
   依赖倒置、blocked diagnostic 被 qualification 接受均失败；
6. determinism/recovery：不同 Decimal ambient context、不同输入 chunk、每个 phase 后 crash 的结果逐字节一致；
7. PostgreSQL integration：后续 qualification snapshot exact retry/conflict、v2 Decimal shared gate 与 v1 隔离；
   本 child 内核本身保持无 DB 副作用；
8. 独立复审：金融符号、时间因果、并发、资源上限、安全与文档无未关闭 P0/P1。

首个合成 golden vector 使用测试专用 contract/tier snapshot（不声称是当前 OKX schedule）：`f=0.01 BTC`、
`q=10`、`T0=I0=1000 USDT`、`F=0`、`A=50000`、`L=5`、`mmr=0.005`、`d=0`、
taker `=0.0005`、liquidation fee `=0.0025`。
开仓费为 `2.5 USDT`，开仓后 `B=997.5`；mark `51000` 时 `U=100`、equity `1097.5`、MM `25.5`。
正 funding `0.0001` 时 long 支付 `0.51`；以 `52000` 平仓实现 `200`、平仓费 `2.6`，最终 equity
`1194.39`。此时 `rho=0.008`，开仓后的 long 简化强平价为 `40347.8`（向上到 `0.1` tick）。
同一 timestamp 的 tradable event 不得成交刚产生的 decision；下一条严格更晚事件才可成交。

另冻结以下彼此独立的最小向量；测试期望值必须作为常量直接写入 fixture，不能调用生产 accounting 函数生成：

- 单位/PnL：`q=3`、`f=0.01 BTC`、`A=60000`、`M=61000`，则 `Q=0.03 BTC`、
  `notional(M)=1830 USDT`、long `U=30 USDT`；short 同条件 `U=-30 USDT`。
- fee/rebate：上述 3 张在 `P=60000` 的 notional 为 `1800`；taker `0.0005` 的 fee debit 为 `0.9`；
  maker `-0.0002` 的 `fee_amount=-0.36`，故 `B` 增加 `0.36`。负 fee 不得被 clamp 为零。
- 强平：`q=1`、`f=0.01`、`A=50000`、`B=50`、`d=0`、`L=10`、`mmr=0.005`、
  `r_force=0.0005`，故 `rho=0.0055`。long 未离散值为
  `45248.868778280542986425339366515837104072398190045`，`0.1` tick 向上得到 `45248.9`；short 未离散值为
  `54699.154649428145201392342118349080059671805072103`，`0.1` tick 向下得到 `54699.1`。
- funding：`abs(q)=1`、`f=0.01`、`M=50000`、`r_f=0.0001` 时绝对 payment 为 `0.05`；long 的
  `funding_payment=0.05`、账户 cashflow `-0.05`，short 的 `funding_payment=-0.05`、账户 cashflow `+0.05`。

每个向量都必须同时断言单位、符号、完整精度和保守 tick 方向；不得只比较经过 display rounding 的数值。

## 14. 兼容、迁移与回滚

- `backtest-run/v2`、其 CLI、schema 和 consumer 保持字节/行为不变；新 v1 使用不同 package、schema、目录与
  validator，不提供旧 artifact 原位升级。
- Phase 2 现有 consumer 在 qualification integration 前必须继续拒绝 `derivatives-backtest-run/v1`，而不是
  看到新文件即视为 eligible。
- rollout 顺序：纯 contracts/accounting → golden vectors → event engine → publisher/recovery → child validator →
  qualification round。每一阶段均默认无 workflow 调用。
- 回滚只需移除新内部调用并保留已生成 artifact 作为历史审计；不得删除/改写 immutable child directory。
  schema 被否决时发布新的 `/v2`，不得改变 `/v1` 语义。

## 15. 后续 qualification round 接口

LF-B3 才能建立 `phase2_qualification_rounds/<round_id>`。其父 manifest 必须显式锚定 exact Step 2 ID、exact
Step 3 ID、candidate digest/size/parameter fingerprint、本 ADR child manifest path/digest/size/run fingerprint、
instrument snapshot 与 position-tier/MMR/liquidation-fee/fee schedule snapshot 的 ID/digest/effective window、
source seal 和期望 combo 拓扑。managed DB snapshot 以同 round ID
exact retry；任何漂移 conflict。Phase 6 只能显式接收 exact qualification round ID，禁止 latest/index fallback。

由于本 ADR v1 固定 `capital_promotion_eligible=false`，首个 qualification round 也只能验证链路、重算 metrics
并给出 `blocked/hold`。要晋级，必须另立 ADR，至少关闭真实 source seal、instrument/risk-schedule temporal
lineage、mark/index/funding continuity、交易所级 margin/liquidation 校准及 L2/paper calibration，并经真人
Risk/Data reviewer 签字。

## 16. 方案比较与拒绝项

| 方案 | 结论 | 原因 |
| --- | --- | --- |
| 在 SPOT harness 上增加 `if contract_type == linear` | 拒绝 | 错误复用 SPOT 状态机，极易漏 funding/margin/liquidation 与证据边界 |
| 把 `BTC-USDT` SPOT 结果按杠杆换算成 SWAP | 拒绝 | instrument、资金、费用、强平和时间语义均不等价 |
| 用 Gold close 代替 mark/index、用 aligned rate 代替 funding event | 拒绝 | 制造不存在的来源真实性，无法重算精确 settlement |
| 直接实现完整多品种交易所仿真 | 暂缓 | 范围过大，无法先验证最小正确纵向切片 |
| 独立 BTC linear isolated v1，永久不可晋级 | **采纳** | 可隔离风险、冻结单位与因果语义，并为后续资格轮提供可信 child contract |

同时明确拒绝：mutable output、全局 latest、隐式 defaults、float 资本记账、随机 fill、same-bar fill、跨窗口成交、
缺流时降级、inverse/cross/hedge、多 symbol、将市场 liquidation-order 当账户强平、自动 recommendation/apply、
live profile 或真实资金验证。

## 17. 实施门与验收决定

本 ADR 进入 Accepted 前必须由实名 RDP Owner、Independent Risk Reviewer、Data Lineage Reviewer 对以下内容签字：
金融公式/符号、freshness 数值、source seal、instrument temporal lineage、强平模型限制、artifact schema 和
`capital_promotion_eligible=false` 不变量。AI 生成、单元测试或模板填写不能替代该签字。

本 ADR 当前结论仅为“可以据此开始实现合成、失败关闭的内部纵向切片”。当前 Gold、mark/index、instrument
snapshot、position-tier/MMR/liquidation-fee schedule snapshot 与 source seal 阻断仍开放；没有运行证据，没有 qualification round，没有部署授权，也没有任何真实资金
副作用。故截至 2026-08-28：**LF-B 未完成，RDP 不可据此晋级或发布到实盘。**
