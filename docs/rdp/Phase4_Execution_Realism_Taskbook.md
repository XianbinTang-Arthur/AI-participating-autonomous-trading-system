# Phase 4 任务书（Execution Realism / 成交可行性研究）

## 1. 目标

在 Phase 3 完成 replay / live attribution 之后，进入 **Phase 4：Execution Realism / 成交可行性研究**。

Phase 4 的核心目标不是继续做参数研究，也不是继续做“为什么没下单”的归因，而是建立一个更接近真实市场成交条件的研究层，用来回答：

> **即使策略层想开，且 live 也允许下单，这笔单在真实市场微观结构下，是否真的可成交？实际成交成本可能是多少？**

Phase 4 要解决的问题包括：

1. 某个 replay opening 在真实 trades / orderbook 条件下是否可成交
2. 理论 edge 在考虑滑点、吃单深度、盘口冲击后还剩多少
3. 某个 family 的机会是否主要死在 execution feasibility，而不是 strategy quality
4. `15m / 1H` 上看起来成立的机会，在微观结构层面是否其实不可执行
5. 不同参数组合是否只是“纸面上更好”，但 execution realism 更差

---

## 2. Phase 4 的定位

### 2.1 它不是 Phase 2
Phase 2 关注：
- 历史 replay
- calibration
- parameter scan
- family/timeframe 比较

### 2.2 它不是 Phase 3
Phase 3 关注：
- replay vs live 差异
- 未下单归因
- strategy / allocator / risk / execution layer attribution

### 2.3 Phase 4 真正关注的是
- trades
- orderbook
- slippage
- fill feasibility
- execution cost realism

也就是说，Phase 4 进入的是：

> **市场微观结构层。**

---

## 3. Phase 4 要解决的核心问题

Phase 4 必须能回答以下问题：

1. 这笔 hypothetical order 按当时盘口是否有足够流动性
2. 如果吃单，平均成交价会落在哪里
3. 预估滑点是多少 bps
4. 扣除 execution cost 之后，net edge 是否仍为正
5. 某一类策略机会是否系统性依赖“盘口太理想”的假设
6. 某些参数配置是否只是增加了“不可成交机会”

---

## 4. Phase 4 的固定范围

第一版 Phase 4 只支持：

- `symbol = BTC-USDT-SWAP`
- `timeframes = 15m, 1H`
- `families = independent, directional`

并且只研究：

- swap
- 单一交易所（当前系统所用交易所）

第一版不支持：

- 多交易所
- ETH
- spot execution realism
- 跨市场路由
- portfolio-level optimal execution

---

## 5. Phase 4 的总体思路

建立一条 execution realism 分析链：

```text
Replay / Live Candidate
  -> Hypothetical Order Intent
  -> Market Snapshot (Trades / Orderbook)
  -> Fill Feasibility
  -> Slippage Estimate
  -> Execution Cost Estimate
  -> Net Realized Edge Proxy
  -> Execution Realism Report
```

重点是把“策略层想做的单”转成：

- 在当时盘口是否能做
- 做了以后实际代价是多少
- 这笔机会是否还值得做

---

## 6. Phase 4 的核心交付物

Phase 4 至少产出：

```text
artifacts/research/execution_rounds/<round_id>/
  round_manifest.json
  execution_alignment.csv
  fill_feasibility_summary.csv
  slippage_summary.csv
  execution_cost_summary.json
  execution_realism_comparison.csv
  phase4_execution_realism_conclusion.md
```

### 文件说明

#### `execution_alignment.csv`
每个 replay/live candidate 对应的 hypothetical order 与 market snapshot 对齐结果。

#### `fill_feasibility_summary.csv`
每笔机会是否可成交、可成交比例、吃单层数等摘要。

#### `slippage_summary.csv`
每笔机会的预估滑点。

#### `execution_cost_summary.json`
execution cost 的汇总输出。

#### `execution_realism_comparison.csv`
不同 family / timeframe / 参数组的 execution realism 比较。

#### `phase4_execution_realism_conclusion.md`
最终结论文档。

---

## 7. Phase 4 分成 4 个子阶段

### Phase 4-A：市场数据对齐层
目标：
- 建立 trades / orderbook / replay candidate 的对齐模型

交付：
- market snapshot schema
- replay/live candidate 到 market snapshot 的匹配逻辑

---

### Phase 4-B：Fill Feasibility 引擎
目标：
- 判断 hypothetical order 在当时盘口下是否可成交

