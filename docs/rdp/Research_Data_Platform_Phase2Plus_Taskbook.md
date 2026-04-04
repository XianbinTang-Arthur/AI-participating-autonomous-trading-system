# Research Data Platform 后续 Phase 任务书（Phase 2+）

> 本文件是 `Research_Data_Platform_Taskbook.md` 的续篇，重点补齐 **Phase 2 及之后** 的建设任务。  
> 目标是把平台从“能 ingest、能 replay”推进到：  
> **能做系统化研究、能和 live 对照、能支撑执行真实性分析、能管理实验与数据治理、能长期运维。**

---

## 目录

- [1. 总体阶段图](#1-总体阶段图)
- [2. Phase 2：研究与回放能力产品化](#2-phase-2研究与回放能力产品化)
- [3. Phase 3：接入 live 运行数据并做归因闭环](#3-phase-3接入-live-运行数据并做归因闭环)
- [4. Phase 4：执行真实性与成交质量分析](#4-phase-4执行真实性与成交质量分析)
- [5. Phase 5：多数据域扩展与统一研究语义](#5-phase-5多数据域扩展与统一研究语义)
- [6. Phase 6：数据治理、运维与平台化能力](#6-phase-6数据治理运维与平台化能力)
- [7. 各阶段目录与模块补充](#7-各阶段目录与模块补充)
- [8. 各阶段脚本清单](#8-各阶段脚本清单)
- [9. 各阶段数据表补充](#9-各阶段数据表补充)
- [10. 各阶段验收标准](#10-各阶段验收标准)
- [11. 推荐实施顺序](#11-推荐实施顺序)

---

## 1. 总体阶段图

### 已有阶段
- **Phase 1**：数据底座建设  
  完成：
  - Bronze / Silver / Gold 分层
  - OKX K线与 funding ingest
  - 标准化、manifest、quality report
  - replay-ready dataset

### 后续阶段
- **Phase 2**：研究与回放能力产品化
- **Phase 3**：接入 live 运行数据并做归因闭环
- **Phase 4**：执行真实性与成交质量分析
- **Phase 5**：多数据域扩展与统一研究语义
- **Phase 6**：数据治理、运维与平台化能力

---

# 2. Phase 2：研究与回放能力产品化

## 2.1 阶段目标

把平台从“能 ingest、能产 Gold 数据”提升到：

1. 能系统地 replay `independent / directional`
2. 能做参数扫描
3. 能做 funding-adjusted edge 分析
4. 能生成可复现的实验结果
5. 能用统一数据口径输出研究报告

这阶段的关键词不是“更多数据”，而是：

> **让研究过程本身标准化、可复现、可批量化。**

---

## 2.2 本阶段交付物

- Replay Runner
- Replay Context Builder
- Experiment Registry
- Parameter Scan Pipeline
- Signal Diagnostics
- Edge Analysis
- Replay Result Dataset
- Markdown / CSV / JSON 报告生成器

---

## 2.3 模块任务拆分

### 模块 P2-A：Replay Core

目录：
`aats/data_platform/research/replay/`

---

### P2-A1 `replay_context_builder.py`

#### 职责
把 Gold replay-ready 数据构造为与 live 决策链尽可能接近的上下文。

#### 要解决的问题
- 历史 K线如何喂给现有 family 逻辑
- funding 如何附着到当前 bar
- decision 所需的上下文快照如何构造

#### 待办
- [ ] 定义 `ReplayBarContext`
- [ ] 定义 `ReplayDecisionContext`
- [ ] 实现 bar -> context 转换
- [ ] 支持 family 共用上下文
- [ ] 支持 funding-aware context
- [ ] 支持 parameter overrides 注入

#### 建议新增类
- `ReplayBarContext`
- `ReplayDecisionContext`
- `ReplayRuntimeOverrides`

---

### P2-A2 `replay_runner.py`

#### 职责
按时间顺序逐 bar 执行策略逻辑。

#### 待办
- [ ] 支持 family = `independent`
- [ ] 支持 family = `directional`
- [ ] 支持多 symbol 单独 replay
- [ ] 支持单次实验 parameter overrides
- [ ] 输出逐 bar 的策略决策
- [ ] 输出 summary stats
- [ ] 支持 dry-run portfolio tracking（可选，后置）

#### 最小输出
每根 bar：
- `state`
- `selectable`
- `execution_compatible`
- `long/short score`
- `blocking_reasons`
- `expected_net_edge_bps`
- `funding_adjusted_edge_bps`
- `target_position_qty`
- `delta_position_qty`

---

### P2-A3 `replay_result_writer.py`

#### 职责
把 replay 结果标准化写出。

#### 待办
- [ ] 写 parquet 结果
- [ ] 写 experiment manifest
- [ ] 生成 summary json
- [ ] 保存参数快照
- [ ] 保存数据集版本引用

---

## 2.4 模块 P2-B：实验管理

目录：
`aats/data_platform/research/experiments/`

---

### P2-B1 `models.py`

#### 职责
定义 experiment 规格。

#### 新增对象
- `ExperimentSpec`
- `ExperimentResultSummary`
- `ExperimentArtifactRef`

#### 字段建议
- experiment_id
- family
- symbol
- timeframe
- start_ts
- end_ts
- dataset_version
- parameter_overrides
- created_at
- runner_version
- result_dataset_path
- summary_path

---

### P2-B2 `registry.py`

#### 职责
实验注册表。

#### 待办
- [ ] experiment manifest 写入
- [ ] experiment 查询
- [ ] 通过 experiment_id 找到结果文件
- [ ] 支持同一时间窗不同参数版本对比

---

### P2-B3 `report_builder.py`

#### 职责
自动生成实验报告。

#### 报告内容建议
- 样本期
- family
- symbol/timeframe
- 参数覆盖
- opening 数量
- selectable 比例
- execution_compatible 比例
- top blocking reasons
- edge 分布
- funding-adjusted edge 分布

#### 输出格式
- Markdown
- CSV summary
- JSON summary

---

## 2.5 模块 P2-C：研究分析

目录：
`aats/data_platform/research/analytics/`

---

### P2-C1 `signal_diagnostics.py`

#### 职责
做信号与阻断诊断。

#### 核心问题
- independent 为什么开仓少
- directional 为什么多/少
- 主要阻断原因是什么
- 某参数变化后，阻断结构如何变化

#### 待办
- [ ] 统计 opening / blocked / selectable 比例
- [ ] 按小时聚合 blocking reasons
- [ ] family 对比
- [ ] symbol 对比
- [ ] 输出 diagnostics dataset

---

### P2-C2 `edge_analysis.py`

#### 职责
分析 edge 分布。

#### 待办
- [ ] 原始 `expected_net_edge_bps`
- [ ] funding-adjusted edge
- [ ] edge 分位数分析
- [ ] 低边际样本过滤效果分析
- [ ] family 间 edge 质量对比

---

### P2-C3 `parameter_scan.py`

#### 职责
对关键参数做批量扫描。

#### 第一批参数
- `min_confirm_ticks`
- `score_stability threshold`
- `min_safe_net_edge_bps`
- `liquidity_quality threshold`（如果已有）
- `execution_compatibility threshold`（如果适用）

#### 待办
- [ ] 定义参数网格输入格式
- [ ] 支持多组 experiment 自动跑
- [ ] 汇总结果表
- [ ] 输出 top-N 配置候选

---

## 2.6 脚本任务

### `scripts/replay_strategy.py`
#### 新任务
- [ ] 支持 family 参数
- [ ] 支持 dataset version 参数
- [ ] 支持 parameter override
- [ ] 输出 experiment artifact

### `scripts/scan_strategy_params.py`
#### 新任务
- [ ] 支持 JSON/YAML grid
- [ ] 支持多实验批跑
- [ ] 自动汇总 ranking

### `scripts/build_experiment_report.py`
#### 新增
- [ ] 读取 experiment_id
- [ ] 生成 Markdown 报告
- [ ] 生成 summary csv/json

---

## 2.7 Phase 2 验收标准

- [ ] 可以 replay `independent`
- [ ] 可以 replay `directional`
- [ ] 可以输出逐 bar 决策结果
- [ ] 可以做 funding-adjusted edge 分析
- [ ] 可以批量扫描参数
- [ ] 每次实验可复现、可追踪、可对比
- [ ] 能自动生成研究报告

---

# 3. Phase 3：接入 live 运行数据并做归因闭环

## 3.1 阶段目标

这是平台从“研究工具”升级为“研究-实盘闭环平台”的关键阶段。  
目标是把 live 运行数据接进同一体系，让你能回答：

- 为什么 replay 会开仓而 live 没开仓
- 为什么 live allocator 批了但 risk 拦了
- 为什么 budget 把某条意图压成 0
- 为什么 execution bundle blocked

---

## 3.2 本阶段交付物

- Live Data Sink
- Live-vs-Replay Diff Engine
- 决策层 / 风险层 / 执行层差异报告
- 结构化归因数据集

---

## 3.3 需要接入的 live 数据域

建议按优先级接入：

### 第一批必须接入
- `strategy_sleeve_intents`
- `portfolio_allocation_decisions`
- `allocator_budget_snapshots`
- `risk.decisions`
- `strategy_execution_bundles`
- `execution_orders`
- `execution_fills`
- `reconciliation_state_snapshots`

### 第二批建议接入
- `event_store`
- `portfolio_snapshots`
- `account state snapshots`
- `operator review items`

---

## 3.4 模块任务拆分

目录建议：
`aats/data_platform/live_bridge/`

---

### 模块 P3-A：Live Snapshot Extractor

#### 文件建议
- `live_strategy_intent_extractor.py`
- `live_risk_decision_extractor.py`
- `live_execution_extractor.py`
- `live_reconciliation_extractor.py`

#### 职责
从 PostgreSQL / event_store 抽取 live 数据，标准化后落入 Silver/Gold 风格表。

#### 待办
- [ ] 定义 live extract schema
- [ ] 按时间窗口抽取
- [ ] 输出结构化 parquet
- [ ] 记录 extract manifest

---

### 模块 P3-B：Live Canonicalization

#### 职责
把 live 事件统一成研究平台可消费的 canonical schema。

#### 需要标准化的东西
- symbol
- timestamp
- family
- sleeve
- route_action
- delta qty
- target qty
- risk rejection reasons
- bundle status
- order status
- fill status

---

### 模块 P3-C：Replay vs Live Diff

目录：
`aats/data_platform/research/analytics/live_vs_replay_diff.py`

#### 职责
做时间对齐与行为差异归因。

#### 需要回答的问题
- replay 有 opening，live 没有 → 差异发生在哪一层
- live 有 opening，bundle blocked → 是 risk 拦了还是 allocator/budget 变了
- replay edge 很高，live 最终没下单 → 是 permission / budget / risk / execution 里的谁挡了

#### 建议输出字段
- `ts`
- `symbol`
- `family`
- `replay_state`
- `live_state`
- `replay_target_qty`
- `live_target_qty`
- `replay_delta_qty`
- `live_delta_qty`
- `divergence_stage`
- `divergence_reason_codes`

---

## 3.5 新增 Gold / Analytics 数据集

### Gold-LiveDecisionFrame
按 bar 或时间窗口对齐的 live 决策帧：

- live signal
- live permission
- live budget
- live allocator
- live risk
- live execution result

### Gold-ReplayVsLiveFrame
把 replay 与 live 对齐后的归因表：

- replay opening?
- live opening?
- allocator approved?
- risk blocked?
- bundle blocked?
- orders created?
- fills created?

---

## 3.6 新增脚本

### `scripts/export_live_decision_window.py`
导出某一时间窗的 live canonical 数据。

### `scripts/compare_live_vs_replay.py`
对同一 symbol/timeframe/time window 做差异分析。

### `scripts/build_live_diff_report.py`
产出结构化 diff report。

---

## 3.7 Phase 3 验收标准

- [ ] 可以从 live DB/event_store 抽取关键运行数据
- [ ] 可以与 replay 输出按时间对齐
- [ ] 可以明确标注差异发生在哪一层
- [ ] 可以生成一份“为什么 live 没下单”的结构化报告
- [ ] independent / directional 至少各有一个完整差异案例

---

# 4. Phase 4：执行真实性与成交质量分析

## 4.1 阶段目标

在 Phase 3 之前，平台主要研究的是：

- 信号
- gating
- risk
- allocator
- replay

Phase 4 要开始回答：

> **就算决定要下单，这个下单在真实市场里到底会发生什么？**

也就是执行真实性问题。

---

## 4.2 本阶段重点

- 历史交易（trade prints）
- 订单簿（L2 / depth）
- 滑点建模
- 成交质量分析
- 小仓位/最小下单单位约束分析
- risk sizing 与最小可成交量的适配分析

---

## 4.3 新增数据域

### 历史交易
用于：
- 成交分布
- micro volatility
- 真实滑点建模

### 订单簿
用于：
- 流动性质量
- depth/imbalance
- queue position 粗估
- 可执行性判断

### instrument metadata
用于：
- tick size
- lot size
- min size
- leverage / margin specs

---

## 4.4 模块任务拆分

### 模块 P4-A：Trades Ingestion & Normalization
文件：
- `ingestion/okx/trades_ingestor.py`
- `normalization/trades_normalizer.py`

#### 待办
- [ ] 历史成交 raw ingest
- [ ] Silver trades schema
- [ ] 去重 / 排序 / UTC 统一
- [ ] quality report

---

### 模块 P4-B：Orderbook Ingestion & Normalization
文件：
- `ingestion/okx/orderbook_ingestor.py`
- `normalization/orderbook_normalizer.py`

#### 待办
- [ ] 订单簿原始解析
- [ ] 定义 snapshot schema
- [ ] 定义 depth levels 标准化
- [ ] quality report

---

### 模块 P4-C：Execution Realism
目录：
`aats/data_platform/research/execution/`

建议文件：
- `slippage_model.py`
- `liquidity_quality_metrics.py`
- `fill_feasibility.py`
- `min_size_feasibility.py`
- `execution_cost_analysis.py`

#### 要回答的问题
- 当前最小开仓量是否真的可成交
- budget / risk 批准的量是否会在盘口里太小或太大
- independent / directional 的信号是否落在“流动性差时段”
- execution cost 是否吞掉 edge

---

## 4.5 关键分析任务

### 任务 A：最小开仓量可行性分析
结合：
- account equity
- target leverage
- instrument min size
- tick/lot size
- required initial margin
- orderbook depth

回答：
- 当前账户规模下，哪种开仓量才是真正可执行的

### 任务 B：滑点与成交成本建模
根据 trades/orderbook 构建：
- passive / aggressive fill cost proxy
- spread cost
- impact cost

### 任务 C：流动性质量评分
将你现有的 `liquidity_quality` 判断与真实 depth/trade 数据对照，验证它是不是有预测性。

---

## 4.6 新增脚本

- `scripts/ingest_okx_trades.py`
- `scripts/ingest_okx_orderbook.py`
- `scripts/analyze_execution_cost.py`
- `scripts/analyze_min_trade_feasibility.py`
- `scripts/build_liquidity_quality_report.py`

---

## 4.7 Phase 4 验收标准

- [ ] 可以 ingest trades
- [ ] 可以 ingest orderbook snapshots
- [ ] 可以估算小仓位的成交可行性
- [ ] 可以输出 execution cost / slippage diagnostics
- [ ] 可以验证 liquidity_quality 规则是否有用

---

# 5. Phase 5：多数据域扩展与统一研究语义

## 5.1 阶段目标

把平台从“以 K线和 funding 为主的研究平台”，扩展成真正的**统一市场与运行研究平台**。

---

## 5.2 新扩展的数据域

### Market Domain
- candles
- trades
- orderbook
- mark price
- index price

### Carry Domain
- funding rate
- premium
- basis

### Instrument Domain
- contract specs
- lot size
- tick size
- leverage rules
- margin rules

### Account & Execution Domain
- balances
- positions
- orders
- fills
- margin snapshots
- risk decisions

### Research Domain
- replay inputs
- feature tables
- experiment outputs
- diagnostics outputs

---

## 5.3 统一研究语义任务

### 任务 A：统一 feature-ready 数据集
构建一个统一的 Gold/Feature 数据集，供：
- replay
- diagnostics
- parameter scan
- model research
共用。

### 任务 B：统一 family 对照框架
对不同 family 定义一致的输出列：
- state
- selectable
- execution_compatible
- blocking_reasons
- edge
- target/delta

### 任务 C：统一归因 taxonomy
定义一个统一的 divergence / blocking taxonomy，例如：

- strategy_gated
- permission_denied
- budget_zero_suppressed
- allocator_not_actionable
- risk_only_reduce
- execution_bundle_blocked
- order_submit_unknown
- reconciliation_resume_blocked

这样后面报表与 dashboard 不会各说各话。

---

## 5.4 建议新增模块

目录：
`aats/data_platform/taxonomy/`

文件：
- `blocking_reason_taxonomy.py`
- `divergence_taxonomy.py`
- `execution_reason_taxonomy.py`

---

## 5.5 Phase 5 验收标准

- [ ] 多数据域接入后仍保持统一 schema/manifest/version 规则
- [ ] 不同 family 可在同一研究框架下比较
- [ ] live 与 replay 差异分类有统一 taxonomy
- [ ] edge / cost / risk / execution 可在一套分析视图里串起来

---

# 6. Phase 6：数据治理、运维与平台化能力

## 6.1 阶段目标

把平台从“可用”推进到“长期可维护”。

---

## 6.2 本阶段关注的问题

- 数据更新调度
- 数据保留与归档
- 版本升级策略
- 质量告警
- 再运行与幂等
- 元数据管理
- 权限与共享

---

## 6.3 模块任务拆分

### 模块 P6-A：调度与更新
新增：
- `scheduler/`
- 或简化成 `scripts/run_daily_ingest.py`

#### 任务
- [ ] 日常增量 ingest
- [ ] 定时质量检查
- [ ] 自动构建最新 Gold replay dataset

---

### 模块 P6-B：数据保留与归档
#### 任务
- [ ] Bronze 长期保留策略
- [ ] Silver/Gold 历史版本保留策略
- [ ] 大文件压缩与归档规范

---

### 模块 P6-C：质量告警
#### 任务
- [ ] 缺失 bars 告警
- [ ] funding 断点告警
- [ ] orderbook snapshot 质量告警
- [ ] dataset build 失败告警

---

### 模块 P6-D：幂等与重建
#### 任务
- [ ] ingest 幂等
- [ ] normalize 幂等
- [ ] gold rebuild 幂等
- [ ] experiment rerun 幂等

---

### 模块 P6-E：文档与数据契约
#### 任务
- [ ] 数据 schema 文档
- [ ] manifest 文档
- [ ] dataset version 规则文档
- [ ] replay 语义文档
- [ ] live-vs-replay diff 文档

---

## 6.4 平台化脚本

- `scripts/run_daily_ingest.py`
- `scripts/rebuild_gold_datasets.py`
- `scripts/run_quality_checks.py`
- `scripts/archive_old_datasets.py`

---

## 6.5 Phase 6 验收标准

- [ ] 数据 ingest / normalize / gold build 可以稳定重复运行
- [ ] 缺失与异常数据会触发告警
- [ ] 数据版本升级可追踪
- [ ] 平台文档完整
- [ ] 新成员可以基于文档接入研究流程

---

# 7. 各阶段目录与模块补充

建议在后续阶段补充这些目录：

```text
aats/
  data_platform/
    live_bridge/
      __init__.py
      live_strategy_intent_extractor.py
      live_risk_decision_extractor.py
      live_execution_extractor.py
      live_reconciliation_extractor.py

    research/
      execution/
        __init__.py
        slippage_model.py
        liquidity_quality_metrics.py
        fill_feasibility.py
        min_size_feasibility.py
        execution_cost_analysis.py

    taxonomy/
      __init__.py
      blocking_reason_taxonomy.py
      divergence_taxonomy.py
      execution_reason_taxonomy.py

    scheduler/
      __init__.py
      daily_ingest.py
      rebuild_jobs.py
      quality_jobs.py
```

---

# 8. 各阶段脚本清单

## Phase 2
- `scripts/replay_strategy.py`
- `scripts/scan_strategy_params.py`
- `scripts/build_experiment_report.py`

## Phase 3
- `scripts/export_live_decision_window.py`
- `scripts/compare_live_vs_replay.py`
- `scripts/build_live_diff_report.py`

## Phase 4
- `scripts/ingest_okx_trades.py`
- `scripts/ingest_okx_orderbook.py`
- `scripts/analyze_execution_cost.py`
- `scripts/analyze_min_trade_feasibility.py`
- `scripts/build_liquidity_quality_report.py`

## Phase 6
- `scripts/run_daily_ingest.py`
- `scripts/rebuild_gold_datasets.py`
- `scripts/run_quality_checks.py`
- `scripts/archive_old_datasets.py`

---

# 9. 各阶段数据表补充

## Phase 3：Live Canonical Tables
建议新增：

### `GoldLiveDecisionFrame`
- ts
- symbol
- family
- permission_mode
- budget_zero_suppressed
- route_action
- allocator_approved
- risk_only_reduce_required
- bundle_status
- order_created
- fill_created

### `GoldReplayVsLiveFrame`
- ts
- symbol
- family
- replay_opening
- live_opening
- divergence_stage
- divergence_reason_codes

---

## Phase 4：Execution Datasets
### `SilverTradePrint`
- ts
- symbol
- price
- size
- side
- trade_id
- quality_flags

### `SilverOrderbookSnapshot`
- ts
- symbol
- bid_px_1..n
- bid_sz_1..n
- ask_px_1..n
- ask_sz_1..n
- quality_flags

---

# 10. 各阶段验收标准

## Phase 2
- [ ] replay runner 稳定
- [ ] funding-adjusted edge 可输出
- [ ] 参数扫描可批跑
- [ ] experiment 可复现
- [ ] 报告可自动生成

## Phase 3
- [ ] live 数据可抽取
- [ ] replay 与 live 可按时间窗对齐
- [ ] 差异可结构化归因
- [ ] 至少形成一份完整 live-vs-replay 归因报告

## Phase 4
- [ ] trades / orderbook 可 ingest
- [ ] 成交可行性分析可运行
- [ ] 流动性质量分析可运行
- [ ] execution cost 研究可运行

## Phase 5
- [ ] 多数据域统一 schema/manifest/version
- [ ] family 间可统一对比
- [ ] 统一 taxonomy 可用于报表与归因

## Phase 6
- [ ] 定时 ingest 可运行
- [ ] 质量检查可告警
- [ ] 数据重建可重复
- [ ] 文档与数据契约齐全

---

# 11. 推荐实施顺序

最推荐的推进顺序是：

1. **先完成 Phase 2**
   - 让平台真正能研究、能 replay、能扫参数

2. **再完成 Phase 3**
   - 让研究结果能和 live 对上，形成闭环

3. **再做 Phase 4**
   - 把 execution realism 接进来

4. **然后做 Phase 5**
   - 把多数据域与 taxonomy 统一

5. **最后做 Phase 6**
   - 把这套体系平台化、文档化、运维化

---

## 最后一段建议

如果只从“业务价值 / 时间投入比”来看，后续最值得你优先推进的是：

### 第一优先
**Phase 2：Replay + Parameter Scan + Experiment Registry**

### 第二优先
**Phase 3：Live vs Replay Diff**

因为你现在最需要的，不是更复杂的数据，而是：

> **把“为什么没下单、为什么 replay 和 live 不一致”变成可重复回答的问题。**

---

## 一句话结论

**Phase 1 只是把 Research Data Platform 的地基打出来；真正让它对你的交易系统产生持续价值的，是 Phase 2 的研究产品化、Phase 3 的 live 归因闭环、以及 Phase 4 之后的执行真实性与平台化治理。**
