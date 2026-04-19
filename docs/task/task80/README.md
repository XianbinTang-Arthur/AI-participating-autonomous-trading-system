# Task80 智能套利磨损模型与可执行收益框架升级任务书

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../../docs/project_positioning.md)。


## 1. 任务定位

`Task80` 用于把当前 `smart_arbitrage` 的粗粒度成本估算，升级成一套能同时服务于：

- 事前入场判断
- 事中 operator 解释
- 事后真实磨损归因
- replay / recovery 后校准

的统一磨损模型。

本任务不引入新的套利家族，不扩展为跨平台搬砖，也不把 `perp-perp` 合约价差套利塞进现有 `smart_arbitrage`。当前只覆盖现有 `spot + hedge(derivatives)` 智能套利主链，并兼容 `inventory_reverse_carry / margin_reverse_carry`。

## 2. 当前问题

### 2.1 已确认问题

1. 当前 `smart_arbitrage` 仍主要依赖单一 `smart_arbitrage_estimated_cost_bps` 或少量 `estimated_*_bps` 汇总字段。
2. 当前没有区分：
   - 理论收益 `Ideal`
   - 可执行收益 `Executable`
3. 当前没有把成本按阶段拆开：
   - 开仓
   - 持有
   - 平仓
4. 当前没有把成本按来源拆开：
   - 手续费
   - spread
   - slippage
   - execution mismatch
   - funding
   - borrow
   - transfer / time decay
5. 当前 funding 和 borrow 仍接近“平滑常数”，不具备事件/窗口语义。
6. 当前 operator / UI 不能明确回答：
   - 为什么这轮不做
   - 是价差不够，还是磨损吃掉了机会
   - 理论上可赚多少，可执行后还剩多少

### 2.2 生产风险

1. `estimated_cost_bps` 与未来细分成本项并存时，最容易出现重复计费或漏计费。
2. funding / borrow 如果继续按连续均值算，会高估或低估短周期套利的真实磨损。
3. 如果 `Ideal Edge` 和 `Executable Edge` 混用，最容易造成“理论可做、执行后亏钱”的错误开仓。
4. 如果预测成本和真实回填共用同一字段，回放和 operator 排障会失去解释力。

## 3. 任务目标

### 3.1 行为目标

- 入场判断改为以 `Executable Edge` 为主。
- 页面同时展示：
  - 理论收益
  - 可执行收益
  - 总磨损分解
- 缺数据时必须保守退化，不能乐观开仓。
- replay / attribution 需要能还原“预测磨损 vs 实际磨损”。

### 3.2 一致性目标

- `cost_model -> engine -> coordinator -> allocator -> operator -> UI` 统一使用同一套成本词汇。
- `estimated_*` 旧字段在兼容期仍保留，但语义明确映射到新框架。
- 预测值和真实回填值不得混字段。

## 4. 非目标

本任务默认不包含以下内容：

- `perp-perp` 跨合约套利
- 跨交易所 / 跨账户 / 跨链现货搬砖
- Treasury / transfer orchestration
- 重写 execution / replay / ledger 主链

## 5. 全链路范围

- `aats/services/strategy_engines/smart_arbitrage/schemas.py`
- `aats/services/strategy_engines/smart_arbitrage/cost_model.py`
- `aats/services/strategy_engines/smart_arbitrage/engine.py`
- `aats/services/strategy_engines/coordinator.py`
- `aats/services/strategy_engines/allocator.py`
- `aats/bootstrap/settings.py`
- `aats/services/operator/query_service.py`
- `aats/api/static/modules/views/strategy-view.js`
- `aats/api/static/modules/terms.js`
- `configs/strategy_profiles/*.yaml`
- 相关 unit / integration tests

## 6. 关键不变量

1. `state consistency`
   - 候选、sleeve intent、allocation 和 UI 必须对同一套利机会给出一致解释。