交付：
- fill feasibility evaluator
- per-candidate feasibility result

---

### Phase 4-C：Slippage / Execution Cost 模型
目标：
- 估计实际执行成本

交付：
- slippage estimator
- execution cost model
- cost-adjusted edge summary

---

### Phase 4-D：execution realism 比较与收口
目标：
- family / timeframe / 参数组之间做 execution realism 比较
- 形成最终结论

交付：
- execution realism comparison
- Phase 4 结论文档

---

## 8. 需要新增的脚本

建议新增：

```text
scripts/rdp_run_execution_realism.py
scripts/rdp_run_phase4_round.py
```

### 脚本职责

#### `rdp_run_execution_realism.py`
对某个时间窗 / family / timeframe 做一次 one-shot execution realism 分析。

#### `rdp_run_phase4_round.py`
批量跑一轮 execution realism round，并输出汇总产物。

---

## 9. 建议新增的模块目录

```text
aats/data_platform/execution_realism/
  market_alignment.py
  fill_feasibility.py
  slippage_estimator.py
  execution_cost_model.py
  aggregation.py
  report_builder.py
  phase4_round_runner.py
```

---

## 10. 必须复用的现有能力

Phase 4 必须复用：

### 10.1 Replay / Research 产物
- replay decisions
- diagnostics
- parameter candidates
- Phase 2 artifacts

### 10.2 Live Attribution 产物
- replay/live alignment
- attribution outputs
- live cases from Phase 3

### 10.3 现有 live / market 数据来源
必须基于真实可获取的数据源，例如：
- trades
- orderbook snapshots / depth snapshots
- execution orders
- fills

### 10.4 不允许重写
- replay 主链
- attribution taxonomy
- parameter scan 主流程

---

## 11. Phase 4-A：市场数据对齐层任务

### 11.1 目标
定义如何把一条 candidate order 对齐到当时的市场快照。

### 11.2 要解决的问题
- 用什么时间点取 orderbook
- 如果没有精确 snapshot，怎么回退
- trade 流和 orderbook snapshot 同时存在时谁为主
- replay opening 和 live order 的对齐规则是否相同

### 11.3 第一版建议
采用：

- event ts 为 anchor
- 取最近可用 orderbook snapshot
- trades 作为补充流动性证据

### 11.4 输出字段
`execution_alignment.csv` 每条至少包含：

- `family`
- `symbol`
- `timeframe`
- `candidate_ts`
- `candidate_source` (`replay` / `live`)
- `candidate_side`
- `candidate_qty`
- `snapshot_ts`
- `trades_window_start`
- `trades_window_end`
- `alignment_status`

---

## 12. Phase 4-B：Fill Feasibility 引擎任务

### 12.1 目标
判断一笔 hypothetical order 是否在当时盘口下可成交。

### 12.2 第一版问题定义
至少回答：

- 是否能在 N bps 内完全成交
- 是否只能部分成交
- 需要吃几档
- 可成交量占目标量的比例

### 12.3 输出字段建议
`fill_feasibility_summary.csv` 每条至少包含：

- `candidate_id`
- `candidate_side`
- `candidate_qty`
- `book_depth_available_qty`
- `fillable_qty`
- `fillable_ratio`
- `levels_consumed`
- `full_fill_possible`
- `partial_fill_possible`
- `feasibility_category`

### 12.4 feasibility_category 建议
- `fully_fillable`
- `partially_fillable`
- `not_fillable`
- `insufficient_market_data`

---

## 13. Phase 4-C：Slippage 估计任务

### 13.1 目标
估计 hypothetical order 的实际滑点。

### 13.2 第一版方法
第一版不需要做复杂 market impact 模型。  
可以先用：

- 吃单穿档法
- 基于 orderbook depth 的 VWAP 估计
- trades 作为 sanity check

### 13.3 输出字段建议
`slippage_summary.csv` 每条至少包含：

- `candidate_id`
- `candidate_side`
- `candidate_qty`
- `arrival_mid_px`
- `estimated_fill_vwap_px`
- `estimated_slippage_bps`
- `estimated_fee_bps`
- `estimated_total_execution_cost_bps`

### 13.4 第一版原则
- 重在透明、可解释
- 不追求复杂 impact model
- 不要一开始就做黑盒 execution simulator

---

## 14. Phase 4-D：Execution Cost Model 任务

