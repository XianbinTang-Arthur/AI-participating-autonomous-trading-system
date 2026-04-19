# Task72-B 合约优先版第二批具体开发任务

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../../docs/project_positioning.md)。


## 1. 文档定位

这份文档承接 `Task72-A` 已完成的合约底座、账户语义、pre-trade 风控、执行状态机与恢复阻断工作。  
当前目标进入阶段 4，先补“资金费账务闭环 v1”“合约持仓按仓位侧隔离 v1”“统一 realized PnL / 手续费 / funding fee 收益视图 v1”和“cross / isolated 保证金占用、释放与对账规则 v1”，把交易所账单、合约双向持仓、收益归因和保证金状态都提升为本地可审计真相。

## 2. 当前批次范围

本批次包含四个高优先级任务：

1. `Task72-B1` 合约资金费账务闭环 v1
2. `Task72-B2` 合约持仓按仓位侧隔离 v1
3. `Task72-B3` 统一 realized PnL / 手续费 / funding fee 收益视图 v1
4. `Task72-B4` cross / isolated 保证金占用、释放与对账规则 v1

这不是阶段 4 的全部内容。  
它优先解决三类高风险缺口：

1. 资金费只存在于交易所页面或临时缓存里，无法进入本地 ledger / operator / 回放链路
2. `long_short_mode` 下本地持仓与 lot 只按 `symbol` 聚合，可能把多空双腿混成一条净仓
3. 交易 realized PnL、手续费和 funding fee 分散在多个接口里，operator 无法在一张收益视图里看完整结果
4. 仓位级全仓 / 逐仓模式、保证金占用、维持保证金和强平价虽然部分存在于交易所原始字段里，但没有进入本地快照、对账和控制面摘要

## 3. Task72-B1 合约资金费账务闭环 v1

### 3.1 目标

- 把 OKX recent bills 中的 funding fee 结构化成持久化记录
- 把 funding fee 变化幂等写入 ledger journal / entry
- 让资金费影响本地可用余额与后续快照重建
- 让 operator 可以直接读取已持久化的 funding fee 列表与摘要
- 为后续持仓、PnL、cross / isolated 对账继续扩展留出稳定落点

### 3.2 代码级开发子项

#### B1.1 资金费结构化 schema 与持久化模型

主要改动：

- 增加 `FundingFeeRecord`
- 增加 `funding_fee_records` 表
- 增加内存版和 Postgres 版 repo
- 增加 runtime scope 过滤能力

当前落点：

- `aats/schemas/portfolio.py`
- `aats/storage/sqlalchemy_models.py`
- `aats/storage/base.py`
- `aats/storage/funding_fee_repo.py`
- `aats/storage/funding_fee_repo_postgres.py`
- `aats/services/runtime_scope.py`

验收标准：

- 同一 `bill_id` 可以幂等 upsert
- scope 查询能按 `product_type`、`margin_mode`、`allowed_symbols` 过滤
- 记录中必须保留 `amount`、`currency`、`symbol`、`bill_type`、`sub_type`、`funding_direction`、`ledger_posting_state`

#### B1.2 资金费 ledger 入账服务

主要改动：

- 增加 `LedgerFundingFeeSyncService`
- 过滤 OKX recent bills 中的 funding fee
- 将 funding fee record 与 ledger journal / entry 放进同一事务提交
- 对已入账 `bill_id` 做幂等保护
- 对已入账后字段漂移的账单做冲突阻断

当前落点：

- `aats/services/ledger/funding_fee_sync.py`

验收标准：

- funding fee expense 会减少 `cash_available`
- funding fee income 会增加 `cash_available`
- journal source 需稳定映射到 `bill_id`
- 重复同步同一批账单不会重复记账

#### B1.3 runtime 启动与后台刷新接入

主要改动：

- runtime 启动时在账户 refresh 后同步 funding fee
- account refresh loop 成功后继续同步 funding fee
- 有新 funding fee 入账时触发一次本地快照重建，避免 ledger 与快照长期漂移

当前落点：

- `aats/bootstrap/config.py`

验收标准：

- Postgres runtime 启动后能够自动拉起 funding fee 同步
- 后台 refresh 不会因为重复账单产生重复 journal
- 新资金费到账后，后续 operator 读取到的本地余额与快照会收敛

#### B1.4 operator 查询接口

主要改动：

