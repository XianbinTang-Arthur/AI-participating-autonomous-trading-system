# Research Data Platform — Phase 1 总 README / 总设计总览

> 这是 Research Data Platform Phase 1 的总入口文档。  
> 它的作用不是替代各子文档，而是把：
>
> - 目标
> - 设计边界
> - 已冻结决策
> - 文档结构
> - 实施顺序
> - 交付状态
>
> 收口成一个统一入口。  
> 任何人开始接手 Phase 1 前，都应先阅读本文件。

---

## 目录

- [1. 项目概述](#1-项目概述)
- [2. 为什么要做这个平台](#2-为什么要做这个平台)
- [3. Phase 1 的核心目标](#3-phase-1-的核心目标)
- [4. Phase 1 不做什么](#4-phase-1-不做什么)
- [5. Phase 1 的总体架构](#5-phase-1-的总体架构)
- [6. 历史补齐与增量采集模型](#6-历史补齐与增量采集模型)
- [7. 数据库与分层模型](#7-数据库与分层模型)
- [8. 已完成的真实样本调查](#8-已完成的真实样本调查)
- [9. 已冻结的关键 schema 决策](#9-已冻结的关键-schema-决策)
- [10. Phase 1 文档包说明](#10-phase-1-文档包说明)
- [11. Phase 1 实施顺序](#11-phase-1-实施顺序)
- [12. 当前可直接开工的内容](#12-当前可直接开工的内容)
- [13. 推荐的 agent 协作方式](#13-推荐的-agent-协作方式)
- [14. Phase 1 验收标准](#14-phase-1-验收标准)
- [15. 风险与边界](#15-风险与边界)
- [16. 下一阶段展望](#16-下一阶段展望)

---

## 1. 项目概述

### 项目名称
**Research Data Platform**

### 当前阶段
**Phase 1**

### 服务对象
当前阶段，这个平台首先服务于：

> **你的自动交易系统本身**

它不是先做成给外部团队共享的数据平台，也不是先做成产品化 UI。  
它的首要目标是为你的交易系统提供：

- 统一口径的历史市场数据
- 持续更新的近期市场数据
- 可供研究、回放、参数扫描使用的基础数据资产

---

## 2. 为什么要做这个平台

当前问题不是“缺几个分析脚本”，而是：

- 历史数据、回放、参数扫描、live 排查没有统一数据口径
- 文件源、API 源、研究输入没有标准化边界
- 研究结果无法稳定复现
- live 运行问题难以归因到 strategy / budget / risk / execution 的具体层次
- 后面想接 trades / orderbook / funding / account snapshots 时，如果没有统一底座，会越来越乱

因此，这个项目不是在做一个“分析模块”，而是在建设一套：

> **研究与回放数据底座**

Phase 1 先解决：
- 数据如何稳定进来
- 如何标准化
- 如何分层存储
- 如何为后续 replay 与归因打底

---

## 3. Phase 1 的核心目标

Phase 1 的唯一核心目标可以归纳为：

> **建立一个以 PostgreSQL 为主仓库、支持历史补齐与 API 增量采集的统一市场数据底座。**

更具体地说，Phase 1 要做到：

1. 能导入 **OKX 历史下载文件**
2. 能持续通过 **OKX REST API** 拉取最近数据
3. 让两条来源最终进入 **同一套 canonical schema**
4. 让数据分层进入：
   - `staging`
   - `bronze`
   - `silver`
   - `gold`
5. 让 `silver` 成为后续 replay / analytics / 归因的唯一标准输入
6. 让 `gold` 提供 replay-ready bars

---

## 4. Phase 1 不做什么

Phase 1 明确不以以下内容为交付目标：

- 完整的 live-vs-replay 归因系统
- trades / orderbook 全量接入
- execution realism 建模
- 参数扫描产品化
- dashboard / Web UI
- Airflow / 重型调度平台接入
- 全面的实验结果数据库化

这些属于后续阶段。

---

## 5. Phase 1 的总体架构

Phase 1 已冻结的总体架构如下：

### 5.1 平台边界
Research Data Platform 是：

- **独立服务进程**
- 与交易平台紧密协作
- 但不与交易平台共享运行进程
- 使用同一个 PostgreSQL 实例中的**独立 database**

### 5.2 主仓库
PostgreSQL 是 Phase 1 的**主仓库**，不是 parquet-first、也不是 notebook-first。

### 5.3 分层
数据库中采用 5 个 schema：

- `meta`
- `staging`
- `bronze`
- `silver`
- `gold`

### 5.4 覆盖数据域
Phase 1 只做：

- **candles**
- **funding**

### 5.5 覆盖品种
- `BTC-USDT`
- `ETH-USDT`
- `BTC-USDT-SWAP`
- `ETH-USDT-SWAP`

### 5.6 覆盖周期
- `1m`
- `5m`
- `15m`
- `1H`

---

## 6. 历史补齐与增量采集模型

Phase 1 使用**双通道模型**：

### 6.1 Historical Backfill
来源：
- OKX 官方历史下载文件

特点：
- 按 **日维度 / 月维度文件包** 导入
- 调度单元是 **source file**
- 不是任意起止时间窗口拉取

标准流程：

`source_file -> staging -> bronze -> silver -> gold(optional)`

### 6.2 Rolling Incremental Ingestion
来源：
- OKX REST API

特点：
- 常驻 scheduler 驱动
- 按 symbol / timeframe 持续抓取
- 维护 checkpoint / watermark
- 自动检测缺口并回补

标准流程：

`api_response -> staging -> bronze -> silver -> gold(optional)`

### 6.3 统一写入规则
两条通道都必须经过：

> **staging -> validate -> merge**

不允许外部 collector 直接写 canonical tables。

---

## 7. 数据库与分层模型

### 7.1 Meta
负责：
- dataset manifests
- raw source files
- ingest runs
- ingest run items
- checkpoints
- quality reports

### 7.2 Staging
负责：
- collector 批次落点
- 原始结构化临时存储
- 供 validate / merge 使用

### 7.3 Bronze
负责：
- 结构化来源层
- 保留 raw trace
- 保留原始数量语义

### 7.4 Silver
负责：
- canonical truth
- 统一 symbol / 时间语义 / numeric parsing
- 作为 replay / analytics 的标准输入

### 7.5 Gold
负责：
- replay-ready derived bars
- 对 swap bars 做 funding 对齐
- 保持可重建，不是最终真相层

---

## 8. 已完成的真实样本调查

Phase 1-A 已基于**真实样本**完成初步调查与 schema 冻结。

### 8.1 历史文件样本
已验证：

#### Candles
- `BTC-USDT`
- `ETH-USDT`
- `BTC-USDT-SWAP`
- `ETH-USDT-SWAP`

#### Funding
- `BTC-USDT-SWAP`
- `ETH-USDT-SWAP`

### 8.2 API 样本
已验证：

#### Candles
- 4 个 instrument × 4 个 timeframe

#### Funding
- `BTC-USDT-SWAP`
- `ETH-USDT-SWAP`

### 8.3 已确认结论
- candles 文件源与 API 源原始结构不同，但可收敛到同一 schema
- candles `ts/open_time` 统一为 **bar open timestamp**
- funding 文件与 API 可收敛到同一 funding schema
- `vol / vol_ccy / vol_quote` 必须保留，不能过早压缩语义
- funding API 比历史 funding 文件字段更丰富，因此 funding schema 需允许 nullable 扩展字段

---

## 9. 已冻结的关键 schema 决策

### 9.1 物理表组织
- 现货 / 合约分表
- candles 按 timeframe 分表
- funding 独立表
- gold replay bars 也按 spot/swap + timeframe 分表

### 9.2 主键
#### Candles / Funding / Replay Bars
- `(symbol, ts)`

### 9.3 时间语义
#### Candles
- `ts` = bar open timestamp (UTC)

#### Funding
- `ts` = funding event timestamp (UTC)

### 9.4 审计字段
所有表都保留：
- `created_at`
- `updated_at`

### 9.5 追踪字段
事实表保留：
- `ingest_run_id`
- `dataset_version`
- `quality_flags`

### 9.6 分层语义
- Bronze：来源真相保留层
- Silver：唯一 canonical truth
- Gold：可重建 replay-ready layer

---

## 10. Phase 1 文档包说明

下面这些文档构成当前 Phase 1 的完整设计链路。

### A. 设计决策
- `Phase1_Historical_Backfill_and_Incremental_Ingestion_Decision.md`

### B. 数据库设计与迁移
- `Phase1_Database_Plan_and_Migration_Taskbook.md`
- `Phase1_Migration_SQL_Skeleton.zip`

### C. 数据流水线实现
- `Phase1_Collector_Normalize_Merge_Taskbook.md`

### D. 实施排期与 backlog
- `Phase1_Implementation_Schedule_and_Issue_Backlog.md`

### E. Agent 开工提示词
- `Codex_ClaudeCode_Phase1_Prompt_Pack.md`

### F. 调查与 schema freeze
- `source_survey_okx_candles.md`
- `source_survey_okx_funding.md`
- `field_mapping_okx_candles.md`
- `field_mapping_okx_funding.md`
- `time_semantics_phase1.md`
- `schema_validation_phase1.md`
- `schema_freeze_phase1_v1.md`

---

## 11. Phase 1 实施顺序

推荐严格按下面顺序推进：

### Step 1
数据库与 migration
- schema
- meta tables
- staging / bronze / silver / gold tables

### Step 2
历史文件 backfill 主链
- file discovery
- parser
- staging 写入
- run tracking

### Step 3
rolling API 主链
- candles API collector
- funding API collector
- checkpoint 推进

### Step 4
normalize / validate / merge
- canonical symbol
- UTC timestamp
- quality report
- bronze merge
- silver merge

### Step 5
gap detection / gap repair
- 先检测
- 再自动创建 repair run

### Step 6
gold replay bars
- swap funding 对齐
- replay-ready bars 落 Gold

---

## 12. 当前可直接开工的内容

按照现在已有文档，Codex / Claude Code 已经可以直接开工实现：

### 已可直接开工
- migration SQL
- meta tables
- staging tables
- bronze tables
- silver tables
- gold replay bar tables
- historical file discovery
- candles/funding 历史文件 parser
- staging writers
- candles/funding API collectors
- checkpoint manager
- quality report writer
- staging -> bronze merge
- bronze -> silver merge
- gold replay bar builder

### 尚未要求实现
- replay engine 本体
- parameter scan
- live attribution
- UI

---

## 13. 推荐的 agent 协作方式

不要让单个 agent 一口气实现整个 Phase 1。

### 最推荐做法
按 milestone 或 issue group 分开：

#### Agent A
- 数据库与 migration

#### Agent B
- 历史文件 backfill

#### Agent C
- rolling API collectors

#### Agent D
- normalize / validate / merge

#### Agent E
- gold replay bars

### 约束
所有 agent 必须使用统一的：
- schema freeze
- field mapping
- time semantics
- prompt pack

---

## 14. Phase 1 验收标准

Phase 1 完成时，至少应达到：

### 数据层
- 历史 candles / funding 文件可导入
- API candles / funding 可持续拉取
- checkpoint 正常推进
- quality reports 可生成

### 仓库层
- `staging`
- `bronze`
- `silver`
- `gold`
全部可写入并可追踪

### 语义层
- Silver 已成为 canonical truth
- Gold replay bars 可生成
- swap Gold bars 可带 funding 对齐

### 控制层
- ingest_runs 可追踪
- ingest_run_items 可追踪
- raw_source_files 可追踪
- ingest_checkpoints 可追踪
- quality_reports 可追踪

---

## 15. 风险与边界

### 风险 1
文件源与 API 源在未来可能出现字段变动

**缓解**：
- 统一走 field mapping 文档
- 保留 raw trace
- API optional fields 允许 nullable

### 风险 2
过早把 Gold 扩展成 feature layer

**缓解**：
- Phase 1 只做 replay-ready bars
- feature tables 后置

### 风险 3
rolling 还未稳定就过早做 gap repair

**缓解**：
- 先让 checkpoint 推进稳定
- 再自动 repair

### 风险 4
agent 擅自改架构

**缓解**：
- 使用统一 prompt pack
- 明确禁止改主仓、分层和表组织策略

---

## 16. 下一阶段展望

当 Phase 1 收口后，后续阶段大致是：

### Phase 2
研究与回放能力产品化
- replay runner
- experiment registry
- parameter scan
- diagnostics

### Phase 3
live attribution
- live 决策数据抽取
- replay vs live diff

### Phase 4
execution realism
- trades
- orderbook
- slippage / fill feasibility

### Phase 5+
治理与平台化
- 文档体系
- 调度与告警
- 数据契约
- 长期运维

---

## 最后一段建议

如果你现在要把这个项目交给 Codex / Claude Code 或未来的协作者，最好的入口顺序是：

1. 先看本 README
2. 再看 `Phase1_Historical_Backfill_and_Incremental_Ingestion_Decision.md`
3. 再看 `schema_freeze_phase1_v1.md`
4. 再看 `Phase1_Database_Plan_and_Migration_Taskbook.md`
5. 再看 `Phase1_Collector_Normalize_Merge_Taskbook.md`
6. 最后按 `Phase1_Implementation_Schedule_and_Issue_Backlog.md` 开工

---

## 一句话总结

**Phase 1 不是在做几个脚本，而是在建设一个以 PostgreSQL 为主仓、支持历史补齐与持续增量采集、具有 Bronze/Silver/Gold 分层并能为后续 replay 与归因打底的 Research Data Platform 数据底座。**
