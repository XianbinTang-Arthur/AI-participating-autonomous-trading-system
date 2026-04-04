# Phase 3 任务书（Live Attribution / Replay 对照归因）

## 1. 目标

在 Phase 2 完成参数研究闭环之后，进入 **Phase 3：Live Attribution / Replay 对照归因**。

Phase 3 的核心目标不是继续做参数扫描，而是建立一个标准化流程，用来回答：

> **为什么 live 没下单？为什么 replay 认为该开，live 却没开？差异具体出在策略、权限、预算、风控还是执行层？**

Phase 3 要把你过去依赖人工排查数据库、日志和事件流的工作，系统化为：

- 可重复执行
- 可结构化输出
- 可直接用于排障和复盘
- 可逐层归因

---

## 2. Phase 3 的定位

### 2.1 它不是 Phase 2 的延长
Phase 2 关注的是：
- 参数研究
- 历史 replay
- calibration
- scan
- family/timeframe 比较

Phase 3 关注的是：
- replay vs live 差异
- 未下单原因归因
- 真实交易链路解释
- 分层 blame / attribution

### 2.2 它也不是 Phase 4
Phase 3 暂时不碰：
- trades
- orderbook
- fill simulation
- slippage realism
- execution cost realism

这些属于 Phase 4。

---

## 3. Phase 3 要解决的核心问题

Phase 3 必须能回答以下问题：

1. 某个时间窗里，live 为什么没有 opening
2. replay 里出现 opening 时，live 卡在哪一层
3. live 与 replay 的差异主要集中在哪些 gate
4. 某次“没下单”是否是：
   - strategy blocked
   - permission disabled
   - allocator rejected
   - budget insufficient
   - risk only-reduce / margin gate
   - execution blocked
5. 对于 independent 与 directional，各自常见的卡点结构是什么

---

## 4. Phase 3 的固定范围

第一版 Phase 3 只支持：

- `symbol = BTC-USDT-SWAP`
- `timeframes = 15m, 1H`
- `families = independent, directional`

第一版不支持：

- ETH
- spot
- execution realism
- orderbook 对照
- fill feasibility 仿真

---

## 5. Phase 3 的总体思路

建立一条标准化归因链：

```text
Replay Decision
  -> Live Strategy Intent
  -> Permission / Auto Control
  -> Allocator Decision
  -> Budget / Exposure Gate
  -> Risk Decision
  -> Execution Bundle
  -> Execution Order / Fill
```

每个阶段都要能回答两件事：

1. 这一层是否通过
2. 如果没通过，具体 reason 是什么

最终形成：

> **Per-window / per-family / per-event 的 attribution report**

---

## 6. Phase 3 的核心交付物

Phase 3 至少产出：

```text
artifacts/research/attribution_rounds/<round_id>/
  round_manifest.json
  replay_live_alignment.csv
  replay_live_alignment.json
  attribution_summary.csv
  attribution_summary.json
  top_failure_modes.json
  phase3_live_attribution_conclusion.md
```

### 文件说明

#### `replay_live_alignment.*`
逐 event / 逐窗口对齐 replay 与 live 的结构化记录。

#### `attribution_summary.*`
按 family / timeframe / reason 聚合后的摘要。

#### `top_failure_modes.json`
最常见的未下单原因统计。

#### `phase3_live_attribution_conclusion.md`
最终结论文档。

---

## 7. Phase 3 分成 4 个子阶段

### Phase 3-A：Replay / Live 对齐模型
目标：
- 定义 replay 与 live 的对齐粒度
- 明确 event matching 规则

交付：
- 对齐规则文档
- alignment schema
- 第一版对齐输出

---

### Phase 3-B：分层 attribution 引擎
目标：
- 建立 strategy / permission / allocator / budget / risk / execution 的归因框架

交付：
- attribution reason taxonomy
- per-layer attribution logic
- aggregated summary

---

### Phase 3-C：未下单原因报告
目标：
- 对“最近为什么没下单”形成标准报告

交付：
- one-shot attribution runner
- top failure modes
- human-readable report

---

### Phase 3-D：family / timeframe 差异比较
目标：
- 对 independent / directional
- 对 15m / 1H
做结构化失败模式对比

交付：
- family-timeframe attribution comparison
- default failure mode catalog

---

