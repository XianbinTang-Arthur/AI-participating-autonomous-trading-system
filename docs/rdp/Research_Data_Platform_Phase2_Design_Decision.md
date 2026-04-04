# Research Data Platform — Phase 2 设计决策文档（正式版）

> 本文档用于冻结 Research Data Platform **Phase 2** 的设计边界与实施方向。  
> 目标是让 Claude Code / Codex / 人工开发者在不重新讨论高层方向的前提下，直接进入实现阶段。
>
> 本文档承接 Phase 1 已完成的数据底座能力，包括：
>
> - 历史文件 backfill
> - API 增量采集
> - PostgreSQL 主仓库
> - `meta / staging / bronze / silver / gold` 分层
> - candles / funding 数据域
> - Phase 1 schema freeze
> - Gold replay bars 基础能力

---

## 目录

- [1. 文档目标](#1-文档目标)
- [2. Phase 2 的定位](#2-phase-2-的定位)
- [3. Phase 2 的核心目标](#3-phase-2-的核心目标)
- [4. Phase 2 的首批范围](#4-phase-2-的首批范围)
- [5. Phase 2 不做什么](#5-phase-2-不做什么)
- [6. Phase 2 总体架构](#6-phase-2-总体架构)
- [7. Phase 2 的 6 个核心模块](#7-phase-2-的-6-个核心模块)
- [8. Replay Core 设计决策](#8-replay-core-设计决策)
- [9. Strategy Adapter Layer 设计决策](#9-strategy-adapter-layer-设计决策)
- [10. Experiment Registry 设计决策](#10-experiment-registry-设计决策)
- [11. Parameter Scan Engine 设计决策](#11-parameter-scan-engine-设计决策)
- [12. Diagnostics Engine 设计决策](#12-diagnostics-engine-设计决策)
- [13. Report Builder 设计决策](#13-report-builder-设计决策)
- [14. 实验结果存储策略](#14-实验结果存储策略)
- [15. 第一批参数与取值范围](#15-第一批参数与取值范围)
- [16. 实施顺序](#16-实施顺序)
- [17. 第一阶段验收标准](#17-第一阶段验收标准)
- [18. 对 Claude Code 的实现约束](#18-对-claude-code-的实现约束)
- [19. 后续阶段边界](#19-后续阶段边界)
- [20. 结论](#20-结论)

---

## 1. 文档目标

本文档只做一件事：

> **冻结 Phase 2 的设计决策。**

本文档要回答的问题包括：

1. Phase 2 到底要解决什么问题
2. Phase 2 先支持哪些市场 / family / timeframe
3. Phase 2 的核心模块是什么
4. 实验结果应该怎么记录和存储
5. 第一批参数扫描该扫什么
6. 开发顺序和验收标准是什么

本文档不负责编写具体代码，也不替代后续任务书和 issue backlog。

---

## 2. Phase 2 的定位

### 2.1 Phase 2 的本质
Phase 2 不是继续做数据接入，也不是继续修 Phase 1 的底层表结构。  
Phase 2 的本质是：

> **把 Phase 1 的数据底座升级为参数研究平台。**

### 2.2 为什么要做 Phase 2
Phase 1 解决了：
- 数据如何稳定进入系统
- 历史数据与 API 数据如何统一
- canonical schema 如何落库
- replay-ready Gold bars 如何生成

但 Phase 1 还没有解决：

- 策略如何稳定 replay
- 参数如何批量扫描
- 实验如何登记与复现
- 结果如何诊断和比较
- 如何用历史数据反哺参数决策

因此，Phase 2 的重点是：

> **让历史数据真正为参数选择服务。**

---

## 3. Phase 2 的核心目标

### 3.1 一句话目标
建立一个以 `BTC-USDT-SWAP` 的 `15m / 1H` 为首批范围、以 `independent` 为首批重点 family 的参数研究平台，使策略参数可以通过历史 replay、批量实验、结构化诊断和自动报告形成闭环，并为后续适配 `directional` 及更多策略提供统一框架。

### 3.2 更具体地说，Phase 2 要做到
1. 能对历史 Gold replay bars 做逐 bar replay
2. 能支持 `independent` 与 `directional` 两个策略 family 的统一接入
3. 能记录每次实验的元数据与产物路径
4. 能批量扫描关键参数
5. 能输出阻断结构、edge 分布、开仓分布等诊断结果
6. 能自动生成研究报告

---

## 4. Phase 2 的首批范围

### 4.1 市场范围
首批只支持：

- `BTC-USDT-SWAP`

### 4.2 时间周期
首批只支持：

- `15m`
- `1H`

### 4.3 策略范围
#### 架构目标
必须同时兼容：
- `independent`
- `directional`

#### 实现优先级
先偏：
- `independent`

也就是说：
- **架构上**：两个 family 都要能接
- **优先实现上**：先把 independent 路径做深做稳

### 4.4 数据输入
首批 Phase 2 的 replay 输入来自：

- `gold.market_swap_replay_bars_15m`
- `gold.market_swap_replay_bars_1h`

不直接读取 Bronze/Silver 作为 replay 主输入。

---

## 5. Phase 2 不做什么

为了防止 scope 膨胀，Phase 2 明确不做以下内容：

### 5.1 不做 live-vs-replay 归因
这属于后续阶段（Phase 3）。

### 5.2 不做 trades / orderbook / slippage realism
这属于后续阶段（Phase 4）。

### 5.3 不做 dashboard / Web UI
Phase 2 先以文件产物和 registry 为主。

### 5.4 不做超重型实验平台
不做：
- 大规模调度系统
- 分布式实验框架
- 复杂实验工作流引擎

### 5.5 不做全市场 / 全策略 / 全周期一口气铺开
首批只聚焦：
- `BTC-USDT-SWAP`
- `15m / 1H`
- `independent` 优先
- `directional` 兼容

---

## 6. Phase 2 总体架构

Phase 2 建立在 Phase 1 之上，采用如下高层结构：

`Gold replay bars -> Replay Core -> Experiment Registry -> Diagnostics -> Parameter Scan -> Report Builder`

其中：

- Replay Core 提供逐 bar 决策重放能力
- Strategy Adapter Layer 负责 family 适配
- Experiment Registry 负责元数据与产物管理
- Diagnostics Engine 提供解释层
- Parameter Scan Engine 提供批量扫描能力
- Report Builder 提供交付层

---

## 7. Phase 2 的 6 个核心模块

Phase 2 冻结为以下 6 个核心模块：

1. **Replay Core**
2. **Strategy Adapter Layer**
3. **Experiment Registry**
4. **Parameter Scan Engine**
5. **Diagnostics Engine**
6. **Report Builder**

任何 Phase 2 实现都不得跳过这些模块边界，直接把逻辑写成零散脚本。

---

## 8. Replay Core 设计决策

### 8.1 职责
Replay Core 负责：
- 从 Gold replay bars 读取历史 bar
- 按时间顺序逐 bar 重放
- 调用 family adapter
- 输出逐 bar 决策结果

### 8.2 输入
最小输入包括：
- family
- symbol
- timeframe
- data window
- dataset version
- parameter overrides

### 8.3 输出
每根 bar 至少输出以下字段：

- `ts`
- `family`
- `symbol`
- `timeframe`
- `state`
- `selectable`
- `execution_compatible`
- `long_score`
- `short_score`
- `blocking_reasons`
- `expected_net_edge_bps`
- `target_position_qty`
- `delta_position_qty`

### 8.4 边界
Replay Core 在 Phase 2 不负责：
- 完整撮合仿真
- 完整 PnL accounting
- 滑点模型
- order book realism

这些属于后续阶段。

### 8.5 首批实现要求
至少能对：
- `independent`
- `BTC-USDT-SWAP`
- `15m`
跑通一段时间窗 replay。

---

## 9. Strategy Adapter Layer 设计决策

### 9.1 目标
为了避免 replay 框架只认识 `independent`，Phase 2 必须引入 Strategy Adapter Layer。

### 9.2 职责
负责把不同 family 包装成统一接口。

### 9.3 统一适配输出
每个 family adapter 至少应输出：

- `state`
- `selectable`
- `execution_compatible`
- `long_score`
- `short_score`
- `blocking_reasons`
- `expected_net_edge_bps`
- `target_position_qty`
- `delta_position_qty`

### 9.4 首批支持策略
- `independent`：优先打通
- `directional`：接口必须能接，尽快适配

### 9.5 约束
不允许把 replay runner 写死成只支持某一个 family。

---

## 10. Experiment Registry 设计决策

### 10.1 目标
每次实验必须可追踪、可比较、可复现。

### 10.2 存储策略
Experiment Registry 存入 PostgreSQL。

### 10.3 至少记录的字段
每条 experiment 至少包含：

- `experiment_id`
- `family`
- `symbol`
- `timeframe`
- `dataset_version`
- `parameter_overrides`
- `run_started_at`
- `run_finished_at`
- `status`
- `result_path`
- `summary_path`
- `report_path`

### 10.4 Registry 的职责
Registry 只负责：
- 元数据
- 产物引用
- 状态

Registry 不负责存放大体量逐 bar replay 数据。

---

## 11. Parameter Scan Engine 设计决策

### 11.1 目标
支持对关键参数做批量实验，并生成结构化对比结果。

### 11.2 第一阶段原则
Phase 2 第一阶段只支持：
- 小规模参数网格
- 少量参数组合
- 以解释性为优先

不做：
- 复杂搜索算法
- 黑盒优化器
- 大规模分布式并行

### 11.3 扫描对象
首批重点偏：
- `independent`

但框架应允许后续扩展到：
- `directional`
- 其他策略 family

### 11.4 输出
每组参数都应形成：
- 一个 experiment entry
- 一个 result artifact
- 一个 diagnostics summary
- 一个 report artifact

---

## 12. Diagnostics Engine 设计决策

### 12.1 目标
让参数研究结果可解释，而不是只看一个结果分数。

### 12.2 第一批必须支持的诊断指标
至少包括：

- `opening_count`
- `blocked_count`
- `selectable_ratio`
- `execution_compatible_ratio`
- `blocking_reasons_top_n`
- `score_distribution`
- `expected_edge_distribution`

### 12.3 设计原则
Diagnostics Engine 必须是独立模块，不允许把这些统计逻辑散落在 report builder 或 parameter scan runner 里。

### 12.4 为什么重要
你做数仓和 replay 的真正目的，不是排查几个 isolated bug，而是：

> **找到适合的参数，并理解这些参数如何影响真实历史机会结构。**

因此 diagnostics 是 Phase 2 的核心，不是附属功能。

---

## 13. Report Builder 设计决策

### 13.1 目标
把每次 experiment 的结果转成可交付、可阅读的研究报告。

### 13.2 第一阶段输出格式
必须支持：

- Markdown report
- JSON summary
- CSV summary

### 13.3 每份报告至少包含
- experiment 基本信息
- family
- symbol
- timeframe
- dataset version
- parameter overrides
- opening / blocked / selectable 统计
- blocking reasons top N
- edge summary
- 核心结论
- caveats

### 13.4 边界
Report Builder 不做 dashboard，不做交互式 UI。

---

## 14. 实验结果存储策略

### 14.1 总原则
采用：

> **Registry 进库，结果落文件。**

### 14.2 进 PostgreSQL 的内容
只存：
- experiment metadata
- status
- artifact references
- summary metadata

### 14.3 文件落盘的内容
大体量输出先落文件，例如：
- replay decisions
- diagnostics raw outputs
- comparison tables
- reports

建议文件格式包括：
- parquet
- csv
- json
- md

### 14.4 为什么这样做
因为 Phase 2 的核心是参数研究闭环，不是先建一个庞大的实验结果数据库。  
过早把所有逐 bar 数据表化，会带来：
- schema 膨胀
- migration 膨胀
- 结构锁死
- 结果迭代困难

---

## 15. 第一批参数与取值范围

Phase 2 第一批冻结为以下 3 个参数：

### 15.1 `min_confirm_ticks`
建议取值：
- `2`
- `3`
- `4`

### 15.2 `score_stability_threshold`
建议取值：
- `2.0`
- `5.0`
- `10.0`

### 15.3 `min_safe_net_edge_bps`
建议取值：
- `5`
- `10`
- `15`

### 15.4 为什么选这 3 个
这 3 个参数分别覆盖：

#### `min_confirm_ticks`
信号确认强度

#### `score_stability_threshold`
强信号是否被过度拦截

#### `min_safe_net_edge_bps`
边缘机会放行下限

这 3 个参数足够形成第一批有解释力的参数研究实验，又不会使第一阶段规模失控。

---

## 16. 实施顺序

Phase 2 必须按以下顺序推进：

### Step 1：Replay Core
先让 replay 真能跑。

### Step 2：Strategy Adapter Layer
先打通 `independent`，同时让 `directional` 有统一适配入口。

### Step 3：Experiment Registry
每次 replay 都必须登记 experiment 元数据。

### Step 4：Diagnostics Engine
先让 replay 结果可解释。

### Step 5：Parameter Scan Engine
在 replay + registry + diagnostics 都稳定之后，再做参数扫描。

### Step 6：Report Builder
最后把结果自动组织成报告。

### 不允许的顺序
- 先做 report builder 再做 diagnostics
- 先做 parameter scan 再补 experiment registry
- 先做 directional 大量扩展而 replay core 还不稳

---

## 17. 第一阶段验收标准

当以下条件全部满足时，Phase 2 第一阶段可视为完成：

### 必须满足
1. `independent` 能 replay `BTC-USDT-SWAP` 的 `15m`
2. `independent` 能 replay `BTC-USDT-SWAP` 的 `1H`
3. `directional` 至少完成 adapter 接口接入，或完成最小 replay 打通
4. 每次 experiment 都能登记到 registry
5. 至少能扫描 2 个关键参数
6. diagnostics 能输出：
   - opening count
   - blocking reasons
   - edge summary
7. 能自动生成至少一份 Markdown report

### 通过条件的本质
不是“代码写了”，而是：

> **参数研究闭环已跑通。**

---

## 18. 对 Claude Code 的实现约束

以下约束是 Phase 2 实现时必须遵守的：

### 18.1 不允许重写 Phase 1 主架构
不得擅自改动：
- PostgreSQL 主仓
- Phase 1 的 canonical schema
- Gold replay bars 输入地位

### 18.2 不允许把 Replay Core 写死成 independent-only
即便首批偏 independent，也必须保留 family adapter 结构。

### 18.3 不允许把所有 experiment 结果塞进数据库
必须遵守：
- registry 进库
- 大结果落文件

### 18.4 不允许在 Phase 2 混入 execution realism
不要接：
- trades
- orderbook
- slippage realism
- fill simulation

### 18.5 不允许一开始就泛化到全市场全周期
必须先把首批范围跑通：
- `BTC-USDT-SWAP`
- `15m`
- `1H`

---

## 19. 后续阶段边界

### Phase 3
- live attribution
- replay vs live diff
- strategy / permission / budget / risk / execution 分层归因

### Phase 4
- execution realism
- trades / orderbook
- slippage / fill feasibility
- execution cost analysis

### Phase 5+
- platform governance
- scheduling / alerts
- data contracts
- long-term maintenance

---

## 20. 结论

Phase 2 已正式定义为：

> **Parameter Research Platform**

它不是继续做数据接入，也不是泛化研究脚手架，而是围绕：

- `BTC-USDT-SWAP`
- `15m / 1H`
- `independent` 优先
- `directional` 兼容

建立一个以 replay、registry、parameter scan、diagnostics 和 report builder 为核心的参数研究闭环，使历史数据真正能够反哺参数选择与策略理解。

任何后续开发都应以本设计文档为准，不应在实现阶段重新讨论高层方向。
