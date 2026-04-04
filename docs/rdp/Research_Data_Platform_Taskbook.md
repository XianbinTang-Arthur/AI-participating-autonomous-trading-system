# Research Data Platform 详细任务书

> 面向 AATS（AIParticipatingAutonomousTradingSystem）的研究与回放数据平台建设方案。  
> 目标是把“历史行情下载 + 分析脚本 + 回放实验”升级为一套**可分层存储、可标准化、可回放、可复现、可对接 live 归因**的数据体系。

---

## 目录

- [1. 项目目标](#1-项目目标)
- [2. 为什么必须建设成体系](#2-为什么必须建设成体系)
- [3. 平台总体范围](#3-平台总体范围)
- [4. 五层体系架构](#4-五层体系架构)
- [5. 建设原则](#5-建设原则)
- [6. 分阶段路线图](#6-分阶段路线图)
- [7. 推荐目录结构](#7-推荐目录结构)
- [8. 数据分层设计](#8-数据分层设计)
- [9. 标准表设计](#9-标准表设计)
- [10. 模块级详细任务书](#10-模块级详细任务书)
- [11. 脚本任务清单](#11-脚本任务清单)
- [12. 元数据与质量控制](#12-元数据与质量控制)
- [13. 回放与研究层详细任务](#13-回放与研究层详细任务)
- [14. 与现有交易系统的衔接点](#14-与现有交易系统的衔接点)
- [15. Phase 1 更细任务拆分](#15-phase-1-更细任务拆分)
- [16. Issue 标题建议](#16-issue-标题建议)
- [17. 验收标准](#17-验收标准)
- [18. 实施顺序建议](#18-实施顺序建议)
- [19. 风险与常见误区](#19-风险与常见误区)
- [20. 最终一句话定义](#20-最终一句话定义)

---

## 1. 项目目标

### 1.1 项目名称
**Research Data Platform（研究与回放数据平台）**

### 1.2 核心目标
把当前零散的：

- OKX 历史数据下载
- 回放脚本
- 参数分析
- live 排障 SQL
- 研究型数据处理

统一为一套可以长期支撑以下能力的平台：

1. **历史行情标准化与沉淀**
2. **可复现的离线 replay**
3. **参数扫描与实验管理**
4. **funding / 成本 / edge 归因**
5. **live 与 replay 对照分析**
6. **风险门 / allocator / budget / execution 的结构化归因**

### 1.3 平台需要服务的场景

#### A. 离线研究
- K线回放
- funding 成本分析
- 参数扫描
- family 对比
- signal diagnostics

#### B. 运行复盘
- live 某时段为什么没下单
- replay 与 live 差异归因
- 风控门、allocator、budget 哪层改变了最终结果

#### C. 未来扩展
- trades / orderbook 接入
- execution realism 建模
- post-trade analysis
- data-driven dashboard / runbook

---

## 2. 为什么必须建设成体系

如果只做“想到哪里做哪里”的分析模块，后面很快会出现以下问题：

- 同一份 OKX 数据被不同脚本各自清洗，口径不一致
- 研究、回放、参数扫描、live 复盘依赖的不是同一套数据定义
- funding、trades、orderbook 一旦加入，目录和 schema 迅速失控
- 每次新增一种数据类型都要重写 loader
- 研究结论无法沉淀，实验不可复现
- 回放结论无法和 live 行为直接对比
- 你会永远陷在“脚本越来越多，体系越来越弱”的状态里

因此，这个项目必须被定义成：

> **一套研究与回放数据平台，而不是若干临时分析脚本。**

---

## 3. 平台总体范围

### 3.1 Phase 1 范围内必须做
- OKX K线 ingest
- OKX funding ingest
- Bronze / Silver / Gold 分层
- symbol / timestamp / timeframe 标准化
- dataset registry / manifest
- quality report
- replay-ready gold dataset
- replay runner
- experiment 输出

### 3.2 暂不做
- 全量订单簿数据仓库服务化
- tick 级 execution simulator
- Web UI 数据平台页面
- 多交易所统一接入
- 分布式调度
- 大规模 OLAP 查询层

### 3.3 第一阶段优先接入的数据
#### 先做
1. **K线数据**
2. **资金费率**

#### 后做
3. 历史交易
4. 订单簿 L2 / 深度数据

---

## 4. 五层体系架构

### 4.1 数据采集层
负责把外部数据拉进来。

数据来源包括：
- OKX 历史 K线
- OKX funding
- 后续的 trades / orderbook / mark price / instrument metadata

职责：
- 下载
- 原始落盘
- 批次记录
- checksum
- 原始元数据登记

**不做策略逻辑。**

---

### 4.2 标准化层
负责把来源各异的原始数据，转成平台内部统一格式。

职责：
- symbol 规范化
- timestamp 统一为 UTC
- timeframe 统一
- 字段命名统一
- 单位统一
- 去重
- 排序
- 数据完整性与合法性检查

产物：
- Silver 层 canonical dataset

---

### 4.3 数据仓库层
这是平台的核心。

职责：
- Bronze / Silver / Gold 分层存储
- 分区管理
- dataset version 管理
- manifest / registry
- quality reports
- 统一读写入口

---

### 4.4 研究与回放层
职责：
- replay runner
- 参数扫描
- 特征工程
- edge / funding 分析
- blocking reason diagnostics
- live vs replay diff

---

### 4.5 反馈与运营层
职责：
- 把研究结论反馈给 live 配置与 runbook
- 形成可追踪的参数建议
- 形成可重复的实验与报告
- 逐步支撑 dashboard / 可视化

---

## 5. 建设原则

### 5.1 历史数据与研究逻辑解耦
- ingest 不直接做策略分析
- normalizer 不直接做 replay
- replay 不直接读取 raw bronze 文件

### 5.2 标准化数据是唯一事实来源
- 上层研究、回放、参数扫描统一依赖 Silver/Gold
- 禁止不同脚本各自读原始文件各自清洗

### 5.3 时间语义必须先明确
必须提前定义：
- bar timestamp 是开盘时刻还是收盘时刻
- funding timestamp 的语义
- replay 是 close-bar decision 还是 next-bar action

### 5.4 Symbol 必须 canonical
系统内部统一 symbol 命名，例如：
- `BTC-USDT-SWAP`

不要让研究层同时出现：
- `BTCUSDT`
- `BTC-USDT`
- `BTC-USDT-SWAP`

### 5.5 每个数据集都必须有 manifest
不能只有 parquet 文件没有来源、版本、构建信息。

### 5.6 实验必须可复现
实验结果必须记录：
- 使用的数据集版本
- 时间窗口
- 参数覆盖
- family
- 输出路径
- 生成时间

---

## 6. 分阶段路线图

### Phase 1：数据底座建设

#### 目标
先把数据采集、标准化、分层仓库、基础 manifest 做起来。

#### 交付物
- Bronze / Silver / Gold 目录规范
- candles ingest
- funding ingest
- symbol canonicalizer
- time semantics 规范
- dataset registry / manifest
- quality report
- replay-ready gold dataset

#### 成功标准
给定：
- `BTC-USDT-SWAP`
- `15m`
- 某个日期范围

能够：
1. 从 Bronze 原始文件生成 Silver 标准表
2. 再生成 Gold replay-ready 数据
3. 有 manifest 可追踪来源、版本、质量状态

---

### Phase 2：研究与回放能力建设

#### 目标
让平台具备“研究可复现”的最小闭环。

#### 交付物
- replay runner
- funding-aligned replay
- parameter scan
- experiment registry
- diagnostics report

#### 成功标准
给定一个时间窗口，能输出：
- 每根 bar 的策略状态
- score
- blocking reasons
- edge / funding-adjusted edge
- 参数变化下的 opening 数量变化

---

### Phase 3：与 live 运行数据打通

#### 目标
把历史研究和 live 归因接起来。

#### 交付物
- risk_decisions sink
- strategy_sleeve_intents sink
- allocator_budget_snapshots sink
- execution_orders/fills sink
- replay vs live comparison job

#### 成功标准
你可以回答：
- 某次 replay 会开仓，为什么 live 没开仓
- 风险门、allocator、budget 哪层导致分歧
- funding/成本是否解释了 live 与 replay 的偏差

---

## 7. 推荐目录结构

```text
aats/
  data_platform/
    ingestion/
      __init__.py
      okx/
        __init__.py
        candles_ingestor.py
        funding_ingestor.py
        trades_ingestor.py
        orderbook_ingestor.py
        file_discovery.py
        raw_file_parsers.py

    normalization/
      __init__.py
      symbol_mapper.py
      time_utils.py
      candles_normalizer.py
      funding_normalizer.py
      common_validators.py

    warehouse/
      __init__.py
      layout.py
      registry.py
      manifest_models.py
      quality_checks.py
      partitioning.py
      dataset_writer.py
      dataset_reader.py

    gold/
      __init__.py
      funding_alignment.py
      replay_dataset_builder.py
      feature_ready_dataset_builder.py

    research/
      __init__.py
      loaders/
        __init__.py
        candle_loader.py
        funding_loader.py
        replay_dataset_loader.py
      replay/
        __init__.py
        replay_runner.py
        replay_context_builder.py
        replay_result_writer.py
      analytics/
        __init__.py
        signal_diagnostics.py
        edge_analysis.py
        parameter_scan.py
        live_vs_replay_diff.py
      experiments/
        __init__.py
        models.py
        registry.py
        report_builder.py

    schemas/
      __init__.py
      bronze.py
      silver.py
      gold.py
      quality.py
      experiment.py

scripts/
  ingest_okx_candles.py
  ingest_okx_funding.py
  build_replay_dataset.py
  replay_strategy.py
  scan_strategy_params.py
  compare_live_vs_replay.py
  validate_dataset.py

data/
  bronze/
    okx/
      candles/
      funding/
      trades/
      orderbook/
  silver/
    market/
      candles/
      funding/
  gold/
    replay/
    features/
    analytics/
  manifests/
  quality_reports/
  experiments/
```

---

## 8. 数据分层设计

### 8.1 Bronze 层
原始层，尽量保留外部文件原貌，仅补最少元信息。

**用途**
- 保留原始来源
- 便于追溯与重建
- 避免清洗逻辑变更后无法回溯

**最小元数据**
- `source = "okx"`
- `dataset_type = "candles" | "funding" | ...`
- `downloaded_at`
- `source_path`
- `checksum`
- `raw_row_count`
- `source_date_range`

---

### 8.2 Silver 层
标准化层，是平台对上层的 canonical truth。

**用途**
- 所有研究、回放、分析统一依赖
- 已完成规范化、去重、排序、时间统一

**要求**
- 上层逻辑禁止直接依赖 Bronze 原始文件

---

### 8.3 Gold 层
研究特化层，是为 replay / diagnostics / analytics 准备的加工表。

**典型数据集**
- K线 + funding 对齐
- replay-ready bar dataset
- feature-ready dataset
- diagnostics-ready dataset

---

## 9. 标准表设计

### 9.1 Silver 表：Candles
建议字段：

- `ts: datetime`
- `venue: str`
- `symbol: str`
- `timeframe: str`
- `open: float`
- `high: float`
- `low: float`
- `close: float`
- `volume: float`
- `quote_volume: float | None`
- `is_closed: bool`
- `source_file: str`
- `dataset_version: str`
- `ingested_at: datetime`
- `quality_flags: list[str]`

**约束**
- `ts` 为 UTC
- `symbol` 为 canonical symbol
- `(venue, symbol, timeframe, ts)` 唯一

---

### 9.2 Silver 表：Funding
建议字段：

- `ts: datetime`
- `venue: str`
- `symbol: str`
- `funding_rate: float`
- `source_file: str`
- `dataset_version: str`
- `ingested_at: datetime`
- `quality_flags: list[str]`

**约束**
- `ts` 为 UTC
- `(venue, symbol, ts)` 唯一

---

### 9.3 Gold 表：ReplayReadyBar
建议字段：

- `ts: datetime`
- `venue: str`
- `symbol: str`
- `timeframe: str`
- `open: float`
- `high: float`
- `low: float`
- `close: float`
- `volume: float`
- `quote_volume: float | None`
- `aligned_funding_rate: float | None`
- `funding_source_ts: datetime | None`
- `is_closed: bool`
- `quality_flags: list[str]`
- `source_dataset_version: str`
- `gold_dataset_version: str`
- `built_at: datetime`

---

### 9.4 Experiment Output 表
建议字段：

- `experiment_id: str`
- `ts: datetime`
- `symbol: str`
- `timeframe: str`
- `family: str`
- `state: str`
- `selectable: bool`
- `execution_compatible: bool`
- `long_score: float | None`
- `short_score: float | None`
- `expected_net_edge_bps: float | None`
- `funding_adjusted_edge_bps: float | None`
- `score_support_count: int | None`
- `blocking_reasons: list[str]`
- `target_position_qty: float | None`
- `delta_position_qty: float | None`
- `parameter_snapshot: dict`
- `dataset_version: str`

---

## 10. 模块级详细任务书

### 模块 A：Ingestion
目录：`aats/data_platform/ingestion/okx/`

#### A1. `file_discovery.py`
**职责**
- 扫描 `data/bronze/okx/` 下原始文件
- 判断文件类型
- 识别 symbol / timeframe / 日期范围

**任务**
- [ ] 实现 Bronze 文件发现器
- [ ] 支持 candles / funding 文件识别
- [ ] 生成 `RawFileDescriptor`

---

#### A2. `raw_file_parsers.py`
**职责**
- 解析 OKX 原始格式
- 不做策略/业务逻辑，只做字段读取

**任务**
- [ ] 实现 candles 原始解析器
- [ ] 实现 funding 原始解析器
- [ ] 对异常格式给出明确错误

---

#### A3. `candles_ingestor.py`
**职责**
- 把原始 candles 文件 ingest 到 Bronze metadata，并触发 Silver normalizer

**任务**
- [ ] 读取 raw candle file
- [ ] 计算 checksum
- [ ] 记录 Bronze manifest
- [ ] 交给 normalizer 产出 Silver

---

#### A4. `funding_ingestor.py`
**职责**
- ingest funding 原始文件，并触发 Silver normalizer

**任务**
- [ ] funding 原始解析
- [ ] Bronze manifest 记录
- [ ] 交给 Silver normalizer

---

### 模块 B：Normalization
目录：`aats/data_platform/normalization/`

#### B1. `symbol_mapper.py`
**职责**
统一 symbol。

**任务**
- [ ] 设计 canonical symbol 规则
- [ ] 提供 `normalize_symbol(source, raw_symbol) -> canonical_symbol`
- [ ] 支持 OKX 合约命名映射

---

#### B2. `time_utils.py`
**职责**
统一时间语义。

**任务**
- [ ] 原始时间戳转 UTC
- [ ] 定义 candle `ts` 表示开盘还是收盘
- [ ] 定义 funding timestamp 语义
- [ ] 提供对齐 helper

---

#### B3. `candles_normalizer.py`
**职责**
生成 Silver Candle。

**任务**
- [ ] 字段映射
- [ ] UTC 统一
- [ ] symbol 标准化
- [ ] 去重
- [ ] 排序
- [ ] 唯一键校验
- [ ] quality flags 生成

---

#### B4. `funding_normalizer.py`
**职责**
生成 Silver Funding。

**任务**
- [ ] 字段映射
- [ ] UTC 统一
- [ ] symbol 标准化
- [ ] 去重
- [ ] 排序
- [ ] quality flags

---

#### B5. `common_validators.py`
**职责**
通用质量检查。

**任务**
- [ ] 缺失时间检查
- [ ] 重复键检查
- [ ] 时间乱序检查
- [ ] 数值合法性检查（OHLC）
- [ ] funding_rate 合法范围检查

---

### 模块 C：Warehouse
目录：`aats/data_platform/warehouse/`

#### C1. `layout.py`
**职责**
定义仓库目录规范。

**任务**
- [ ] 定义 Bronze/Silver/Gold 路径规则
- [ ] 定义 symbol/timeframe/date 分区规则
- [ ] 提供 path builder

---

#### C2. `manifest_models.py`
**职责**
定义 manifest schema。

**任务**
- [ ] 定义 Bronze ingest manifest
- [ ] 定义 Silver normalize manifest
- [ ] 定义 Gold build manifest
- [ ] 定义 quality report manifest

**至少包含**
- dataset_id
- dataset_type
- source
- symbol
- timeframe
- start_ts
- end_ts
- row_count
- checksum
- schema_version
- dataset_version
- built_from
- built_at
- status

---

#### C3. `registry.py`
**职责**
作为 dataset registry / manifest store。

**任务**
- [ ] 提供 manifest 写入与读取
- [ ] 支持按 symbol/timeframe/date 查询
- [ ] 支持找最新 dataset version

---

#### C4. `quality_checks.py`
**职责**
把质量检查结果结构化输出。

**任务**
- [ ] 输出统一 quality report
- [ ] 统计缺 bar、重复、乱序、异常值
- [ ] 给 dataset 打质量等级

---

#### C5. `dataset_writer.py` / `dataset_reader.py`
**职责**
统一 parquet 读写。

**任务**
- [ ] 统一写 parquet
- [ ] 保留 schema/version metadata
- [ ] 统一按分区读取

---

### 模块 D：Gold Builder
目录：`aats/data_platform/gold/`

#### D1. `funding_alignment.py`
**职责**
把 funding 对齐到 candles。

**任务**
- [ ] 定义 funding 对齐规则
- [ ] 实现最近已知 funding 向 bar 对齐
- [ ] 保留 funding 原始时间戳引用

---

#### D2. `replay_dataset_builder.py`
**职责**
从 Silver candles + Silver funding 生成 GoldReplayReadyBar。

**任务**
- [ ] 加载 candles
- [ ] 可选加载 funding
- [ ] 对齐 funding
- [ ] 生成 gold replay dataset
- [ ] 写 manifest

---

#### D3. `feature_ready_dataset_builder.py`
**职责**
后续扩展为 feature-ready dataset builder。  
Phase 1 可先放骨架。

---

### 模块 E：Research Loaders
目录：`aats/data_platform/research/loaders/`

#### E1. `candle_loader.py`
**职责**
读取 Silver/Gold candles。

**任务**
- [ ] load by symbol/timeframe/date range
- [ ] 支持 manifest 解析
- [ ] 支持 quality filter

---

#### E2. `funding_loader.py`
**职责**
读取 Silver funding。

---

#### E3. `replay_dataset_loader.py`
**职责**
读取 Gold replay-ready dataset。

---

### 模块 F：Replay
目录：`aats/data_platform/research/replay/`

#### F1. `replay_context_builder.py`
**职责**
把历史数据组装成可喂给现有策略逻辑的上下文。

**任务**
- [ ] 构造和 live 接近的 market snapshot
- [ ] 构造 bar context
- [ ] 构造 funding-aware context

---

#### F2. `replay_runner.py`
**职责**
逐 bar 跑策略。

**任务**
- [ ] 支持 family=independent / directional
- [ ] 支持参数覆盖
- [ ] 输出逐 bar 决策结果
- [ ] 输出 summary stats

---

#### F3. `replay_result_writer.py`
**职责**
把 replay 结果写到：
- `data/experiments/`
- 或 Gold analytics dataset

---

### 模块 G：Analytics
目录：`aats/data_platform/research/analytics/`

#### G1. `signal_diagnostics.py`
**职责**
分析：
- opening 数量
- blocking reasons 分布
- selectable / execution_compatible 比例
- family 差异

**任务**
- [ ] independent diagnostics
- [ ] directional diagnostics
- [ ] 按日/按小时统计

---

#### G2. `edge_analysis.py`
**职责**
分析：
- expected_net_edge_bps
- funding-adjusted edge
- signal quality 分布

---

#### G3. `parameter_scan.py`
**职责**
批量扫描关键参数。

**第一批建议支持**
- `min_confirm_ticks`
- `score_stability threshold`
- `min_safe_net_edge_bps`

---

#### G4. `live_vs_replay_diff.py`
**职责**
Phase 3 用于：
- live 决策和 replay 决策对比
- 定位差异来自哪一层

---

### 模块 H：Experiments
目录：`aats/data_platform/research/experiments/`

#### H1. `models.py`
定义 experiment schema：
- experiment_id
- family
- symbol
- timeframe
- start_ts
- end_ts
- dataset_version
- parameter_overrides
- created_at

---

#### H2. `registry.py`
**职责**
记录实验输入、输出、版本、摘要。

**任务**
- [ ] experiment manifest
- [ ] result path registry
- [ ] dataset linkage

---

#### H3. `report_builder.py`
**职责**
生成 Markdown / CSV / JSON 报告。

---

## 11. 脚本任务清单

### 11.1 `scripts/ingest_okx_candles.py`
**参数**
- `--input-dir`
- `--symbol`
- `--timeframe`
- `--output-root`
- `--dataset-version`

**输出**
- Bronze manifest
- Silver parquet
- quality report

---

### 11.2 `scripts/ingest_okx_funding.py`
**参数**
- `--input-dir`
- `--symbol`
- `--output-root`
- `--dataset-version`

**输出**
- Bronze manifest
- Silver funding parquet
- quality report

---

### 11.3 `scripts/build_replay_dataset.py`
**参数**
- `--symbol`
- `--timeframe`
- `--start`
- `--end`
- `--include-funding`
- `--dataset-version`

**输出**
- Gold replay dataset
- Gold manifest

---

### 11.4 `scripts/replay_strategy.py`
**参数**
- `--family independent|directional`
- `--symbol`
- `--timeframe`
- `--start`
- `--end`
- `--dataset-version`
- `--param key=value`
- `--output-path`

**输出**
- replay result parquet/csv
- experiment manifest
- summary report

---

### 11.5 `scripts/scan_strategy_params.py`
**参数**
- `--family`
- `--symbol`
- `--timeframe`
- `--start`
- `--end`
- `--param-grid path/to/grid.json`

**输出**
- 多组实验结果
- 汇总表
- top configs report

---

### 11.6 `scripts/compare_live_vs_replay.py`
Phase 3 再做。

---

### 11.7 `scripts/validate_dataset.py`
**参数**
- `--dataset-type`
- `--symbol`
- `--timeframe`
- `--start`
- `--end`

**输出**
- quality report
- pass/fail summary

---

## 12. 元数据与质量控制

### 12.1 每个数据集都必须有 manifest
不允许“只有 parquet 没有说明”。

**至少包含**
- dataset_id
- dataset_version
- schema_version
- source_dataset_ids
- symbol
- timeframe
- start_ts
- end_ts
- row_count
- build_time
- builder_version
- quality_summary

---

### 12.2 每个质量报告至少包含
- missing_intervals_count
- duplicate_rows_count
- out_of_order_rows_count
- invalid_price_rows_count
- invalid_volume_rows_count
- suspect_rows_count

---

### 12.3 dataset versioning 规则
- Bronze version：和 source/下载批次绑定
- Silver version：和 normalization 逻辑绑定
- Gold version：和 gold builder + alignment 逻辑绑定

---

## 13. 回放与研究层详细任务

### 第一批最值得做的实验

#### 实验 1：复盘最近“不下单”的时段
目标：
- 看 independent 当时的 opening 是否在 replay 中也出现
- 看 blocking_reasons 是否一致

#### 实验 2：扫描 `min_confirm_ticks`
目标：
- 看 independent 被 `score_support_below_min_confirm_ticks` 挡掉的比例

#### 实验 3：扫描 `score_stability`
目标：
- 看阈值变化对 opening 数量的影响

#### 实验 4：funding-adjusted edge
目标：
- 看 funding 是否显著改变原本可执行机会

---

## 14. 与现有交易系统的衔接点

### 当前先复用
- feature 计算逻辑
- independent / directional family 逻辑
- decision context 构建中可复用部分

### 当前先不要耦合
- OKX live adapter
- order manager
- operator auth
- startup recovery 主链

### Phase 3 再接入 live 数据表
建议纳入：
- `event_store`
- `strategy_sleeve_intents`
- `portfolio_allocation_decisions`
- `allocator_budget_snapshots`
- `risk.decisions`
- `strategy_execution_bundles`
- `execution_orders`
- `execution_fills`

---

## 15. Phase 1 更细任务拆分

### 第 1 周：打基础
**任务**
- [ ] 建目录
- [ ] 定义 Silver/Gold schema
- [ ] 定义 canonical symbol 规范
- [ ] 定义 time semantics
- [ ] 定义 manifest schema
- [ ] 写 `scripts/ingest_okx_candles.py`
- [ ] 写 `candles_ingestor.py`
- [ ] 写 `candles_normalizer.py`
- [ ] 写 `quality_checks.py`
- [ ] 写 `dataset_writer.py`

**验收**
- [ ] 某个 BTC 15m 文件可以被 ingest
- [ ] 生成 Silver candles parquet
- [ ] 生成 manifest 与 quality report

---

### 第 2 周：funding 与 Gold
**任务**
- [ ] 写 `scripts/ingest_okx_funding.py`
- [ ] 写 `funding_ingestor.py`
- [ ] 写 `funding_normalizer.py`
- [ ] 写 `funding_alignment.py`
- [ ] 写 `scripts/build_replay_dataset.py`
- [ ] 写 `replay_dataset_builder.py`

**验收**
- [ ] Silver funding 可生成
- [ ] funding 可对齐到 bars
- [ ] Gold replay dataset 可生成

---

### 第 3 周：Replay runner
**任务**
- [ ] 写 `replay_context_builder.py`
- [ ] 写 `replay_runner.py`
- [ ] 写 `replay_result_writer.py`
- [ ] 跑 independent 一段时间窗
- [ ] 跑 directional 同一时间窗

**验收**
- [ ] 输出逐 bar 决策结果
- [ ] 输出 blocking reasons 与 score 字段
- [ ] 结果可持久化到 experiments

---

### 第 4 周：Diagnostics 与参数扫描
**任务**
- [ ] 写 `signal_diagnostics.py`
- [ ] 写 `parameter_scan.py`
- [ ] 写 `report_builder.py`
- [ ] 做第一份实验报告

**验收**
- [ ] 可以统计 opening / blocked / selectable 比例
- [ ] 可以对比不同参数的 opening 数量变化
- [ ] 可以导出 Markdown/CSV 报告

---

## 16. Issue 标题建议

1. **Create Research Data Platform directory structure and schema models**
2. **Implement canonical symbol mapping for OKX market data**
3. **Implement OKX candle bronze ingest and silver normalization**
4. **Implement OKX funding bronze ingest and silver normalization**
5. **Add dataset manifest registry and quality report framework**
6. **Build replay-ready gold dataset from candles and funding**
7. **Implement replay runner for independent and directional families**
8. **Add experiment registry and replay result writer**
9. **Add signal diagnostics and blocking reason analytics**
10. **Add parameter scan pipeline for independent family**
11. **Design live-vs-replay comparison pipeline for Phase 3**

---

## 17. 验收标准

### Phase 1
- [ ] 可以 ingest OKX K线
- [ ] 可以 ingest funding
- [ ] 有 Bronze/Silver/Gold 分层
- [ ] 有 manifest
- [ ] 有 quality report
- [ ] 可以构建 replay-ready dataset

### Phase 2
- [ ] 可以 replay independent
- [ ] 可以 replay directional
- [ ] 输出逐 bar 决策结果
- [ ] 支持 funding-adjusted edge
- [ ] 支持参数扫描
- [ ] 支持生成 experiment report

### Phase 3
- [ ] 可以对比 live vs replay
- [ ] 可以解释某时段为什么 replay 开仓而 live 没开
- [ ] 可以将 risk/allocator/budget 差异结构化归因

---

## 18. 实施顺序建议

### 最优顺序
1. 先 K线 ingest
2. 再 funding ingest
3. 再 Gold replay dataset
4. 再 replay runner
5. 再 parameter scan
6. 最后再接 live vs replay

### 不推荐的顺序
- 一开始就接 orderbook
- 一开始就做数据库服务化
- 一开始就做 Web UI
- 一开始就写很多零散 notebook

---

## 19. 风险与常见误区

### 误区 1：先写很多分析脚本
结果会变成：
- 每个脚本自己读 CSV
- 自己改列名
- 自己处理时间
- 自己对齐 funding

最后不可复用。

### 误区 2：过早服务化
当前最重要的是：
- schema
- dataset version
- manifest
- replay-ready 数据

不是先做一个很重的数据服务平台。

### 误区 3：历史数据与 live 数据完全分家
最终必须让：
- historical replay
和
- live 实际行为

进入同一分析框架。

### 误区 4：没有 dataset version
如果 normalize / alignment 逻辑变了但 version 不变，后面实验结论不可比较。

---

## 20. 最终一句话定义

**这个 Research Data Platform 的第一目标，不是“多存一些历史数据”，而是“把历史数据、研究、回放、live 归因放进同一套可复现、可追溯、可扩展的数据体系”。**