## 8. 需要新增的脚本

建议新增：

```text
scripts/rdp_run_live_attribution.py
scripts/rdp_run_phase3_round.py
```

### 脚本职责

#### `rdp_run_live_attribution.py`
用于对某个时间窗 / family 做一次 one-shot live attribution。

#### `rdp_run_phase3_round.py`
用于批量跑一轮 Phase 3 归因 round，并输出汇总产物。

---

## 9. 建议新增的模块目录

```text
aats/data_platform/attribution/
  alignment.py
  taxonomy.py
  layer_classifier.py
  aggregation.py
  report_builder.py
  phase3_round_runner.py
```

---

## 10. 必须复用的现有能力

Phase 3 不是从零开始，它必须复用：

### 10.1 Replay 相关
- replay runner
- adapters
- diagnostics output
- experiment artifacts

### 10.2 现有 live 数据来源
- `strategy_sleeve_intents`
- `portfolio_allocation_decisions`
- `strategy_execution_bundles`
- `execution_orders`
- `execution_fills`
- `reconciliation_state_snapshots`
- `allocator_budget_snapshots`
- `event_store`

这些是你此前 live 排查已经反复使用的事实来源。

### 10.3 不允许重写
- replay 主链
- 交易系统生产逻辑
- 现有 research schema

---

## 11. Phase 3-A：Replay / Live 对齐模型任务

### 11.1 目标
明确“一个 replay decision 对应哪个 live intent / bundle / risk decision”。

### 11.2 要解决的问题
- 对齐是按 bar 时间、窗口、还是最近 intent 匹配
- 一条 replay opening 对应多个 live intent 时怎么处理
- live 没有 opening 时如何表示 unmatched

### 11.3 第一版建议
第一版采用：

- `family`
- `symbol`
- `timeframe`
- `ts/window_ts`

为主键进行近邻匹配

允许：
- replay-only
- live-only
- aligned

### 11.4 输出
`replay_live_alignment.csv` 中每条至少包含：

- `family`
- `symbol`
- `timeframe`
- `replay_ts`
- `live_ts`
- `alignment_status` (`aligned` / `replay_only` / `live_only`)
- `replay_state`
- `live_state`
- `replay_opening`
- `live_opening`

---

## 12. Phase 3-B：Attribution Taxonomy 任务

### 12.1 目标
定义统一归因分类，不允许一会儿说 margin issue，一会儿说 risk blocked，口径不一致。

### 12.2 建议 taxonomy

```text
strategy_blocked
permission_disabled
allocator_rejected
budget_rejected
risk_rejected
execution_blocked
execution_not_emitted
order_not_created
fill_not_observed
reconciliation_restricted
unknown
```

### 12.3 每类应有标准 reason code
例如：

#### `strategy_blocked`
- `score_not_stable`
- `score_below_entry_threshold`
- `net_edge_below_safe_minimum`
- `liquidity_quality_below_minimum`

#### `permission_disabled`
- `automatic_enabled_false`
- `family_disabled`
- `override_not_allowed`

#### `allocator_rejected`
- `family_not_approved`
- `approved_delta_zero`

#### `budget_rejected`
- `budget_multiplier_zero`
- `exposure_limit_reached`

#### `risk_rejected`
- `insufficient_initial_margin`
- `liquidation_buffer_breached`
- `only_reduce_required`
- `leg_only_reduce_mode_active`

#### `execution_blocked`
- `bundle_blocked`
- `execution_guard_failed`

---

## 13. Phase 3-C：分层 attribution 逻辑任务

### 13.1 目标
建立“由上往下”的 attribution 顺序。

### 13.2 固定顺序
对每个 replay opening / live candidate，依次判断：

1. replay 是否想开
2. live strategy 是否也想开
3. permission 是否允许
4. allocator 是否批准
5. budget 是否允许
6. risk 是否允许
7. execution bundle 是否放行
8. order 是否创建
9. fill 是否出现

### 13.3 归因原则
**归因停在第一层失败处**。

例如：

- strategy 都没放行  
  -> 归因到 `strategy_blocked`

- strategy 放行，allocator 没批  
  -> 归因到 `allocator_rejected`

- allocator 批了，risk only-reduce  
  -> 归因到 `risk_rejected`

不要跨层乱归因。

---