- 新增 `/account/recent-funding-fees`
- `account/state` 增加已持久化 funding fee 摘要
- 保留交易所 recent bills 摘要，同时补充本地 persisted 摘要

当前落点：

- `aats/services/operator/query_service.py`
- `aats/api/routes.py`

验收标准：

- operator 能看到已持久化 funding fee 列表
- operator 能看到按币种聚合后的净额与绝对额
- operator 能区分交易所 recent summary 与本地 persisted summary

#### B1.5 回归测试

主要改动：

- PostgreSQL 账务回归：入账、幂等、冲突阻断
- operator API 回归：recent-funding-fees 与 account/state 摘要
- runtime phase5 回归：保证控制面未被新增语义破坏

当前落点：

- `tests/unit/test_task72_funding_fee_sync.py`
- `tests/integration/test_operator_api.py`
- 复用 `tests/integration/test_phase5_control_plane_runtime.py`

验收标准：

- 账务回归必须真实命中 PostgreSQL
- operator API 必须能读出新 funding fee 数据
- 现有 phase5 控制面套件不得回归失败

## 4. 当前完成状态

`Task72-B1` 已实现第一轮版本，当前能力包括：

- funding fee 结构化持久化
- funding fee 幂等 ledger 入账
- runtime 启动与 refresh 同步接入
- operator recent-funding-fees 查询
- account/state 中的 persisted funding fee 摘要

`Task72-B2` 已实现第一轮版本，当前能力包括：

- 本地 `PortfolioState`、lot projection、portfolio snapshot 以 `position_key` 区分 `symbol:long` / `symbol:short`
- `position_mode`、`pos_side`、`instrument_family`、`settle_currency` 已贯通到持仓快照与 lot 元数据
- replay、reconciliation、recovery 已按 `position_key` 对比本地持仓
- operator `/positions` 已同时提供逐腿持仓视图和按 `symbol` 聚合后的净仓视图

`Task72-B3` 已实现第一轮版本，当前能力包括：

- `profitability_overview` 已同时返回交易 realized 收益、funding fee 汇总和合并后的 realized 净额
- operator 报表已新增 `recent_realized_events`，能按统一时间轴查看成交和资金费事件
- 旧的 `recent_closed_fills` 与试盘守护依赖字段保持兼容，未直接改变风控语义

`Task72-B4` 已实现第一轮版本，当前能力包括：

- OKX 合约仓位中的 `mgnMode`、保证金占用、维持保证金、保证金率和强平价已结构化进入 `ExchangePosition`
- exchange baseline / rebaseline 导入后，本地 `PortfolioSnapshot` 会保留交易所来源的保证金字段，并显式标记 `margin_source`
- reconciliation 已新增仓位级保证金金额偏差和 `cross / isolated` 模式偏差识别，不再只比较数量
- operator `/positions` 与 `/account/state` 已新增本地 / 交易所保证金摘要和最新保证金对账摘要

## 4.1 Task72-B2 合约持仓按仓位侧隔离 v1

### 4.1.1 目标

- 在 `long_short_mode` 下把本地持仓从“按 `symbol` 一条”升级成“按仓位侧多条”
- 防止 long / short 双腿在 fill 重放、lot 重建、快照持久化和恢复比较时被错误净额合并
- 让 operator 可以同时看到逐腿持仓和按 `symbol` 聚合后的净仓视图

### 4.1.2 代码级开发子项

#### B2.1 持仓主键与结构化 schema

主要改动：

- 增加 `position_key`
- 在 `Position` / `PositionRecord` 中显式保留 `position_mode`、`pos_side`、`instrument_family`、`settle_currency`
- 增加持仓侧别工具函数，统一 `symbol`、`position_mode`、`pos_side` 到稳定 key

当前落点：

- `aats/schemas/portfolio.py`
- `aats/services/portfolio_service/positions.py`
- `aats/services/portfolio_service/position_keys.py`

验收标准：

- `spot` 和 `net_mode` 继续使用 `symbol` 作为 position key
- `long_short_mode` 下必须稳定生成 `symbol:long` / `symbol:short`
- 从 exchange snapshot 和 local snapshot 恢复状态时不会丢失 `pos_side`

#### B2.2 FIFO lot 与 portfolio snapshot 升级

主要改动：

- lot builder 改成按 `position_key` 重建
- open / close lot event payload 保留仓位侧语义
- portfolio snapshot 中的 position、cost basis、leverage profile 兼容 side-scoped key

当前落点：

