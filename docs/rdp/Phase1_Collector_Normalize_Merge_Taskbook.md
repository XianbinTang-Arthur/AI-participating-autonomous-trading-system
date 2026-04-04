# Phase 1 Collector / Normalize / Merge 实现任务书

> 本文档承接以下产物：
>
> - `Phase1_Historical_Backfill_and_Incremental_Ingestion_Decision.md`
> - `Phase1_Database_Plan_and_Migration_Taskbook.md`
> - `Phase1_Migration_SQL_Skeleton.zip`
> - `Phase1A_Documents.zip`
>
> 目标是把 Phase 1 从“建库与 migration”继续推进到：
>
> 1. 历史文件采集与入库
> 2. API 增量采集与入库
> 3. 规范化与质量校验
> 4. staging -> bronze -> silver -> gold 的 merge/build 流程
>
> 使 Codex / Claude Code 可以按本文档直接开始实现 Phase 1 的核心数据流水线。

---

## 目录

- [1. 文档目标](#1-文档目标)
- [2. Phase 1 数据流水线总览](#2-phase-1-数据流水线总览)
- [3. 总体模块划分](#3-总体模块划分)
- [4. 历史文件采集（Backfill Collector）任务书](#4-历史文件采集backfill-collector任务书)
- [5. API 增量采集（Rolling Collector）任务书](#5-api-增量采集rolling-collector任务书)
- [6. Normalize 任务书](#6-normalize-任务书)
- [7. Validation / Quality Check 任务书](#7-validation--quality-check-任务书)
- [8. Merge 任务书](#8-merge-任务书)
- [9. Gold Replay Bar Builder 任务书](#9-gold-replay-bar-builder-任务书)
- [10. Job / Scheduler 任务书](#10-job--scheduler-任务书)
- [11. 推荐目录结构](#11-推荐目录结构)
- [12. 模块级接口建议](#12-模块级接口建议)
- [13. 配置项建议](#13-配置项建议)
- [14. 实施顺序](#14-实施顺序)
- [15. 验收标准](#15-验收标准)
- [16. 非目标与边界](#16-非目标与边界)

---

## 1. 文档目标

本文档只定义 Phase 1 核心流水线的工程实现任务：

- collector
- normalize
- validation
- merge
- gold builder
- job tracking

本文档不负责：
- 完整 replay runner 实现
- parameter scan 实现
- live-vs-replay diff
- trades / orderbook ingestion
- UI / dashboard

---

## 2. Phase 1 数据流水线总览

Phase 1 采用双通道模型：

### 通道 A：Historical Backfill
`historical_file -> staging -> bronze -> silver -> gold(optional)`

### 通道 B：Rolling Incremental
`okx_api -> staging -> bronze -> silver -> gold(optional)`

二者共用：
- PostgreSQL 主仓库
- canonical schema
- validation 规则
- meta run / checkpoint / quality 机制

---

## 3. 总体模块划分

建议 Phase 1 拆为 6 类核心模块：

1. **Backfill Collectors**
2. **Rolling Collectors**
3. **Normalizers**
4. **Validators / Quality Checkers**
5. **Mergers**
6. **Gold Builders**

外加：
7. **Job / Scheduler / Run Tracking**

---

## 4. 历史文件采集（Backfill Collector）任务书

### 4.1 目标
把 OKX 官方下载的历史文件纳入平台，自动完成：

- 原始文件发现
- 文件登记
- 文件解析
- staging 入库
- 触发 normalize / validation / merge

### 4.2 输入来源
- 历史下载目录
- 文件类型：
  - candles day/month zip
  - funding day/month zip

### 4.3 核心任务

#### Task B1: Raw File Discovery
扫描下载目录，识别新文件并登记到：
- `meta.raw_source_files`

需要提取：
- dataset_domain
- instrument_type
- symbol_hint
- timeframe_hint
- source_granularity
- source_path
- checksum
- file_size
- source_start/end (若可从文件名推断)

#### Task B2: Raw File Parse
对 ZIP / CSV 文件进行解析。

要求：
- 能处理 candles
- 能处理 funding
- 保留原始列名映射
- 允许不同 source type 走不同 parser

#### Task B3: Run Registration
历史文件处理前先写：
- `meta.ingest_runs`
- `meta.ingest_run_items`

要求：
- run_type = `backfill`
- item 级别绑定 `source_file_id`

#### Task B4: Staging Insert
将解析后的记录写入对应 staging 表。

要求：
- 以批次方式写入
- 绑定 `ingest_run_id`
- 写入 `raw_symbol` / `raw_ts`
- 写入 `dataset_version`

### 4.4 验收标准
- 新下载文件能被识别并登记
- 同一个文件不会被重复导入
- staging 表中可见批次入库结果
- 失败文件能在 meta 表中看到错误状态

---

## 5. API 增量采集（Rolling Collector）任务书

### 5.1 目标
持续从 OKX REST API 拉取最近 candles 和 funding，维护最新数据与近期缺口。

### 5.2 覆盖范围

#### Candles
- `BTC-USDT`
- `ETH-USDT`
- `BTC-USDT-SWAP`
- `ETH-USDT-SWAP`

timeframes:
- `1m`
- `5m`
- `15m`
- `1H`

#### Funding
- `BTC-USDT-SWAP`
- `ETH-USDT-SWAP`

### 5.3 核心任务

#### Task R1: Checkpoint Load
从 `meta.ingest_checkpoints` 读取当前 watermark。

#### Task R2: Fetch Window Planning
根据当前时间、timeframe、checkpoint，决定本轮要请求的窗口。

#### Task R3: API Fetch
调用：
- `GET /api/v5/market/history-candles`
- `GET /api/v5/public/funding-rate-history`

要求：
- 控制速率
- 记录请求参数
- 记录抓取时间
- 支持分页/多次请求
- 原始响应可选落盘或写 run item details

#### Task R4: Staging Insert
把 API 响应写入对应 staging 表。

#### Task R5: Checkpoint Update
在 merge 成功后更新：
- `last_successful_ts`
- `last_attempted_ts`
- `next_expected_ts`
- `gap_detected`

### 5.4 Gap Repair
如果发现预期时间点与实际最新数据之间存在缺口：

- 自动生成 `gap_repair` run
- 重新抓缺失窗口
- 重走 staging -> validate -> merge

### 5.5 验收标准
- 常驻任务能持续写入新 candles/funding
- checkpoint 正常推进
- 缺口能自动检测
- gap repair 至少能补一个简单缺口案例

---

## 6. Normalize 任务书

### 6.1 目标
把不同来源（文件 / API）的 raw input 规范化到统一 Bronze/Silver 语义。

### 6.2 Normalize 分层原则

#### Staging
- 临时结构化写入
- 接近 raw 输入
- 允许保留 raw trace

#### Bronze
- 结构化来源层
- 保留来源痕迹
- 保留原始数量语义

#### Silver
- canonical truth
- timestamp UTC 化
- symbol canonical 化
- 去重 / 排序 / 主键语义稳定

### 6.3 核心任务

#### Task N1: Symbol Canonicalization
实现 symbol 映射：
- `BTC-USDT`
- `ETH-USDT`
- `BTC-USDT-SWAP`
- `ETH-USDT-SWAP`

要求：
- 文件源与 API 源都统一走同一个 canonical symbol mapper

#### Task N2: Timestamp Normalization
- 原始毫秒时间戳 -> `TIMESTAMPTZ`
- candles 统一解释为 `bar open timestamp`
- funding 统一解释为 `funding event timestamp`

#### Task N3: Numeric Parsing
- 价格字段转 numeric
- volume 字段转 numeric
- funding_rate 转 numeric
- API optional fields 转 nullable numeric/text

#### Task N4: Field Mapping
按已冻结 mapping 文档实现：
- candles file mapping
- candles API mapping
- funding file mapping
- funding API mapping

### 6.4 验收标准
- 同一数据域的文件/API 输入都能进入相同 Bronze/Silver 语义
- 不会因为来源不同造成 schema 分叉
- `vol / vol_ccy / vol_quote` 保留清晰

---

## 7. Validation / Quality Check 任务书

### 7.1 目标
在 merge 前检查数据质量，并把结果写入 `meta.quality_reports`。

### 7.2 Candles 规则

#### 必查
- duplicate rows
- missing intervals
- out-of-order rows
- OHLC 合法性
- volume 非负
- `confirm` 合法值

### 7.3 Funding 规则

#### 必查
- duplicate rows
- out-of-order rows
- funding_rate 非空
- funding_rate 数值格式合法

### 7.4 输出要求
每次 validation 要输出：

- 统计指标
- quality_status (`pass / warn / fail`)
- details JSON
- 对应 run / symbol / timeframe / dataset_version

写入：
- `meta.quality_reports`

### 7.5 验收标准
- 至少可以对 candles/funding 各跑出一条 quality report
- 数据异常能进入 `warn/fail`
- 正常批次能进入 `pass`

---

## 8. Merge 任务书

### 8.1 目标
把 staging 中的数据通过校验后合并到 Bronze 和 Silver。

### 8.2 Merge 总原则
- 不允许外部 collector 直接写 Bronze/Silver
- 必须先经过 staging
- merge 是唯一 canonical 写入路径

### 8.3 历史文件 merge
推荐顺序：

1. staging -> bronze
2. bronze -> silver

### 8.4 API 增量 merge
推荐顺序：

1. staging -> bronze
2. bronze -> silver

### 8.5 Candles merge 要求
- 主键 `(symbol, ts)`
- 若目标已存在记录，执行 upsert / merge
- `updated_at` 变化
- `dataset_version` 更新规则明确
- 保持 `raw_symbol` / `raw_ts` 追踪

### 8.6 Funding merge 要求
同 candles，但注意：
- API 可能比文件多扩展字段
- merge 时要保留 richer source information

### 8.7 Merge 冲突策略
建议：
- 以更“完整”的记录覆盖更“贫瘠”的记录
- 以新的 ingest 批次覆盖旧批次
- 但必须保留 run trace

### 8.8 验收标准
- candles/funding 均能从 staging 成功 merge 到 Bronze/Silver
- 重复跑同一批数据不会破坏主键一致性
- `updated_at` 正常变化
- run item 可见写入行数统计

---

## 9. Gold Replay Bar Builder 任务书

### 9.1 目标
从 Silver 构建 replay-ready Gold bars。

### 9.2 设计原则
Gold 是：
- derived layer
- rebuildable
- not final source of truth

### 9.3 核心任务

#### Task G1: Candle Load
从 Silver candles 按 symbol/timeframe/window 读取。

#### Task G2: Funding Alignment
对 swap replay bars，把 funding 事件对齐到 bar：

- `aligned_funding_rate`
- `funding_source_ts`

#### Task G3: Gold Upsert
写入：
- `gold.market_spot_replay_bars_*`
- `gold.market_swap_replay_bars_*`

#### Task G4: Build Run Tracking
建议将 Gold build 也登记为：
- `run_type = gold_build`

### 9.4 Spot vs Swap
#### Spot
- `aligned_funding_rate = NULL`
- `funding_source_ts = NULL`

#### Swap
- funding 对齐有效

### 9.5 验收标准
- 至少能对一个 symbol/timeframe 生成 Gold replay bars
- swap Gold bars 能带 funding 对齐字段
- spot Gold bars 不要求 funding 值

---

## 10. Job / Scheduler 任务书

### 10.1 目标
实现 Phase 1 的最小任务调度与状态跟踪。

### 10.2 调度模型
平台为独立服务进程，内嵌 scheduler。

### 10.3 最小任务类型
- `backfill`
- `rolling`
- `gap_repair`
- `gold_build`

### 10.4 Candles cadence
- 1m: 每 1 分钟
- 5m: 每 5 分钟
- 15m: 每 15 分钟
- 1H: 每 1 小时

### 10.5 Funding cadence
- 每 15 分钟检查一次

### 10.6 状态机
至少支持：
- `pending`
- `running`
- `succeeded`
- `failed`
- `retrying`
- `backfilling`

### 10.7 Run 记录要求
每次任务必须写：
- `meta.ingest_runs`
- 必要时写 `meta.ingest_run_items`

### 10.8 验收标准
- scheduler 能触发至少一轮 rolling candles
- scheduler 能触发至少一轮 funding fetch
- run 状态变化正确可查

---

## 11. 推荐目录结构

```text
aats/
  data_platform/
    collectors/
      backfill/
        file_discovery.py
        file_parser.py
        candles_backfill_collector.py
        funding_backfill_collector.py
      rolling/
        candles_api_collector.py
        funding_api_collector.py

    normalize/
      symbol_mapper.py
      time_normalizer.py
      candle_normalizer.py
      funding_normalizer.py

    validate/
      candle_quality_checker.py
      funding_quality_checker.py
      report_writer.py

    merge/
      bronze_merger.py
      silver_merger.py
      merge_utils.py

    gold/
      replay_bar_builder.py
      funding_aligner.py

    jobs/
      scheduler.py
      run_registry.py
      checkpoint_manager.py
      gap_repair.py

scripts/
  run_backfill_once.py
  run_rolling_once.py
  build_gold_replay_bars.py
```

---

## 12. 模块级接口建议

## 12.1 Backfill Collector
建议接口：

```python
collect_backfill_source_file(source_file_id: str) -> str  # returns ingest_run_id
```

## 12.2 Rolling Collector
建议接口：

```python
collect_candles_incremental(symbol: str, timeframe: str) -> str
collect_funding_incremental(symbol: str) -> str
```

## 12.3 Normalizer
建议接口：

```python
normalize_candle_rows(rows: list[dict], source_type: str) -> list[dict]
normalize_funding_rows(rows: list[dict], source_type: str) -> list[dict]
```

## 12.4 Validator
建议接口：

```python
validate_staging_candles(table_name: str, ingest_run_id: str) -> dict
validate_staging_funding(table_name: str, ingest_run_id: str) -> dict
```

## 12.5 Merger
建议接口：

```python
merge_staging_to_bronze(table_name: str, ingest_run_id: str) -> int
merge_bronze_to_silver(table_name: str, ingest_run_id: str) -> int
```

## 12.6 Gold Builder
建议接口：

```python
build_gold_replay_bars(symbol: str, timeframe: str, instrument_type: str, window_start, window_end) -> str
```

---

## 13. 配置项建议

建议配置文件至少包含：

### 数据库
- `research_db_dsn`

### 文件目录
- `historical_download_dir`

### Candles rolling
- `rolling_candles_enabled`
- `rolling_candles_symbols`
- `rolling_candles_timeframes`
- `rolling_candles_sleep_seconds`

### Funding rolling
- `rolling_funding_enabled`
- `rolling_funding_symbols`
- `rolling_funding_sleep_seconds`

### Gap repair
- `auto_gap_repair_enabled`
- `max_gap_repair_window`

### Gold build
- `gold_replay_build_enabled`

---

## 14. 实施顺序

最推荐的顺序：

### Step 1
先实现：
- meta run tracking
- file discovery
- raw source file registration

### Step 2
实现 candles backfill collector
- 先打通 ZIP -> staging -> bronze -> silver

### Step 3
实现 funding backfill collector
- 同样先打通 ZIP -> staging -> bronze -> silver

### Step 4
实现 rolling candles collector
- 先 15m
- 再推广到 1m / 5m / 1H

### Step 5
实现 rolling funding collector

### Step 6
实现 quality reports

### Step 7
实现 gold replay bar builder

### Step 8
实现 gap repair

---

## 15. 验收标准

以下条件全部满足，才算 Phase 1 collector / normalize / merge 主流程完成：

### Backfill
- [ ] candles 历史文件可自动发现并入 staging
- [ ] candles 可 merge 到 bronze / silver
- [ ] funding 历史文件可自动发现并入 staging
- [ ] funding 可 merge 到 bronze / silver

### Rolling
- [ ] candles API 增量采集可持续运行
- [ ] funding API 增量采集可持续运行
- [ ] checkpoint 可推进

### Quality
- [ ] candles / funding 都能生成 quality report
- [ ] fail / warn / pass 可区分

### Gold
- [ ] 至少一个 symbol/timeframe 的 Gold replay bars 可构建
- [ ] swap Gold bars 带 funding 对齐

### Control Plane
- [ ] `ingest_runs`
- [ ] `ingest_run_items`
- [ ] `ingest_checkpoints`
- [ ] `quality_reports`
  都能反映真实流水线状态

---

## 16. 非目标与边界

以下内容不属于本任务书实现范围：

- 完整 replay strategy engine
- parameter scan
- signal diagnostics
- live-vs-replay attribution
- trades/orderbook ingestion
- UI / dashboard
- Airflow 接入

这些属于 Phase 2 及之后的内容。

---

## 结论

到这一层为止，Phase 1 已经具备了完整的“从样本调查 -> schema freeze -> 建库迁移 -> collector/normalize/merge/build 实施”的闭环。

下一步工程动作应是：

1. 按 migration 建库
2. 先打通 candles/funding backfill
3. 再打通 rolling ingestion
4. 再构建 Gold replay bars
5. 最后补 scheduler 与 gap repair