2. `balance/accounting integrity`
   - 新成本模型不能改变腿级执行量、仓位恢复或 obligation 语义。
3. `idempotency/retry safety`
   - 新增成本字段不得影响命令流幂等键、执行计划身份或 replay 主键。
4. `correct order lifecycle behavior`
   - 成本模型只能影响机会选择和排序，不能改坏既有双腿下单与恢复生命周期。
5. `cost meaning integrity`
   - 预测成本和真实成本必须分开记录，不能互相覆盖。

## 7. 重点排查点

### 7.1 手续费

- spot / hedge
- open / close
- maker / taker
- 账户费率 vs 配置兜底

最容易出问题：
- 开平仓重复计费
- 双腿只算了一条
- 账户费率和配置兜底被重复叠加

### 7.2 spread / slippage / execution mismatch

- 不能继续把三者混成一个总滑点
- 必须至少能回答“理论可赚，但实际不做”的主因

最容易出问题：
- spread 半边/整边口径混乱
- slippage 和 mismatch 双重计入

### 7.3 funding

- 优先按事件数建模，而不是按连续小时粗算
- 至少支持：
  - `expected_funding_events`
  - `funding_interval_hours`
  - `funding_rate_per_event`

最容易出问题：
- 支付/收取方向算反
- 把“历史 proxy”当成“已实现值”

### 7.4 borrow

- 必须按离散计息窗口建模
- 至少支持：
  - `expected_hold_hours`
  - `borrow_hour_windows`
  - `borrow_apr`
  - `interest_free_ratio`

最容易出问题：
- 仍按连续 `hold_hours` 线性粗算
- 忽略整点窗口带来的成本跳变

### 7.5 Ideal vs Executable

- `Ideal Edge` 只看显性规则成本
- `Executable Edge` 再扣执行磨损和时间磨损

最容易出问题：
- 继续让 `Ideal Edge` 参与开仓
- `estimated_cost_bps` 兜底与新细分项重复计费

### 7.6 预测 vs 实际

- 真实 fee / funding / borrow / fill slippage 需要能回填
- operator 至少能看到：
  - 预测磨损
  - 实际磨损
  - 偏差

最容易出问题：
- 真实值缺字段
- 预测/实际口径不一致
- UI 把“预测值”展示成“已实现”

## 8. 任务拆分

### Task80-A0 基线锁定

目标：
- 锁定当前 `estimated_cost_bps` 和旧字段兼容行为

验收：
- 有基线说明
- 有旧字段 fallback 回归测试

### Task80-A1 Schema 升级

目标：
- 新增统一 cost breakdown v2

核心字段：
- `ideal_open_fee_bps`
- `ideal_close_fee_bps`
- `ideal_total_fee_bps`
- `executable_spread_bps`
- `executable_slippage_bps`
- `execution_mismatch_bps`
- `funding_cost_bps`
- `borrow_cost_bps`
- `transfer_cost_bps`
- `time_decay_cost_bps`
- `ideal_total_cost_bps`
- `executable_total_drag_bps`
- `ideal_edge_bps`
- `executable_edge_bps`
- `breakeven_basis_bps`
- `cost_confidence`
- `cost_source_flags`
- `realized_fee_bps`
- `realized_funding_bps`
- `realized_borrow_bps`
- `realized_total_drag_bps`
- `predicted_vs_realized_total_drag_error_bps`

兼容要求：
- 保留 `estimated_*` 和 `net_edge_bps`

### Task80-A2 配置分层

目标：
- 把“规则成本来源”和“经验磨损参数”拆开

