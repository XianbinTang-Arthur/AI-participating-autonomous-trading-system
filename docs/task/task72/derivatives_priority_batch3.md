# Task72-C 合约优先版第三批具体开发任务

## 1. 文档定位

这份文档承接 `Task72-B` 已完成的资金费账务闭环、双向持仓隔离、统一收益视图和 `cross / isolated` 保证金对账工作。  
当前目标继续收敛阶段 4 到阶段 6 之间最影响真钱试运行判断的缺口：收益归因必须能落到持仓生命周期，清算风险必须能在控制面一眼看懂，`trial_guard` 必须纳入资金费拖累而不是只看成交净收益。

## 2. 当前批次范围

本批次包含三个高优先级任务：

1. `Task72-B5` 合约持仓生命周期收益归因 v1
2. `Task72-B6` 清算距离与保证金缓冲控制面 v1
3. `Task72-B7` 资金费纳入 trial guard / forward validation / trial review v1

这三个任务的共同目标是把“收益是否可信、风险是否逼近强平、试盘是否还能继续”从分散字段升级成 operator 可直接使用的结论链。

## 3. Task72-B5 合约持仓生命周期收益归因 v1

### 3.1 目标

- 把成交收益从“逐笔 fill 列表”升级成“按持仓生命周期可复盘”的收益链
- 在 `long_short_mode` 下继续保留 `position_key` 语义，避免把双腿收益混成一条净额
- 明确资金费的归因范围：能唯一匹配持仓窗口时归入生命周期，不能唯一归因时单独标记，不做伪精确分摊

### 3.2 代码级开发子项

#### B5.1 Fill outcome 语义补齐

主要改动：

- `FillOutcomeRecord` 显式增加 `position_key`、`position_mode`、`pos_side`、`instrument_family`、`settle_currency`
- fill outcome 持久化时把合约仓位侧语义一并写入 payload

当前落点：

- `aats/schemas/portfolio.py`
- `aats/services/portfolio_service/positions.py`
- `aats/services/projections/ledger_portfolio.py`

验收标准：

- 新写入的 fill outcome 能直接回答“它属于哪条仓位腿”
- 老数据缺少这些字段时，读取路径仍然兼容

#### B5.2 持仓生命周期汇总器

主要改动：

- 按 `position_key` 重建生命周期窗口
- 识别 open / add / reduce / close / reverse 边界
- 汇总每条生命周期的 `gross_realized_pnl`、`net_realized_pnl`、手续费、峰值仓位、开平时间

当前落点：

- `aats/services/operator/query_service.py`

验收标准：

- 报表能列出 closed / open lifecycle
- `long_short_mode` 下多空双腿不会互相吞并
- reversal 场景必须显式标记为过渡边界，不允许静默混算

#### B5.3 资金费归因窗口

主要改动：

- 按 `symbol + 时间窗口` 尝试把 funding fee 归到唯一生命周期
- 对无法唯一归因的 funding fee 输出 `unassigned` 列表和原因
- 报表中显式给出 `funding_fee_attribution_scope`

当前落点：

- `aats/services/operator/query_service.py`

验收标准：

- 资金费只有在“同一时刻只有一条可解释生命周期”时才允许归因
- 双腿同时持仓或没有匹配生命周期时，必须保留未归因证据

#### B5.4 operator 报表接口

主要改动：

- 新增 `/reports/position-lifecycle-profitability`
- 在 trial review details 中挂入 lifecycle 收益视图

当前落点：

- `aats/services/operator/query_service.py`
- `aats/api/routes.py`

验收标准：

- operator 能直接看到 lifecycle 汇总、最近 lifecycle、未归因 funding fee
- trial review details 中能直接看到这张报表

## 4. Task72-B6 清算距离与保证金缓冲控制面 v1

### 4.1 目标

- 把交易所真实仓位的强平距离和本地风控投影的保证金缓冲收成同一张风险视图
- 让 operator 能同时回答“现在距离强平多近”和“再下一笔下去离 only-reduce / 硬上限还有多远”

### 4.2 代码级开发子项

#### B6.1 交易所仓位强平距离汇总

主要改动：

- 基于 exchange positions 计算最近强平距离、最危险仓位、带强平价的仓位数量
- 对 long / short 使用不同方向的距离公式

当前落点：

- `aats/services/operator/query_service.py`

验收标准：

- 没有仓位时返回 `available=false`
- 有仓位时必须给出 `nearest_liquidation_gap_ratio`
- 最危险仓位必须保留 `symbol`、`pos_side`、`mark_price`、`liquidation_price`