## 14. Phase 3-D：one-shot attribution runner 任务

### 14.1 脚本
```text
scripts/rdp_run_live_attribution.py
```

### 14.2 输入
建议支持：

- `--family`
- `--symbol`
- `--timeframe`
- `--start`
- `--end`
- `--dataset-version`
- `--live-source-window`
- `--artifact-root`

### 14.3 输出
至少生成：

- `replay_live_alignment.csv`
- `attribution_summary.json`
- `top_failure_modes.json`
- `live_attribution_report.md`

### 14.4 使用场景
这个脚本直接服务你最常见的问题：

> 最近为什么没下单？

---

## 15. Phase 3-E：Phase 3 round runner 任务

### 15.1 脚本
```text
scripts/rdp_run_phase3_round.py
```

### 15.2 目标
批量对以下组合做 attribution：

- independent / 15m
- independent / 1H
- directional / 15m
- directional / 1H

### 15.3 输出
- `family_timeframe_attribution_summary.csv`
- `phase3_live_attribution_conclusion.md`

---

## 16. 输出字段要求

### 16.1 `replay_live_alignment.csv`
每行至少包含：

- `family`
- `symbol`
- `timeframe`
- `replay_ts`
- `live_ts`
- `alignment_status`
- `replay_opening`
- `live_opening`
- `final_attribution_category`
- `final_attribution_reason`
- `strategy_reason`
- `permission_reason`
- `allocator_reason`
- `budget_reason`
- `risk_reason`
- `execution_reason`
- `order_status`
- `fill_status`

### 16.2 `attribution_summary.csv`
至少包含聚合维度：

- `family`
- `timeframe`
- `category`
- `reason`
- `count`
- `ratio`

---

## 17. 需要生成的结论文档

### 文件
```text
phase3_live_attribution_conclusion.md
```

### 结构建议

#### 17.1 Scope
- symbol
- family
- timeframe
- time window

#### 17.2 What was aligned
- replay events count
- live events count
- aligned count
- replay-only count
- live-only count

#### 17.3 Top failure modes
- top 5 attribution categories
- top 10 reason codes

#### 17.4 Layer analysis
- strategy layer
- allocator layer
- risk layer
- execution layer

#### 17.5 Family / timeframe differences
- independent vs directional
- 15m vs 1H

#### 17.6 Key findings
- 例如：
  - “最近不下单主因是 risk only-reduce”
  - “directional 当前更多卡在 strategy gate”
  - “1H 比 15m 更少出现 allocator rejection”

#### 17.7 Next step
- 进入 Phase 4 execution realism

---

## 18. 最小实现范围

第一版只要求：

1. 能对 `BTC-USDT-SWAP` 做 replay/live 对齐
2. 能对 `independent` 与 `directional` 做 attribution
3. 能输出 structured attribution summary
4. 能生成 human-readable attribution report
5. 能回答“最近为什么没下单”

第一版不要求：

- ETH
- spot
- orderbook
- fill simulation
- multi-exchange
- dashboard / UI
- 新数据库表

---

## 19. 实现约束

### 19.1 不允许修改生产交易逻辑
Phase 3 是归因与分析层，不改 live trading system。

### 19.2 不允许引入新数据库表作为前置
第一版直接读现有 live 表与 research artifacts 即可。

### 19.3 不允许混入 execution realism
不要提前接：
- orderbook
- trades
- slippage simulation

### 19.4 不允许跳过 taxonomy 直接硬写 report
必须先有：
- attribution taxonomy
- per-layer attribution logic

---

## 20. 验收标准

Phase 3 第一版通过条件：

1. `rdp_run_live_attribution.py` 可运行
2. 对给定时间窗能输出：
   - `replay_live_alignment.csv`
   - `attribution_summary.json`
   - `top_failure_modes.json`
   - `live_attribution_report.md`
3. 能对至少一个真实 case 解释：
   - 为什么 replay opening 但 live 没下单
4. `rdp_run_phase3_round.py` 能批量产出 family/timeframe 对比总结
5. `phase3_live_attribution_conclusion.md` 可直接供人阅读

---

## 21. 一句话总结

Phase 3 的职责是：

> **把“为什么 live 没下单”从人工排查，升级成标准化 replay/live 对照归因流程，并形成结构化失败模式与结论文档。**
