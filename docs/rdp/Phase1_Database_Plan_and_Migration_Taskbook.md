# Phase 1 数据库建表方案与 Migration 任务书

> 本文档基于以下已完成文档继续收口：
>
> - `source_survey_okx_candles.md`
> - `source_survey_okx_funding.md`
> - `field_mapping_okx_candles.md`
> - `field_mapping_okx_funding.md`
> - `time_semantics_phase1.md`
> - `schema_validation_phase1.md`
> - `schema_freeze_phase1_v1.md`
>
> 目标是把 Phase 1 的设计从“调查与冻结”推进到“可以直接建库、建表、写 migration、落 collector 和 normalize pipeline”。

---

## 目录

- [1. 文档目标](#1-文档目标)
- [2. Phase 1 的数据库职责](#2-phase-1-的数据库职责)
- [3. 数据库总体结构](#3-数据库总体结构)
- [4. Schema 划分](#4-schema-划分)
- [5. Phase 1 最小表清单](#5-phase-1-最小表清单)
- [6. 表设计原则](#6-表设计原则)
- [7. Meta 表详细建表方案](#7-meta-表详细建表方案)
- [8. Staging 表详细建表方案](#8-staging-表详细建表方案)
- [9. Bronze 表详细建表方案](#9-bronze-表详细建表方案)
- [10. Silver 表详细建表方案](#10-silver-表详细建表方案)
- [11. Gold 表详细建表方案](#11-gold-表详细建表方案)
- [12. 索引与约束设计](#12-索引与约束设计)
- [13. Migration 分阶段策略](#13-migration-分阶段策略)
- [14. Migration 文件拆分建议](#14-migration-文件拆分建议)
- [15. 每个 Migration 的任务书](#15-每个-migration-的任务书)
- [16. 实施顺序](#16-实施顺序)
- [17. 验收标准](#17-验收标准)
- [18. 非目标与边界](#18-非目标与边界)

---

## 1. 文档目标

本文档只解决两件事：

1. **Phase 1 数据库该如何建表**
2. **这些表应如何通过 migration 逐步落地**

本文档不负责：
- collector 代码实现细节
- API 请求重试细节
- scheduler 代码实现细节
- replay runner 代码实现细节
- live-vs-replay 对比实现

这些能力会以这些表为基础，在后续任务中落地。

---

## 2. Phase 1 的数据库职责

Phase 1 使用 **独立 PostgreSQL database** 作为 Research Data Platform 的主仓库。

数据库必须承载以下职责：

### 2.1 元数据与控制面
- 数据集 manifest
- 原始文件登记
- ingest run / run item 追踪
- checkpoint / watermark
- quality reports

### 2.2 市场数据事实层
- candles
- funding

### 2.3 分层存储
- staging
- bronze
- silver
- gold

### 2.4 研究输入
- replay-ready gold bars

---

## 3. 数据库总体结构

### Database
建议 database 名称：

- `aats_research`

### Schemas
Phase 1 固定为：

- `meta`
- `staging`
- `bronze`
- `silver`
- `gold`

### 设计原则
- 每层职责清晰
- 每类数据物理分表
- 现货 / 合约分表
- timeframe 分表（candles）
- funding 单独表
- staging 不直接视为 canonical
- silver 是 canonical truth
- gold 是可重建研究层

---

## 4. Schema 划分

## 4.1 `meta`
存：
- manifests
- source files
- runs
- run items
- checkpoints
- quality reports

## 4.2 `staging`
存：
- 原始采集批次转换后的临时结构化数据
- 尚未完成 canonical merge

## 4.3 `bronze`
存：
- 保留来源语义的结构化层
- 保留 raw trace 字段

## 4.4 `silver`
存：
- canonical truth
- replay / analytics 的基础输入

## 4.5 `gold`
存：
- replay-ready derived datasets
- 由 Silver 重建

---

## 5. Phase 1 最小表清单

## 5.1 Meta 表
- `meta.dataset_manifests`
- `meta.raw_source_files`
- `meta.ingest_runs`
- `meta.ingest_run_items`
- `meta.ingest_checkpoints`
- `meta.quality_reports`

## 5.2 Staging 表
- `staging.market_spot_candles_1m`
- `staging.market_spot_candles_5m`
- `staging.market_spot_candles_15m`
- `staging.market_spot_candles_1h`
- `staging.market_swap_candles_1m`
- `staging.market_swap_candles_5m`
- `staging.market_swap_candles_15m`
- `staging.market_swap_candles_1h`
- `staging.market_swap_funding`

## 5.3 Bronze 表
- `bronze.market_spot_candles_1m`
- `bronze.market_spot_candles_5m`
- `bronze.market_spot_candles_15m`
- `bronze.market_spot_candles_1h`
- `bronze.market_swap_candles_1m`
- `bronze.market_swap_candles_5m`
- `bronze.market_swap_candles_15m`
- `bronze.market_swap_candles_1h`
- `bronze.market_swap_funding`

## 5.4 Silver 表
- `silver.market_spot_candles_1m`
- `silver.market_spot_candles_5m`
- `silver.market_spot_candles_15m`
- `silver.market_spot_candles_1h`
- `silver.market_swap_candles_1m`
- `silver.market_swap_candles_5m`
- `silver.market_swap_candles_15m`
- `silver.market_swap_candles_1h`
- `silver.market_swap_funding`

## 5.5 Gold 表
- `gold.market_spot_replay_bars_1m`
- `gold.market_spot_replay_bars_5m`
- `gold.market_spot_replay_bars_15m`
- `gold.market_spot_replay_bars_1h`
- `gold.market_swap_replay_bars_1m`
- `gold.market_swap_replay_bars_5m`
- `gold.market_swap_replay_bars_15m`
- `gold.market_swap_replay_bars_1h`

---

## 6. 表设计原则

## 6.1 命名原则
统一使用：

`<schema>.<domain>_<instrument_type>_<dataset>_<timeframe>`

例如：
- `silver.market_swap_candles_15m`
- `gold.market_spot_replay_bars_1h`

Funding 不带 timeframe：
- `silver.market_swap_funding`

## 6.2 审计字段
所有表统一具备：

- `created_at TIMESTAMPTZ NOT NULL`
- `updated_at TIMESTAMPTZ NOT NULL`

## 6.3 追踪字段
所有事实表尽量具备：

- `ingest_run_id UUID NOT NULL`
- `dataset_version TEXT NOT NULL`
- `quality_flags TEXT[] NOT NULL DEFAULT '{}'`

## 6.4 主键策略
### Candles / Gold replay bars
- 主键：`(symbol, ts)`

### Funding
- 主键：`(symbol, ts)`

### Meta
- 使用 UUID 主键

## 6.5 时间语义
### Candles
- `ts` = bar open timestamp (UTC)

### Funding
- `ts` = funding event timestamp (UTC)

---

## 7. Meta 表详细建表方案

## 7.1 `meta.dataset_manifests`

### 职责
记录数据集版本、来源、覆盖范围、状态。

### 建议字段

- `dataset_id UUID PRIMARY KEY`
- `dataset_name TEXT NOT NULL`
- `dataset_layer TEXT NOT NULL`
- `dataset_domain TEXT NOT NULL`
- `instrument_type TEXT NOT NULL`
- `timeframe TEXT NULL`
- `symbol_scope TEXT NOT NULL`
- `dataset_version TEXT NOT NULL`
- `schema_version TEXT NOT NULL`
- `source_type TEXT NOT NULL`
- `source_dataset_ids UUID[] NOT NULL DEFAULT '{}'`
- `start_ts TIMESTAMPTZ NULL`
- `end_ts TIMESTAMPTZ NULL`
- `row_count BIGINT NULL`
- `status TEXT NOT NULL`
- `storage_table TEXT NOT NULL`
- `notes TEXT NULL`
- `created_at TIMESTAMPTZ NOT NULL`
- `updated_at TIMESTAMPTZ NOT NULL`

### 索引建议
- `(dataset_layer, dataset_domain, instrument_type, timeframe)`
- `(dataset_version)`
- `(status)`

---

## 7.2 `meta.raw_source_files`

### 职责
记录历史下载文件或源文件批次。

### 建议字段

- `source_file_id UUID PRIMARY KEY`
- `source_type TEXT NOT NULL`
- `dataset_domain TEXT NOT NULL`
- `instrument_type TEXT NULL`
- `symbol_hint TEXT NULL`
- `timeframe_hint TEXT NULL`
- `source_granularity TEXT NULL`  -- `day` / `month`
- `source_path TEXT NOT NULL`
- `checksum TEXT NULL`
- `file_size_bytes BIGINT NULL`
- `downloaded_at TIMESTAMPTZ NULL`
- `discovered_at TIMESTAMPTZ NOT NULL`
- `source_start_ts TIMESTAMPTZ NULL`
- `source_end_ts TIMESTAMPTZ NULL`
- `raw_row_count BIGINT NULL`
- `parse_status TEXT NOT NULL`
- `parse_error TEXT NULL`
- `ingested_status TEXT NOT NULL DEFAULT 'pending'`
- `created_at TIMESTAMPTZ NOT NULL`
- `updated_at TIMESTAMPTZ NOT NULL`

### 索引建议
- `(dataset_domain, instrument_type, timeframe_hint)`
- `(checksum)`
- `(parse_status, ingested_status)`

---

## 7.3 `meta.ingest_runs`

### 职责
记录一次 ingestion/build 运行。

### 建议字段
- `ingest_run_id UUID PRIMARY KEY`
- `run_type TEXT NOT NULL`  -- `backfill` / `rolling` / `gap_repair` / `gold_build`
- `dataset_domain TEXT NOT NULL`
- `instrument_type TEXT NULL`
- `symbol TEXT NULL`
- `timeframe TEXT NULL`
- `trigger_mode TEXT NOT NULL`  -- `scheduler` / `manual` / `auto_gap_repair`
- `status TEXT NOT NULL`
- `started_at TIMESTAMPTZ NULL`
- `ended_at TIMESTAMPTZ NULL`
- `attempt_count INT NOT NULL DEFAULT 1`
- `checkpoint_before JSONB NULL`
- `checkpoint_after JSONB NULL`
- `error_message TEXT NULL`
- `notes TEXT NULL`
- `created_at TIMESTAMPTZ NOT NULL`
- `updated_at TIMESTAMPTZ NOT NULL`

### 索引建议
- `(run_type, dataset_domain, status)`
- `(symbol, timeframe)`
- `(started_at DESC)`

---

## 7.4 `meta.ingest_run_items`

### 职责
记录 run 内部的更细粒度工作项。

### 建议字段
- `ingest_run_item_id UUID PRIMARY KEY`
- `ingest_run_id UUID NOT NULL`
- `dataset_domain TEXT NOT NULL`
- `instrument_type TEXT NULL`
- `symbol TEXT NULL`
- `timeframe TEXT NULL`
- `window_start_ts TIMESTAMPTZ NULL`
- `window_end_ts TIMESTAMPTZ NULL`
- `source_file_id UUID NULL`
- `status TEXT NOT NULL`
- `raw_rows_read BIGINT NULL`
- `rows_written_staging BIGINT NULL`
- `rows_written_bronze BIGINT NULL`
- `rows_written_silver BIGINT NULL`
- `rows_written_gold BIGINT NULL`
- `error_message TEXT NULL`
- `created_at TIMESTAMPTZ NOT NULL`
- `updated_at TIMESTAMPTZ NOT NULL`

### 外键
- `ingest_run_id -> meta.ingest_runs.ingest_run_id`
- `source_file_id -> meta.raw_source_files.source_file_id`（nullable）

### 索引建议
- `(ingest_run_id)`
- `(dataset_domain, symbol, timeframe, status)`

---

## 7.5 `meta.ingest_checkpoints`

### 职责
记录 rolling ingestion 的 watermark/gap 状态。

### 建议字段
- `checkpoint_id UUID PRIMARY KEY`
- `dataset_domain TEXT NOT NULL`
- `instrument_type TEXT NOT NULL`
- `symbol TEXT NOT NULL`
- `timeframe TEXT NULL`
- `last_successful_ts TIMESTAMPTZ NULL`
- `last_attempted_ts TIMESTAMPTZ NULL`
- `next_expected_ts TIMESTAMPTZ NULL`
- `backfill_completed BOOLEAN NOT NULL DEFAULT FALSE`
- `gap_detected BOOLEAN NOT NULL DEFAULT FALSE`
- `gap_start_ts TIMESTAMPTZ NULL`
- `gap_end_ts TIMESTAMPTZ NULL`
- `checkpoint_status TEXT NOT NULL`
- `last_ingest_run_id UUID NULL`
- `notes TEXT NULL`
- `created_at TIMESTAMPTZ NOT NULL`
- `updated_at TIMESTAMPTZ NOT NULL`

### 约束
唯一键：
- `(dataset_domain, instrument_type, symbol, timeframe)`

### 索引建议
- `(checkpoint_status)`
- `(symbol, timeframe)`

---

## 7.6 `meta.quality_reports`

### 职责
记录数据质量扫描结果。

### 建议字段
- `quality_report_id UUID PRIMARY KEY`
- `dataset_layer TEXT NOT NULL`
- `dataset_domain TEXT NOT NULL`
- `instrument_type TEXT NULL`
- `symbol TEXT NULL`
- `timeframe TEXT NULL`
- `dataset_version TEXT NOT NULL`
- `window_start_ts TIMESTAMPTZ NULL`
- `window_end_ts TIMESTAMPTZ NULL`
- `missing_intervals_count INT NOT NULL DEFAULT 0`
- `duplicate_rows_count INT NOT NULL DEFAULT 0`
- `out_of_order_rows_count INT NOT NULL DEFAULT 0`
- `invalid_price_rows_count INT NOT NULL DEFAULT 0`
- `invalid_volume_rows_count INT NOT NULL DEFAULT 0`
- `suspect_rows_count INT NOT NULL DEFAULT 0`
- `quality_status TEXT NOT NULL`
- `details JSONB NULL`
- `created_at TIMESTAMPTZ NOT NULL`
- `updated_at TIMESTAMPTZ NOT NULL`

### 索引建议
- `(dataset_layer, dataset_domain, instrument_type, timeframe)`
- `(quality_status)`
- `(dataset_version)`

---

## 8. Staging 表详细建表方案

### 设计目标
- 接住 collector 的批次写入
- 支持 validation 前检查
- 支持 merge 入 Bronze/Silver
- 保留 run 边界

---

## 8.1 Candles staging 通用结构

适用于所有：
- `staging.market_spot_candles_*`
- `staging.market_swap_candles_*`

### 建议字段
- `staging_row_id BIGSERIAL PRIMARY KEY`
- `symbol TEXT NOT NULL`
- `ts TIMESTAMPTZ NOT NULL`
- `open NUMERIC(20,10) NOT NULL`
- `high NUMERIC(20,10) NOT NULL`
- `low NUMERIC(20,10) NOT NULL`
- `close NUMERIC(20,10) NOT NULL`
- `vol NUMERIC(28,10) NULL`
- `vol_ccy NUMERIC(28,10) NULL`
- `vol_quote NUMERIC(28,10) NULL`
- `confirm BOOLEAN NOT NULL DEFAULT TRUE`
- `raw_symbol TEXT NULL`
- `raw_ts TEXT NULL`
- `source_file_id UUID NULL`
- `ingest_run_id UUID NOT NULL`
- `dataset_version TEXT NOT NULL`
- `quality_flags TEXT[] NOT NULL DEFAULT '{}'`
- `created_at TIMESTAMPTZ NOT NULL`
- `updated_at TIMESTAMPTZ NOT NULL`

### 索引建议
- `(symbol, ts)`
- `(ingest_run_id)`

---

## 8.2 Funding staging 通用结构

适用于：
- `staging.market_swap_funding`

### 建议字段
- `staging_row_id BIGSERIAL PRIMARY KEY`
- `symbol TEXT NOT NULL`
- `ts TIMESTAMPTZ NOT NULL`
- `funding_rate NUMERIC(18,12) NOT NULL`
- `inst_type TEXT NULL`
- `formula_type TEXT NULL`
- `method TEXT NULL`
- `realized_rate NUMERIC(18,12) NULL`
- `raw_symbol TEXT NULL`
- `raw_ts TEXT NULL`
- `source_file_id UUID NULL`
- `ingest_run_id UUID NOT NULL`
- `dataset_version TEXT NOT NULL`
- `quality_flags TEXT[] NOT NULL DEFAULT '{}'`
- `created_at TIMESTAMPTZ NOT NULL`
- `updated_at TIMESTAMPTZ NOT NULL`

### 索引建议
- `(symbol, ts)`
- `(ingest_run_id)`

---

## 9. Bronze 表详细建表方案

### 设计目标
- 保留来源语义
- 保留 raw trace
- 支持后续规范化与回溯

---

## 9.1 Candles Bronze 通用结构

适用于所有：
- `bronze.market_spot_candles_*`
- `bronze.market_swap_candles_*`

### 建议字段
- `symbol TEXT NOT NULL`
- `ts TIMESTAMPTZ NOT NULL`
- `open NUMERIC(20,10) NOT NULL`
- `high NUMERIC(20,10) NOT NULL`
- `low NUMERIC(20,10) NOT NULL`
- `close NUMERIC(20,10) NOT NULL`
- `vol NUMERIC(28,10) NULL`
- `vol_ccy NUMERIC(28,10) NULL`
- `vol_quote NUMERIC(28,10) NULL`
- `confirm BOOLEAN NOT NULL DEFAULT TRUE`
- `raw_symbol TEXT NULL`
- `raw_ts TEXT NULL`
- `source_file_id UUID NULL`
- `ingest_run_id UUID NOT NULL`
- `dataset_version TEXT NOT NULL`
- `quality_flags TEXT[] NOT NULL DEFAULT '{}'`
- `created_at TIMESTAMPTZ NOT NULL`
- `updated_at TIMESTAMPTZ NOT NULL`

### 主键
- `(symbol, ts)`

### 索引建议
- `(ts)`
- `(source_file_id)`
- `(ingest_run_id)`

---

## 9.2 Funding Bronze 通用结构

适用于：
- `bronze.market_swap_funding`

### 建议字段
- `symbol TEXT NOT NULL`
- `ts TIMESTAMPTZ NOT NULL`
- `funding_rate NUMERIC(18,12) NOT NULL`
- `inst_type TEXT NULL`
- `formula_type TEXT NULL`
- `method TEXT NULL`
- `realized_rate NUMERIC(18,12) NULL`
- `raw_symbol TEXT NULL`
- `raw_ts TEXT NULL`
- `source_file_id UUID NULL`
- `ingest_run_id UUID NOT NULL`
- `dataset_version TEXT NOT NULL`
- `quality_flags TEXT[] NOT NULL DEFAULT '{}'`
- `created_at TIMESTAMPTZ NOT NULL`
- `updated_at TIMESTAMPTZ NOT NULL`

### 主键
- `(symbol, ts)`

### 索引建议
- `(ts)`
- `(ingest_run_id)`

---

## 10. Silver 表详细建表方案

### 设计目标
- 平台唯一 canonical truth
- 供 replay / analytics / later live attribution 使用

---

## 10.1 Candles Silver 通用结构

适用于：
- `silver.market_spot_candles_*`
- `silver.market_swap_candles_*`

### 建议字段
- `symbol TEXT NOT NULL`
- `ts TIMESTAMPTZ NOT NULL`
- `open NUMERIC(20,10) NOT NULL`
- `high NUMERIC(20,10) NOT NULL`
- `low NUMERIC(20,10) NOT NULL`
- `close NUMERIC(20,10) NOT NULL`
- `vol NUMERIC(28,10) NULL`
- `vol_ccy NUMERIC(28,10) NULL`
- `vol_quote NUMERIC(28,10) NULL`
- `confirm BOOLEAN NOT NULL DEFAULT TRUE`
- `raw_symbol TEXT NULL`
- `raw_ts TEXT NULL`
- `source_file_id UUID NULL`
- `ingest_run_id UUID NOT NULL`
- `dataset_version TEXT NOT NULL`
- `quality_flags TEXT[] NOT NULL DEFAULT '{}'`
- `created_at TIMESTAMPTZ NOT NULL`
- `updated_at TIMESTAMPTZ NOT NULL`

### 主键
- `(symbol, ts)`

### 索引建议
- `(ts)`
- `(symbol, ts)`
- `(ingest_run_id)`
- `(dataset_version)`

---

## 10.2 Funding Silver 通用结构

适用于：
- `silver.market_swap_funding`

### 建议字段
- `symbol TEXT NOT NULL`
- `ts TIMESTAMPTZ NOT NULL`
- `funding_rate NUMERIC(18,12) NOT NULL`
- `inst_type TEXT NULL`
- `formula_type TEXT NULL`
- `method TEXT NULL`
- `realized_rate NUMERIC(18,12) NULL`
- `raw_symbol TEXT NULL`
- `raw_ts TEXT NULL`
- `source_file_id UUID NULL`
- `ingest_run_id UUID NOT NULL`
- `dataset_version TEXT NOT NULL`
- `quality_flags TEXT[] NOT NULL DEFAULT '{}'`
- `created_at TIMESTAMPTZ NOT NULL`
- `updated_at TIMESTAMPTZ NOT NULL`

### 主键
- `(symbol, ts)`

### 索引建议
- `(ts)`
- `(ingest_run_id)`
- `(dataset_version)`

---

## 11. Gold 表详细建表方案

### 设计目标
- replay-ready
- derived from Silver
- funding-aligned when applicable
- rebuildable

---

## 11.1 Replay Bars Gold 通用结构

适用于：
- `gold.market_spot_replay_bars_*`
- `gold.market_swap_replay_bars_*`

### 建议字段
- `symbol TEXT NOT NULL`
- `ts TIMESTAMPTZ NOT NULL`
- `open NUMERIC(20,10) NOT NULL`
- `high NUMERIC(20,10) NOT NULL`
- `low NUMERIC(20,10) NOT NULL`
- `close NUMERIC(20,10) NOT NULL`
- `volume NUMERIC(28,10) NULL`
- `quote_volume NUMERIC(28,10) NULL`
- `is_closed BOOLEAN NOT NULL DEFAULT TRUE`
- `aligned_funding_rate NUMERIC(18,12) NULL`
- `funding_source_ts TIMESTAMPTZ NULL`
- `source_candle_dataset_version TEXT NOT NULL`
- `source_funding_dataset_version TEXT NULL`
- `build_run_id UUID NOT NULL`
- `quality_flags TEXT[] NOT NULL DEFAULT '{}'`
- `created_at TIMESTAMPTZ NOT NULL`
- `updated_at TIMESTAMPTZ NOT NULL`

### 主键
- `(symbol, ts)`

### 索引建议
- `(ts)`
- `(build_run_id)`
- `(source_candle_dataset_version)`

---

## 12. 索引与约束设计

## 12.1 通用约束
- 所有 `status` 字段应加 CHECK 约束或枚举约束
- 所有 `dataset_layer` / `dataset_domain` / `instrument_type` / `timeframe` 应有受控值域
- `quality_flags` 默认为空数组，不允许 NULL
- `created_at` / `updated_at` 均 NOT NULL

## 12.2 唯一性
### Candles / Funding facts
- PK `(symbol, ts)`

### Checkpoints
- UNIQUE `(dataset_domain, instrument_type, symbol, timeframe)`

## 12.3 索引优先级
### 高优先级
- facts: `(symbol, ts)`
- checkpoint lookup
- ingest run lookup
- dataset_version lookup

### 低优先级（后置）
- 复杂 partial index
- JSONB GIN index
- advanced covering index

---

## 13. Migration 分阶段策略

Phase 1 migration 不要做成一个超大 SQL。  
建议分阶段推进：

1. 建 schema
2. 建 meta tables
3. 建 staging tables
4. 建 bronze tables
5. 建 silver tables
6. 建 gold tables
7. 建索引与约束补强
8. 建辅助函数/更新时间触发器（可选）

---

## 14. Migration 文件拆分建议

建议采用如下文件拆分：

1. `0001_create_research_schemas.sql`
2. `0002_create_meta_tables.sql`
3. `0003_create_staging_candle_tables.sql`
4. `0004_create_staging_funding_table.sql`
5. `0005_create_bronze_candle_tables.sql`
6. `0006_create_bronze_funding_table.sql`
7. `0007_create_silver_candle_tables.sql`
8. `0008_create_silver_funding_table.sql`
9. `0009_create_gold_replay_bar_tables.sql`
10. `0010_add_indexes_and_constraints.sql`

如果你后面想用 Alembic，也可以把这些逻辑转成 migration revision，但 Phase 1 先保持 SQL-first 更直观。

---

## 15. 每个 Migration 的任务书

## 15.1 `0001_create_research_schemas.sql`
### 任务
- 创建 `meta`
- 创建 `staging`
- 创建 `bronze`
- 创建 `silver`
- 创建 `gold`

### 验收
- 所有 schema 创建成功
- migration 可重复执行时不会破坏已有对象

---

## 15.2 `0002_create_meta_tables.sql`
### 任务
- 建 `dataset_manifests`
- 建 `raw_source_files`
- 建 `ingest_runs`
- 建 `ingest_run_items`
- 建 `ingest_checkpoints`
- 建 `quality_reports`

### 验收
- 所有 meta 表创建成功
- 主键、唯一约束、外键基本可用

---

## 15.3 `0003_create_staging_candle_tables.sql`
### 任务
- 建 8 张 staging candle 表

### 验收
- 8 张表全部存在
- 列一致，仅表名差异
- 索引存在

---

## 15.4 `0004_create_staging_funding_table.sql`
### 任务
- 建 `staging.market_swap_funding`

### 验收
- 字段支持 file + API 两类 funding 输入
- 扩展字段为 nullable

---

## 15.5 `0005_create_bronze_candle_tables.sql`
### 任务
- 建 8 张 bronze candle 表

### 验收
- `(symbol, ts)` 主键生效
- raw trace 字段齐全

---

## 15.6 `0006_create_bronze_funding_table.sql`
### 任务
- 建 `bronze.market_swap_funding`

### 验收
- `(symbol, ts)` 主键生效
- funding API 扩展字段齐全

---

## 15.7 `0007_create_silver_candle_tables.sql`
### 任务
- 建 8 张 silver candle 表

### 验收
- canonical 字段齐全
- 主键与索引生效

---

## 15.8 `0008_create_silver_funding_table.sql`
### 任务
- 建 `silver.market_swap_funding`

### 验收
- canonical funding 字段齐全
- optional extension fields 齐全

---

## 15.9 `0009_create_gold_replay_bar_tables.sql`
### 任务
- 建 8 张 gold replay bar 表

### 验收
- funding alignment 字段存在
- build trace 字段存在

---

## 15.10 `0010_add_indexes_and_constraints.sql`
### 任务
- 补充非主键索引
- 补充 check constraints
- 补充必要唯一约束

### 验收
- explain 基础查询路径可接受
- 不引入明显重复或冲突约束

---

## 16. 实施顺序

### 推荐顺序
1. 先落 schema 与 meta
2. 再落 staging
3. 再落 bronze
4. 再落 silver
5. 最后落 gold
6. 再补索引与约束

### 为什么
因为 collector / normalize pipeline 的开发，会最先依赖：
- meta
- staging
- bronze / silver

gold 可以稍后再接。

---

## 17. 验收标准

以下条件全部满足，才算 Phase 1 数据库建表方案完成：

### 结构层
- [ ] `meta / staging / bronze / silver / gold` schema 建立
- [ ] 所有最小表创建完成
- [ ] 命名规则一致

### 约束层
- [ ] 主键正确
- [ ] checkpoint 唯一键正确
- [ ] 必要索引存在
- [ ] 审计字段齐全

### 语义层
- [ ] candles / funding 字段与已验证 schema freeze 保持一致
- [ ] Bronze/Silver/Gold 分层职责没有混淆
- [ ] Gold 保持可重建语义

### 工程层
- [ ] migration 可按顺序执行
- [ ] 新 database 可从零建成
- [ ] migration 文档足够让 Codex / Claude Code 直接开工

---

## 18. 非目标与边界

以下内容不属于本建表与 migration 文档范围：

- collector 的完整 Python 实现
- scheduler 实现
- gap repair 实现
- normalize / merge SQL 过程实现
- replay runner 实现
- live-vs-replay diff 实现

这些都依赖本文件产出的数据库结构，但不由本文件定义。

---

## 结论

到这一层为止，Phase 1 已经可以从“调查与冻结”进入“建库与迁移实施”阶段。

下一步工程动作应是：

1. 根据本文件产出 migration SQL
2. 在独立 PostgreSQL database 中执行建库
3. 先打通 candles/funding 的 staging -> bronze -> silver
4. 再接 gold replay bars