- `aats/services/ledger/lot_projection.py`
- `aats/services/portfolio_service/snapshots.py`
- `aats/storage/portfolio_repo_postgres.py`

验收标准：

- long / short fill 不会在 lot rebuild 时互相抵消
- 同一 `symbol` 的双腿仓位可以同时出现在本地快照里
- 快照持久化和读取后 `position_key` 不丢失

#### B2.3 回放、恢复与 operator 读取升级

主要改动：

- replay / reconciliation / recovery 按 `position_key` 比较本地持仓
- 决策上下文继续按 `symbol` 聚合净仓，避免策略层被双腿视图污染
- operator `/positions` 同时提供逐腿持仓和净仓聚合

当前落点：

- `aats/services/reconciliation_service/replay.py`
- `aats/services/reconciliation_service/comparator.py`
- `aats/services/execution_engine/recovery.py`
- `aats/services/decision_engine/context_builder.py`
- `aats/services/operator/query_service.py`

验收标准：

- `long_short_mode` 下 replay 不会把双腿快照误判成持仓漂移
- 恢复比较不会因为 `symbol:long` / `symbol:short` 被当成陌生持仓而误报
- operator 能同时看到 `local_positions` 和 `local_net_positions`

#### B2.4 回归测试

主要改动：

- `PortfolioState` 双腿持仓测试
- FIFO lot 双腿隔离测试
- 决策上下文按 `symbol` 聚合测试
- reconciliation 按 `position_key` 的 long-short 比较测试
- operator API 兼容性回归

当前落点：

- `tests/unit/test_portfolio_state.py`
- `tests/unit/test_task57_lot_projection_and_convergence.py`
- `tests/unit/test_decision_context_builder.py`
- `tests/unit/test_reconciliation.py`
- `tests/integration/test_operator_api.py`

验收标准：

- 单测要覆盖 long / short 双腿同时存在的场景
- integration 回归要确认控制面和持久化链路未被破坏

## 4.2 Task72-B3 统一 realized PnL / 手续费 / funding fee 收益视图 v1

### 4.2.1 目标

- 在 operator 侧把交易 realized PnL、手续费和 funding fee 放进同一张收益视图
- 保持现有 `profitability_overview` 兼容，避免破坏试盘守护和 trial review 现有依赖

## 4.3 Task72-B4 cross / isolated 保证金占用、释放与对账规则 v1

### 4.3.1 目标

- 把 OKX 合约仓位中的 `cross / isolated` 模式、仓位保证金占用、维持保证金、保证金率和强平价结构化成一等字段
- 让 exchange baseline / operator rebaseline 导入后的本地快照保留真实交易所保证金语义，而不是只剩数量
- 让 reconciliation 在仓位数量之外，继续比较仓位级保证金金额和 `cross / isolated` 模式
- 让 operator 直接看到本地 / 交易所保证金摘要，以及最新保证金对账结果

### 4.3.2 代码级开发子项

#### B4.1 交易所仓位保证金字段结构化

主要改动：

- 扩展 `ExchangePosition`
- 解析 OKX `positions` 中的 `mgnMode`、`margin / imr`、`mmr`、`mgnRatio`、`liqPx`、`lever`
- 如果交易所已有仓位模式与当前 runtime `margin_mode` 冲突，则在账户状态中直接暴露 blocker

当前落点：

- `aats/schemas/exchange.py`
- `aats/services/execution_engine/okx_account.py`

验收标准：

- `ExchangePosition` 中必须保留 `margin_mode`、`margin_allocated`、`maintenance_margin`、`margin_ratio`、`liquidation_price`
- derivatives 账户 refresh 后，这些字段不能只留在 `raw`
- runtime `margin_mode` 与交易所现有仓位的 `mgnMode` 冲突时，账户状态必须显式变为 not ready

#### B4.2 exchange baseline 到本地快照的保证金保真

主要改动：

- `PortfolioState.load_exchange_snapshot` 保留交易所仓位的保证金字段
- `PortfolioSnapshotBuilder` 对 exchange 导入的仓位标记 `margin_source=exchange`
- 本地快照级 `margin_usage` 优先汇总真实交易所占用，只有本地 fill 推导仓位才退回估算值

当前落点：

- `aats/schemas/portfolio.py`
- `aats/services/portfolio_service/positions.py`
- `aats/services/portfolio_service/snapshots.py`
- `aats/storage/portfolio_repo_postgres.py`

