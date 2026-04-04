# Research Data Platform — Phase 2 任务书

> 本文档用于把 **Phase 2 设计决策** 落实为可执行任务。  
> 目标是让 Claude Code / Codex / 人工开发者可以直接按模块、目录、数据表、脚本和 milestone 开工。
>
> 本文档承接：
>
> - `Research_Data_Platform_Phase2_Design_Decision.md`
> - `Research_Data_Platform_Phase1_Master_README.md`
> - `Phase1_Collector_Normalize_Merge_Taskbook.md`
> - `Phase1_Implementation_Schedule_and_Issue_Backlog.md`
>
> 本文档聚焦：
>
> 1. Phase 2 要实现哪些模块
> 2. 每个模块的目录结构建议
> 3. 需要新增哪些数据表
> 4. 需要新增哪些脚本
> 5. milestone 如何拆分
> 6. 每个 milestone 的完成标准是什么

---

## 目录

- [1. 文档目标](#1-文档目标)
- [2. Phase 2 的核心任务定义](#2-phase-2-的核心任务定义)
- [3. Phase 2 模块划分](#3-phase-2-模块划分)
- [4. 推荐目录结构](#4-推荐目录结构)
- [5. Phase 2 数据存储策略](#5-phase-2-数据存储策略)
- [6. 需要新增的数据表](#6-需要新增的数据表)
- [7. Replay Core 任务书](#7-replay-core-任务书)
- [8. Strategy Adapter Layer 任务书](#8-strategy-adapter-layer-任务书)
- [9. Experiment Registry 任务书](#9-experiment-registry-任务书)
- [10. Parameter Scan Engine 任务书](#10-parameter-scan-engine-任务书)
- [11. Diagnostics Engine 任务书](#11-diagnostics-engine-任务书)
- [12. Report Builder 任务书](#12-report-builder-任务书)
- [13. 需要新增的脚本](#13-需要新增的脚本)
- [14. Milestone 划分](#14-milestone-划分)
- [15. 每个 Milestone 的完成标准](#15-每个-milestone-的完成标准)
- [16. Claude Code 实现约束](#16-claude-code-实现约束)
- [17. 非目标与边界](#17-非目标与边界)
- [18. 最终交付状态](#18-最终交付状态)

---

## 1. 文档目标

本文档不是高层设计文档，而是：

> **Phase 2 的实施任务书。**

它必须达到的标准是：

- 别人拿去能直接开工
- 模块边界足够清楚
- 数据落点足够清楚
- milestone 和验收标准足够清楚

---

## 2. Phase 2 的核心任务定义

Phase 2 的核心任务可以概括为：

> **围绕 `BTC-USDT-SWAP` 的 `15m / 1H` 数据，为 `independent` 和 `directional` 策略建立参数研究闭环。**

更具体地说，Phase 2 要完成：

1. Replay Core
2. Strategy Adapter Layer
3. Experiment Registry
4. Parameter Scan Engine
5. Diagnostics Engine
6. Report Builder

并明确：
- `independent` 优先
- `directional` 兼容
- registry 进库
- 大结果落文件

---

## 3. Phase 2 模块划分

### 3.1 Replay Core
负责逐 bar replay。

### 3.2 Strategy Adapter Layer
负责 family 适配。

### 3.3 Experiment Registry
负责 experiment metadata 与 artifact 路径管理。

### 3.4 Parameter Scan Engine
负责参数扫描与实验调度。

### 3.5 Diagnostics Engine
负责把 replay 结果转成可解释指标。

### 3.6 Report Builder
负责把结果整理成 Markdown / JSON / CSV 报告。

---

## 4. 推荐目录结构

建议在现有 `aats/data_platform/` 基础上新增：

```text
aats/
  data_platform/
    replay/
      core/
        replay_runner.py
        replay_context.py
        replay_result_writer.py
      adapters/
        base_adapter.py
        independent_adapter.py
        directional_adapter.py
      registry/
        experiment_registry.py
        artifact_registry.py
      scan/
        parameter_grid.py
        scan_runner.py
        comparison_builder.py
      diagnostics/
        replay_diagnostics.py
        blocking_reason_analysis.py
        edge_analysis.py
        summary_builder.py
      reports/
        markdown_report_builder.py
        json_summary_builder.py
        csv_summary_builder.py
```

### 4.1 目录边界说明
#### `replay/core`
放 replay 主流程，不放 family-specific 逻辑。

#### `replay/adapters`
放各策略 family 的适配层。

#### `replay/registry`
放 experiment 元数据登记。

#### `replay/scan`
放参数扫描引擎。

#### `replay/diagnostics`
放统计与诊断分析。

#### `replay/reports`
放最终报告输出。

---

## 5. Phase 2 数据存储策略

### 5.1 进 PostgreSQL 的内容
只把以下内容写入 PostgreSQL：

- experiment metadata
- summary metadata
- artifact references
- comparison metadata（若有必要）

### 5.2 文件落盘的内容
以下内容先落文件：

- replay decisions 明细
- diagnostics 原始结果
- comparison tables
- Markdown 报告
- JSON summary
- CSV summary

### 5.3 推荐文件目录
建议：

```text
artifacts/
  research/
    experiments/
      <experiment_id>/
        replay_decisions.parquet
        diagnostics.json
        summary.json
        summary.csv
        report.md
```

### 5.4 原则
- 元数据进库
- 大结果不进库
- 文件路径必须回写 experiment registry

---

## 6. 需要新增的数据表

Phase 2 不需要重构 Phase 1 主表，只需要补少量研究元数据表。

建议新增 schema：

- `research`

### 6.1 `research.experiments`
用于登记 experiment 主记录。

#### 建议字段
- `experiment_id UUID PRIMARY KEY`
- `family TEXT NOT NULL`
- `symbol TEXT NOT NULL`
- `timeframe TEXT NOT NULL`
- `dataset_version TEXT NOT NULL`
- `parameter_overrides JSONB NOT NULL`
- `window_start_ts TIMESTAMPTZ NULL`
- `window_end_ts TIMESTAMPTZ NULL`
- `status TEXT NOT NULL`
- `result_path TEXT NULL`
- `summary_path TEXT NULL`
- `report_path TEXT NULL`
- `notes TEXT NULL`
- `created_at TIMESTAMPTZ NOT NULL`
- `updated_at TIMESTAMPTZ NOT NULL`

### 6.2 `research.experiment_summaries`
用于存放关键 summary metadata，便于快速比较。

#### 建议字段
- `experiment_summary_id UUID PRIMARY KEY`
- `experiment_id UUID NOT NULL`
- `opening_count INT NOT NULL DEFAULT 0`
- `blocked_count INT NOT NULL DEFAULT 0`
- `selectable_ratio DOUBLE PRECISION NULL`
- `execution_compatible_ratio DOUBLE PRECISION NULL`
- `mean_expected_edge_bps DOUBLE PRECISION NULL`
- `median_expected_edge_bps DOUBLE PRECISION NULL`
- `top_blocking_reasons JSONB NULL`
- `created_at TIMESTAMPTZ NOT NULL`
- `updated_at TIMESTAMPTZ NOT NULL`

### 6.3 `research.parameter_scan_runs`
用于记录一次参数扫描任务。

#### 建议字段
- `scan_run_id UUID PRIMARY KEY`
- `family TEXT NOT NULL`
- `symbol TEXT NOT NULL`
- `timeframe TEXT NOT NULL`
- `dataset_version TEXT NOT NULL`
- `parameter_grid JSONB NOT NULL`
- `status TEXT NOT NULL`
- `notes TEXT NULL`
- `created_at TIMESTAMPTZ NOT NULL`
- `updated_at TIMESTAMPTZ NOT NULL`

### 6.4 外键建议
- `research.experiment_summaries.experiment_id -> research.experiments.experiment_id`

### 6.5 说明
Phase 2 第一阶段**不**把 replay 明细表化。  
逐 bar decisions 仍然落文件。

---

## 7. Replay Core 任务书

### 7.1 目标
实现一个逐 bar replay runner，能读取 Gold replay bars，调用策略 adapter，并输出 replay decisions artifact。

### 7.2 输入
- family
- symbol
- timeframe
- dataset_version
- window_start_ts
- window_end_ts
- parameter_overrides

### 7.3 输出
逐 bar replay decisions，建议落为 parquet 或 csv。

### 7.4 必须输出的字段
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

### 7.5 首批范围
- `BTC-USDT-SWAP`
- `15m`
- `1H`
- `independent` 先跑通

### 7.6 验收标准
- 能 replay 一段 `BTC-USDT-SWAP 15m`
- 能输出 decisions artifact
- output 字段完整
- 时间顺序正确

---

## 8. Strategy Adapter Layer 任务书

### 8.1 目标
为 replay core 提供统一的策略 family 接口。

### 8.2 必须有的接口
建议统一接口类似：

```python
evaluate_bar(context) -> ReplayDecision
```

### 8.3 第一批 adapter
- `independent_adapter.py`
- `directional_adapter.py`

### 8.4 实现优先级
#### 第一优先
- `independent_adapter`

#### 第二优先
- `directional_adapter`

### 8.5 验收标准
- independent 可被 replay runner 调用
- directional 至少完成最小接口接入
- replay runner 不包含 family-specific if/else 风暴

---

## 9. Experiment Registry 任务书

### 9.1 目标
记录每次 experiment 的元数据和产物路径。

### 9.2 需要支持的能力
- create experiment
- mark running
- mark succeeded / failed
- attach artifact paths
- attach summary metadata

### 9.3 必须记录的信息
- family
- symbol
- timeframe
- dataset_version
- parameter_overrides
- time window
- result_path
- summary_path
- report_path

### 9.4 验收标准
- 每次 replay 都有 `experiment_id`
- 每次 replay 结束后 registry 有状态更新
- artifact path 可追踪

---

## 10. Parameter Scan Engine 任务书

### 10.1 目标
给定参数网格，自动运行多组 experiment，并生成 comparison artifacts。

### 10.2 第一批固定参数
- `min_confirm_ticks`
- `score_stability_threshold`
- `min_safe_net_edge_bps`

### 10.3 第一批固定取值
#### `min_confirm_ticks`
- `2`
- `3`
- `4`

#### `score_stability_threshold`
- `2.0`
- `5.0`
- `10.0`

#### `min_safe_net_edge_bps`
- `5`
- `10`
- `15`

### 10.4 第一阶段原则
- 小规模参数扫描
- 可解释优先
- 不追求并行最优
- 不做复杂搜索器

### 10.5 输出
- 多个 experiment
- comparison summary
- 每组 experiment 的 artifact

### 10.6 验收标准
- 至少能扫 2 个参数
- 至少每个参数 2~3 个取值
- 能生成对比 summary

---

## 11. Diagnostics Engine 任务书

### 11.1 目标
把 replay decisions 转成结构化诊断结果。

### 11.2 第一批必须支持的指标
- `opening_count`
- `blocked_count`
- `selectable_ratio`
- `execution_compatible_ratio`
- `blocking_reasons_top_n`
- `score_distribution`
- `expected_edge_distribution`

### 11.3 输出形式
建议输出：
- `diagnostics.json`
- 供 report builder 使用的 summary dict

### 11.4 诊断重点
Phase 2 的核心不是只看数量，而是看：

- 参数放宽后，开仓多了多少
- 是哪类 blocking reasons 降低了
- edge 分布如何变化
- 是否只是引入更多低质量机会

### 11.5 验收标准
- 对一份 replay decisions artifact 能生成 diagnostics
- 至少能输出 top blocking reasons
- 至少能输出 opening / blocked / edge summary

---

## 12. Report Builder 任务书

### 12.1 目标
将一组 experiment 的结果整理成可交付报告。

### 12.2 第一阶段输出
- `report.md`
- `summary.json`
- `summary.csv`

### 12.3 Markdown 报告至少应包含
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

### 12.4 验收标准
- 单个 experiment 能产出 Markdown 报告
- parameter scan 能产出 comparison summary
- artifact path 回写 registry

---

## 13. 需要新增的脚本

建议新增如下脚本，先服务于本地/CLI 运行。

### 13.1 `scripts/rdp_run_replay.py`
#### 作用
运行一次 replay experiment。

#### 输入
- family
- symbol
- timeframe
- dataset_version
- window_start / window_end
- parameter overrides

#### 输出
- replay decisions artifact
- experiment registry entry

---

### 13.2 `scripts/rdp_run_parameter_scan.py`
#### 作用
运行一次参数扫描。

#### 输入
- family
- symbol
- timeframe
- dataset_version
- parameter grid
- window_start / window_end

#### 输出
- 多个 experiment
- scan run registry
- comparison summary

---

### 13.3 `scripts/rdp_build_experiment_report.py`
#### 作用
从 experiment artifact 生成 report。

#### 输入
- experiment_id 或 artifact path

#### 输出
- Markdown report
- JSON summary
- CSV summary

---

## 14. Milestone 划分

建议把 Phase 2 第一阶段拆成 5 个 milestone。

### Milestone 1
**Replay Core + Independent Adapter**

### Milestone 2
**Experiment Registry**

### Milestone 3
**Diagnostics Engine**

### Milestone 4
**Parameter Scan Engine**

### Milestone 5
**Report Builder + Directional Adapter 最小接入**

---

## 15. 每个 Milestone 的完成标准

## Milestone 1：Replay Core + Independent Adapter
### 目标
- `BTC-USDT-SWAP`
- `15m`
- `independent`

能跑一条完整 replay。

### 完成标准
- replay decisions artifact 成功生成
- 字段完整
- 顺序正确
- independent adapter 可独立调用

---

## Milestone 2：Experiment Registry
### 目标
每次 replay 能登记 experiment metadata。

### 完成标准
- `research.experiments` 可写入
- 状态能更新
- artifact path 可追踪

---

## Milestone 3：Diagnostics Engine
### 目标
把 replay results 变成结构化诊断。

### 完成标准
- opening / blocked / edge / blocking reasons 可输出
- diagnostics artifact 可生成

---

## Milestone 4：Parameter Scan Engine
### 目标
能批量跑一组参数组合并输出 comparison summary。

### 完成标准
- 至少跑通 2 个参数
- 至少生成一份 comparison summary

---

## Milestone 5：Report Builder + Directional Adapter
### 目标
- 自动生成 Markdown 报告
- directional 至少完成最小 replay 接入

### 完成标准
- report.md 可生成
- directional 能跑最小 replay，或至少完成可用 adapter

---

## 16. Claude Code 实现约束

以下约束必须写进你给 Claude Code 的提示词中：

### 16.1 不允许改 Phase 1 主架构
不得改动：
- PostgreSQL 主仓
- Phase 1 canonical schema
- Gold replay bars 作为 replay 输入的地位

### 16.2 不允许跳过 Strategy Adapter Layer
即便首批偏 independent，也不能写死 replay runner。

### 16.3 不允许把逐 bar replay 明细直接表化
必须遵守：
- registry 进库
- 结果落文件

### 16.4 不允许在 Phase 2 混入 Phase 3 / 4 内容
不要接：
- live attribution
- trades
- orderbook
- slippage realism
- fill simulation

### 16.5 不允许一开始就泛化到全 symbol / 全 timeframe
必须先只做：
- `BTC-USDT-SWAP`
- `15m`
- `1H`

---

## 17. 非目标与边界

以下内容明确不属于 Phase 2 第一阶段：

- live-vs-replay 差异分析
- execution realism
- trades / orderbook ingestion
- dashboard / Web UI
- 大规模分布式实验框架
- 全市场扩展

---

## 18. 最终交付状态

当 Phase 2 第一阶段完成时，至少应达到：

1. `independent` 可对 `BTC-USDT-SWAP 15m / 1H` 做 replay
2. `directional` 完成最小适配接入
3. experiment registry 可追踪实验元数据
4. 至少能扫描 2 个关键参数
5. diagnostics 能输出 blocking reasons / edge summary
6. 自动 Markdown 报告可生成
7. parameter research 闭环可跑通

---

## 结论

到这一层为止，Phase 2 已经从“高层方向”收口成了“可直接开工的任务书”。

下一步工程动作应是：

1. 建 `research` schema 和 Phase 2 的最小 metadata 表
2. 先打通 Replay Core + Independent Adapter
3. 再接 Experiment Registry
4. 再接 Diagnostics
5. 再做 Parameter Scan
6. 最后接 Report Builder 与 Directional Adapter