建议配置：
- `smart_arbitrage_fee_source_mode`
- `smart_arbitrage_funding_source_mode`
- `smart_arbitrage_borrow_source_mode`
- `smart_arbitrage_expected_hold_hours`
- `smart_arbitrage_funding_interval_hours`
- `smart_arbitrage_expected_funding_events`
- `trade_cost_spot_maker_fee_bps`
- `trade_cost_spot_taker_fee_bps`
- `trade_cost_margin_maker_fee_bps`
- `trade_cost_margin_taker_fee_bps`
- `trade_cost_derivatives_maker_fee_bps`
- `trade_cost_derivatives_taker_fee_bps`
- `trade_cost_spot_spread_bps`
- `trade_cost_spot_slippage_bps`
- `trade_cost_margin_spread_bps`
- `trade_cost_margin_slippage_bps`
- `trade_cost_derivatives_spread_bps`
- `trade_cost_derivatives_slippage_bps`
- `smart_arbitrage_estimated_execution_mismatch_bps`
- `smart_arbitrage_estimated_transfer_cost_bps`
- `smart_arbitrage_time_decay_bps_per_hour`
- `smart_arbitrage_estimated_borrow_apr`
- `smart_arbitrage_borrow_interest_free_ratio`

### Task80-A3 显性手续费模型

目标：
- 逐腿、逐阶段 fee 估算

规则：
- 优先账户费率
- 其次新细分 fee 字段
- 最后旧 `estimated_fee_bps`

### Task80-A4 执行磨损模型

目标：
- spread / slippage / mismatch 单独建模

规则：
- spread、slippage、mismatch 分字段
- 第一版允许配置驱动

### Task80-A5 funding 事件模型

目标：
- 支持 funding 事件数和持有窗口

规则：
- 缺少事件参数时，允许回退总 funding bps
- 有事件参数时，按事件数求和

### Task80-A6 borrow 离散计息模型

目标：
- 支持 APR + 离散窗口估算

规则：
- `margin_reverse_carry` 才计 borrow
- 缺 APR 时回退总 borrow bps

### Task80-A7 Ideal vs Executable 输出

目标：
- 统一输出理论收益与可执行收益

规则：
- `Executable Edge` 参与实际开仓判定
- `Ideal Edge` 仅用于解释

### Task80-A8 引擎接入

目标：
- `engine / coordinator / allocator` 改用新字段

规则：
- 排序优先看 `Executable Edge`
- 新增阻断原因：
  - `smart_arbitrage_executable_edge_negative`
  - `smart_arbitrage_drag_exceeds_basis`
  - `smart_arbitrage_funding_window_unfavorable`
  - `smart_arbitrage_borrow_window_unfavorable`

### Task80-A9 归因 / 回放 / 校准

目标：
- operator 可看到预测 vs 实际磨损

第一版要求：
- 真实 fee bps
- 真实 funding bps
- 真实总磨损 bps
- 预测 vs 实际误差

注：
- borrow 真实回填允许先保留为“缺失/待支持”

### Task80-A10 Operator / UI

目标：
- 页面可解释“为什么不做”

至少展示：
- 理论收益 vs 可执行收益
- 总磨损分解
- 本轮主阻断原因
- 成本来源
- 预测 vs 实际偏差

### Task80-A11 测试与上线门槛

必须覆盖：
- fee 双腿开平仓
- funding 事件数
- borrow 窗口计息
- 旧字段 fallback
- `Executable Edge` gating
- operator/runtime 配置透出
- UI 展示与文案
- 预测 vs 实际校准摘要

上线前门槛：
- `Executable Edge` 已接管开仓判断
- replay / operator / UI 对同一成本结果解释一致
- 老配置仍兼容

## 9. 建议实施顺序

1. A0
2. A1
3. A2
4. A3
5. A4
6. A5
7. A6
8. A7
9. A8
10. A9
11. A10
12. A11

## 10. 交付标准

本任务完成后，应能明确回答：

1. 当前这轮基差理论上值不值得做？
2. 扣掉可执行磨损后还值不值得做？
3. 不做的主因是手续费、滑点、funding 还是 borrow？
4. 预测磨损和真实磨损差了多少？
5. 当前智能套利页面展示的是预测值，还是已实现值？