### 14.1 目标
把：
- slippage
- fee
- feasibility penalty

合成 execution cost realism 输出。

### 14.2 输出字段建议
`execution_cost_summary.json` 至少包含：

- `mean_slippage_bps`
- `median_slippage_bps`
- `mean_total_execution_cost_bps`
- `full_fill_ratio`
- `partial_fill_ratio`
- `not_fillable_ratio`

### 14.3 与 Phase 2 的关系
Phase 2 里的 cost model 只是简化参数。  
Phase 4 里的 execution cost 是：

> **基于真实市场快照估计出来的 cost realism。**

因此，Phase 4 要能回答：

- Phase 2 的默认 cost 是否过乐观
- 是否过保守
- 不同 family 是否应该用不同 execution cost 假设

---

## 15. Phase 4-E：Execution Realism Comparison 任务

### 15.1 目标
对以下维度做比较：

- independent vs directional
- 15m vs 1H
- 参数组 A vs 参数组 B

### 15.2 输出
`execution_realism_comparison.csv` 至少包含：

- `family`
- `timeframe`
- `parameter_set`
- `candidate_count`
- `full_fill_ratio`
- `partial_fill_ratio`
- `mean_slippage_bps`
- `mean_total_execution_cost_bps`
- `cost_adjusted_edge_proxy`
- `top_execution_failure_mode`

### 15.3 要回答的问题
- 哪类机会纸面上最好，但 execution realism 最差
- 哪类机会数量少，但更真实可执行
- 是否要用 execution realism 反向约束参数选择

---

## 16. 结论文档：`phase4_execution_realism_conclusion.md`

建议结构：

### 16.1 Scope
- symbol
- family
- timeframe
- windows

### 16.2 What was analyzed
- replay candidates
- live candidates
- market data coverage

### 16.3 Fill feasibility summary
- full fill ratio
- partial fill ratio
- not fillable ratio

### 16.4 Slippage summary
- average slippage
- distribution
- worst cases

### 16.5 Cost-adjusted edge analysis
- net edge after execution realism
- 哪些机会被 execution cost 吃掉

### 16.6 Family / timeframe comparison
- independent vs directional
- 15m vs 1H

### 16.7 Key findings
例如：
- “independent 在 15m 上 opening 较多，但 full fill ratio 偏低”
- “directional 在 1H 上机会少，但 cost-adjusted edge 更稳”
- “当前默认 7bps cost 对 15m independent 仍偏乐观 / 偏保守”

### 16.8 Next step
- Phase 5: platform governance / productionization
或
- execution-aware parameter selection

---

## 17. 最小实现范围

第一版只要求：

1. 能对 `BTC-USDT-SWAP` 做 execution realism 分析
2. 能处理 `independent` 与 `directional`
3. 能输出 fill feasibility + slippage + execution cost summary
4. 能生成比较表与结论文档

第一版不要求：

- 多交易所
- ETH
- spot
- 复杂 impact model
- optimal execution algorithm
- dashboard / UI
- 新数据库表

---

## 18. 实现约束

### 18.1 不允许跳过市场数据对齐直接写结论
必须先有：
- market alignment
- feasibility result
- slippage estimate

### 18.2 不允许把 execution realism 写成黑盒
第一版必须透明、可解释、可审查。

### 18.3 不允许修改 live trading system
Phase 4 是研究层，不改生产交易系统。

### 18.4 不允许过早泛化
第一版只做：
- BTC-USDT-SWAP
- 15m / 1H
- independent / directional

---

## 19. 验收标准

Phase 4 第一版通过条件：

1. `rdp_run_execution_realism.py` 可运行
2. 对给定时间窗能输出：
   - `execution_alignment.csv`
   - `fill_feasibility_summary.csv`
   - `slippage_summary.csv`
   - `execution_cost_summary.json`
   - `live_execution_realism_report.md`
3. `rdp_run_phase4_round.py` 能批量输出 family/timeframe 比较
4. `phase4_execution_realism_conclusion.md` 可直接供人阅读
5. 能对至少一个真实 case 解释：
   - 策略上看可做，但 execution realism 下并不值得做
   - 或者反之

---

## 20. 一句话总结

Phase 4 的职责是：

> **把“策略上看起来应该做的机会”推进到“在真实市场微观结构下是否真的值得做”的研究层，并形成可成交性、滑点和 execution cost 的标准化分析流程。**