#### B6.2 当前 / 预估保证金缓冲视图

主要改动：

- 从 exchange risk snapshot 计算当前 `initial_margin_usage_fraction`
- 从 latest risk decision 读取 `projected_margin_usage` 与 `liquidation_buffer_remaining`
- 生成 `buffer_to_only_reduce`、`buffer_to_hard_limit`、`status`

当前落点：

- `aats/services/operator/query_service.py`

验收标准：

- 能同时看到当前真实缓冲和下一笔投影缓冲
- 状态至少区分 `healthy / warning / critical`

#### B6.3 operator / risk API 暴露

主要改动：

- 新增 `/risk/margin-buffer`
- `account/state`、`system/runtime`、`trial_review_details` 增加 margin buffer 摘要
- 风控 payload 增加中文 `operator_summary` 的上下文数值

当前落点：

- `aats/services/operator/query_service.py`
- `aats/api/routes.py`
- `aats/api/static/modules/terms.js`

验收标准：

- operator 无需自己算公式，也能看懂当前风险位置
- 新增说明必须保持干净 UTF-8 中文

## 5. Task72-B7 资金费纳入 trial guard / forward validation / trial review v1

### 5.1 目标

- 让试盘守护不再只看成交净收益，而是看“成交净收益 + 资金费”的综合结果
- 保留成交样本量、滑点、费用率等交易质量指标，不把资金费和执行质量混为一谈

### 5.2 代码级开发子项

#### B7.1 trial guard 口径升级

主要改动：

- 新增 `daily_trading_net_realized`、`daily_funding_fee_net`、`daily_combined_net_realized`
- 当日亏损阈值改为看 combined net
- 保留连续亏损笔数仍只按 fill 统计

当前落点：

- `aats/services/governance_engine/trial_guard.py`

验收标准：

- 纯资金费拖累也能触发日亏损停机
- 不会因为 funding fee 事件把连续亏损笔数算乱

#### B7.2 forward validation / scaling readiness 升级

主要改动：

- 每个 forward validation period 增加 `funding_fee_net_pnl`、`combined_net_realized_pnl`
- recommendation 改成以 combined net 为主判断亏损边界

当前落点：

- `aats/services/operator/query_service.py`

验收标准：

- period summary 能区分交易净收益与资金费拖累
- pause / shrink 判定可以引用 combined net

#### B7.3 trial review 中文收口

主要改动：

- 修复 trial review action items 的乱码文案
- 在 summary / details 里补资金费口径说明

当前落点：

- `aats/services/operator/query_service.py`
- `aats/api/static/modules/views/risk-view.js`

验收标准：

- 试盘守护和 trial review 页面不再出现乱码
- operator 能明确看懂“试盘暂停是因为交易亏损还是资金费拖累”

## 6. 当前执行顺序

本批次默认顺序如下：

1. 先做 `Task72-B5`，补齐收益归因真相
2. 再做 `Task72-B6`，补齐当前风险与投影风险视图
3. 最后做 `Task72-B7`，把资金费真正接进试盘判断链

## 7. 当前完成状态

`Task72-B5` 已实现第一轮版本，当前能力包括：

- fill outcome 已保留 `position_key / position_mode / pos_side / settle_currency`
- operator 已新增 `position-lifecycle-profitability` 报表
- trial review details 已挂入生命周期收益视图
- funding fee 在能唯一匹配持仓时间窗时会归入 lifecycle，不能唯一归因时会保留 `unassigned` 证据

`Task72-B6` 已实现第一轮版本，当前能力包括：

- operator 已新增 `/risk/margin-buffer`
- `/account/state`、`/positions`、`/system/runtime` 已暴露 margin buffer 概览
- 风控 payload 已增加 projected margin buffer 上下文
- 风险页已展示综合净收益和保证金缓冲摘要

`Task72-B7` 已实现第一轮版本，当前能力包括：

- `trial_guard` 已新增 `daily_trading_net_realized / daily_funding_fee_net / daily_combined_net_realized`
- `forward_validation` 周期报表已新增 `funding_fee_net_pnl / combined_net_realized_pnl`
- `scaling_readiness` 的健康周期判定已切换到 combined net
- trial review action items 乱码已清理为干净 UTF-8 中文

## 8. 完成标志

当以下条件同时满足时，可以认为本批次完成：

- operator 能按持仓生命周期查看收益、手续费、资金费归因
- operator 能直接看到最近强平距离和保证金缓冲
- `trial_guard`、`forward_validation`、`trial_review` 已经采用资金费感知口径
- 相关中文文案没有编码污染