验收标准：

- rebaseline 后生成的本地快照必须保留交易所仓位的真实保证金字段
- `PortfolioSnapshot.positions[*].margin_source` 必须能区分 `exchange` 与 `estimated`
- 不允许因为单条仓位的 `margin_mode` 改变整个快照 scope

#### B4.3 仓位级保证金对账规则

主要改动：

- reconciliation 新增仓位级保证金金额偏差比较
- reconciliation 新增仓位级 `cross / isolated` 模式偏差比较
- 仅当本地仓位保证金字段来源于 `exchange` 时，才把金额偏差升级成正式 mismatch，避免把估算值当真相

当前落点：

- `aats/services/reconciliation_service/comparator.py`

验收标准：

- 仓位数量一致但保证金金额不一致时，report 必须能显式给出 `exchange_margin_mismatches`
- 本地仓位模式与交易所真实仓位模式冲突时，report 必须升级成硬阻断
- fill 推导出来的估算保证金不能持续制造假阳性 mismatch

#### B4.4 operator 保证金摘要视图

主要改动：

- `/positions` 新增本地 / 交易所保证金摘要和最新保证金对账摘要
- `/account/state` 新增本地 / 交易所仓位保证金摘要
- 前端术语表增加新增保证金偏差和 blocker 的 UTF-8 中文映射

当前落点：

- `aats/services/operator/query_service.py`
- `aats/api/static/modules/terms.js`

验收标准：

- operator 能直接看到本地 / 交易所仓位保证金总额、维持保证金总额和仓位模式分布
- operator 能直接看到最近一次保证金对账是否存在模式偏差或金额偏差
- 新增错误码和 blocker 在前端必须显示为干净中文

#### B4.5 回归测试

主要改动：

- OKX 账户解析回归
- exchange baseline 保证金字段保真回归
- reconciliation 保证金偏差 / 模式冲突回归
- operator API 保证金摘要回归

当前落点：

- `tests/unit/test_okx_account.py`
- `tests/unit/test_portfolio_precision.py`
- `tests/unit/test_reconciliation.py`
- `tests/integration/test_operator_api.py`

验收标准：

- OKX 账户单测必须断言仓位保证金字段解析成功
- reconciliation 单测必须同时覆盖金额偏差和模式冲突
- operator API 必须直接暴露新增摘要字段
- 让控制面能够同时看到交易收益、资金费拖累和合并后的 realized 结果

### 4.2.2 代码级开发子项

#### B3.1 收益事件统一建模

主要改动：

- 把 fill outcome 扩成交易 realized event row
- 把 funding fee record 映射成 funding event row
- 按统一时间轴合并最近 realized 事件

当前落点：

- `aats/services/operator/query_service.py`

验收标准：

- `recent_realized_events` 必须能同时包含成交收益事件和 funding fee 事件
- 事件必须保留 `event_kind`、时间戳、symbol、净收益变化和来源标识

#### B3.2 profitability overview 兼容扩展

主要改动：

- 保留 `gross_realized_pnl`、`net_realized_pnl`、`recent_closed_fills`
- 新增 `funding_fee_summary`
- 新增 `combined_net_realized_pnl`
- 新增 `funding_fee_count`、`funding_fee_income_count`、`funding_fee_expense_count`

当前落点：

- `aats/services/operator/query_service.py`

验收标准：

- 老的试盘守护和 trial review 依赖字段继续可用
- 新 summary 能回答交易净收益、资金费净额和合并后的 realized 净额分别是多少

#### B3.3 operator API 回归

主要改动：

- 增加 operator API 对 unified profitability 的断言
- 验证 combined net = trading net + funding net
- 验证 funding fee 事件能进入 realized timeline

当前落点：

- `tests/integration/test_operator_api.py`

验收标准：

- operator API 回归必须覆盖 funding fee 和交易收益同时存在的场景
- full visibility 回归不得因为新增字段而退化

## 5. 下一步建议

完成 `Task72-B1`、`Task72-B2` 和 `Task72-B3` 后，建议继续按以下顺序推进阶段 4：

1. `cross / isolated` 保证金占用、释放与对账规则
2. 按持仓周期与结算周期追踪 PnL 的 operator 视图
3. 按逐腿持仓和净仓同时展示收益、保证金与风险缓冲的控制面视图
4. 再决定 trial guard 是否需要从“交易净收益”升级成“交易净收益 + funding fee”的守护口径
