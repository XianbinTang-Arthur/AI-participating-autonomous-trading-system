# Phase 1 实施排期与 Issue Backlog

> 本文档用于把 Phase 1 的设计与任务书收口成可执行的工程排期与 issue backlog，供 Codex / Claude Code 或人工开发直接开工。
>
> 本文档承接：
>
> - `Phase1_Historical_Backfill_and_Incremental_Ingestion_Decision.md`
> - `Phase1_Database_Plan_and_Migration_Taskbook.md`
> - `Phase1_Migration_SQL_Skeleton.zip`
> - `Phase1_Collector_Normalize_Merge_Taskbook.md`
> - `Phase1A_Documents.zip`

---

## 目录

- [1. 文档目标](#1-文档目标)
- [2. Phase 1 总体里程碑](#2-phase-1-总体里程碑)
- [3. 推荐实施顺序](#3-推荐实施顺序)
- [4. Issue 分组](#4-issue-分组)
- [5. Milestone 1：数据库与迁移](#5-milestone-1数据库与迁移)
- [6. Milestone 2：历史文件 Backfill 主链](#6-milestone-2历史文件-backfill-主链)
- [7. Milestone 3：Rolling API 主链](#7-milestone-3rolling-api-主链)
- [8. Milestone 4：Quality / Checkpoint / Gap Repair](#8-milestone-4quality--checkpoint--gap-repair)
- [9. Milestone 5：Gold Replay Bars](#9-milestone-5gold-replay-bars)
- [10. 优先级与依赖关系](#10-优先级与依赖关系)
- [11. 建议的 Issue 模板](#11-建议的-issue-模板)
- [12. DoD（Definition of Done）](#12-doddefinition-of-done)
- [13. 风险点](#13-风险点)
- [14. 最终交付状态](#14-最终交付状态)

---

## 1. 文档目标

本文档回答三个问题：

1. Phase 1 应该按什么顺序实现
2. 应该拆成哪些 issue
3. 每个 issue 完成的标准是什么

目标不是写大而全项目计划，而是生成：

> **可直接分派、可直接跟踪、可直接验收的 backlog**

---

## 2. Phase 1 总体里程碑

建议把 Phase 1 划分为 5 个里程碑：

### Milestone 1
**数据库与迁移**

目标：
- 建好数据库、schema、表、索引、基本约束

### Milestone 2
**历史文件 Backfill 主链**

目标：
- candles/funding 历史文件能够自动发现、解析、入 staging、merge 到 bronze/silver

### Milestone 3
**Rolling API 主链**

目标：
- candles/funding API 增量采集可运行
- checkpoint 可推进

### Milestone 4
**Quality / Checkpoint / Gap Repair**

目标：
- 质量报告可生成
- gap 可发现
- 自动回补可跑通

### Milestone 5
**Gold Replay Bars**

目标：
- replay-ready Gold bars 能从 Silver 构建
- swap Gold bars 带 funding 对齐

---

## 3. 推荐实施顺序

必须按下面顺序推进：

1. **先数据库**
2. **再历史文件 backfill**
3. **再 rolling API**
4. **再 quality / checkpoint / gap repair**
5. **最后 gold replay bars**

原因：
- 没有数据库与 schema，collector 没有稳定落点
- 没有 backfill，仓库没有历史基线
- 没有 rolling，仓库不具备持续更新能力
- 没有 checkpoint/gap repair，rolling 不能长期可靠
- Gold 依赖 Silver 稳定后再做最稳

---

## 4. Issue 分组

建议 issue 分为 6 组：

### A. Infra / DB
- schema
- migration
- connection setup

### B. Meta / Control Plane
- run tracking
- raw source file registry
- checkpoint manager
- quality report writer

### C. Backfill
- file discovery
- zip/csv parser
- candles backfill
- funding backfill

### D. Rolling
- candles API collector
- funding API collector

### E. Normalize / Validate / Merge
- symbol mapping
- time normalization
- quality checks
- bronze merge
- silver merge

### F. Gold
- funding alignment
- replay bar builder

---

## 5. Milestone 1：数据库与迁移

## Issue 1
### 标题
Create research database schemas and migration baseline

### 目标
建立：
- `meta`
- `staging`
- `bronze`
- `silver`
- `gold`

### 依赖
无

### 产出
- migration 执行通过
- schema 可见

### 验收标准
- 新 database 可成功建 schema
- migration 可重复执行不报灾难性错误

---

## Issue 2
### 标题
Create meta tables for manifests, runs, checkpoints, and quality reports

### 目标
建：
- `meta.dataset_manifests`
- `meta.raw_source_files`
- `meta.ingest_runs`
- `meta.ingest_run_items`
- `meta.ingest_checkpoints`
- `meta.quality_reports`

### 依赖
Issue 1

### 验收标准
- 表全部存在
- 主键、基本唯一约束存在
- 基础索引存在

---

## Issue 3
### 标题
Create staging, bronze, silver, and gold fact tables for candles and funding

### 目标
建：
- 8 张 staging candles
- 1 张 staging funding
- 8 张 bronze candles
- 1 张 bronze funding
- 8 张 silver candles
- 1 张 silver funding
- 8 张 gold replay bars

### 依赖
Issue 1

### 验收标准
- 所有事实表存在
- 主键存在
- 审计字段齐全
- 索引可用

---

## 6. Milestone 2：历史文件 Backfill 主链

## Issue 4
### 标题
Implement raw source file discovery and registration

### 目标
扫描历史下载目录，把文件登记到：
- `meta.raw_source_files`

### 依赖
Issue 2

### 验收标准
- 新文件可被发现
- checksum / path / hint 字段可写入
- 重复文件不会重复登记

---

## Issue 5
### 标题
Implement historical candle file parser

### 目标
解析：
- spot candles zip
- swap candles zip

### 依赖
Issue 4

### 验收标准
- 支持解析你当前样本文件
- 能输出统一的 raw row 结构
- 能提取列：
  - instrument_name
  - open/high/low/close
  - vol/vol_ccy/vol_quote
  - open_time
  - confirm

---

## Issue 6
### 标题
Implement historical funding file parser

### 目标
解析 funding zip。

### 依赖
Issue 4

### 验收标准
- 能解析当前 funding 样本
- 能提取：
  - instrument_name
  - funding_rate
  - funding_time

---

## Issue 7
### 标题
Implement candle backfill staging writer

### 目标
把历史 candles 文件解析结果写入对应 staging 表。

### 依赖
Issue 3, Issue 5

### 验收标准
- 可写入 4 组 instrument 的 candles staging
- 绑定 ingest_run_id
- raw_symbol / raw_ts 可见

---

## Issue 8
### 标题
Implement funding backfill staging writer

### 目标
把历史 funding 文件解析结果写入 `staging.market_swap_funding`

### 依赖
Issue 3, Issue 6

### 验收标准
- funding 样本可写入 staging
- 绑定 ingest_run_id
- API 扩展字段可保留为空

---

## Issue 9
### 标题
Implement historical backfill run tracking

### 目标
backfill 执行时正确写入：
- `meta.ingest_runs`
- `meta.ingest_run_items`

### 依赖
Issue 2, Issue 7, Issue 8

### 验收标准
- 每次 backfill 都有 run
- 每个文件对应 item
- 成功/失败状态清晰

---

## 7. Milestone 3：Rolling API 主链

## Issue 10
### 标题
Implement candles API collector

### 目标
调用 OKX candles API 抓取增量 candles。

### 依赖
Issue 2, Issue 3

### 验收标准
- 支持 4 个 instrument × 4 个 timeframe
- 支持 request pacing
- 原始响应可追踪
- 能写入 staging

---

## Issue 11
### 标题
Implement funding API collector

### 目标
调用 funding history API 抓取近期 funding。

### 依赖
Issue 2, Issue 3

### 验收标准
- 支持 BTC/ETH swap funding
- API 扩展字段可入 staging
- 原始响应可追踪

---

## Issue 12
### 标题
Implement rolling ingest checkpoints

### 目标
从 `meta.ingest_checkpoints` 读取和推进 watermark。

### 依赖
Issue 2, Issue 10, Issue 11

### 验收标准
- candles checkpoint 能推进
- funding checkpoint 能推进
- next_expected_ts 正常更新

---

## Issue 13
### 标题
Implement rolling staging writer

### 目标
将 API 返回写入 staging 表。

### 依赖
Issue 10, Issue 11

### 验收标准
- candles/funding 都可入 staging
- 绑定 ingest_run_id
- raw_ts 保留

---

## 8. Milestone 4：Quality / Checkpoint / Gap Repair

## Issue 14
### 标题
Implement symbol and time normalization utilities

### 目标
实现：
- canonical symbol mapping
- UTC timestamp normalization

### 依赖
Issue 3

### 验收标准
- 文件源和 API 源都能统一走同一套 mapper
- candles / funding 时间语义正确

---

## Issue 15
### 标题
Implement candle quality validation

### 目标
对 staging / bronze candles 执行质量检查。

### 依赖
Issue 7, Issue 13, Issue 14

### 验收标准
- 可检测 duplicate / missing / ordering / invalid OHLC
- 写 quality report

---

## Issue 16
### 标题
Implement funding quality validation

### 目标
对 staging / bronze funding 执行质量检查。

### 依赖
Issue 8, Issue 13, Issue 14

### 验收标准
- 可检测 duplicate / ordering / invalid funding_rate
- 写 quality report

---

## Issue 17
### 标题
Implement bronze merge pipeline

### 目标
staging -> bronze

### 依赖
Issue 7, Issue 8, Issue 13, Issue 14, Issue 15, Issue 16

### 验收标准
- candles/funding 都能 merge 到 bronze
- 幂等性可接受
- 主键不冲突

---

## Issue 18
### 标题
Implement silver merge pipeline

### 目标
bronze -> silver

### 依赖
Issue 17

### 验收标准
- candles/funding 都能进入 Silver canonical tables
- source trace 字段保留
- dataset_version 可追踪

---

## Issue 19
### 标题
Implement automatic gap detection and repair job creation

### 目标
发现缺口并生成 `gap_repair` run。

### 依赖
Issue 12, Issue 15, Issue 16, Issue 18

### 验收标准
- 至少能识别一个缺口
- 至少能创建一个 gap_repair run
- 缺口状态反映到 checkpoint

---

## 9. Milestone 5：Gold Replay Bars

## Issue 20
### 标题
Implement swap funding alignment for replay bars

### 目标
把 Silver funding 对齐到 Silver swap candles。

### 依赖
Issue 18

### 验收标准
- 可生成 `aligned_funding_rate`
- 可生成 `funding_source_ts`

---

## Issue 21
### 标题
Implement replay-ready Gold bar builder

### 目标
从 Silver 构建 Gold replay bars。

### 依赖
Issue 18, Issue 20

### 验收标准
- 至少一个 spot symbol/timeframe 可生成 Gold
- 至少一个 swap symbol/timeframe 可生成 Gold
- swap Gold bars 包含 funding 对齐字段

---

## Issue 22
### 标题
Register Gold build runs and outputs

### 目标
Gold build 也进入 run tracking。

### 依赖
Issue 21

### 验收标准
- gold_build run 可追踪
- run items 可见
- build 结果可关联 dataset version

---

## 10. 优先级与依赖关系

## P0
必须先做：
- Issue 1
- Issue 2
- Issue 3

## P1
然后做：
- Issue 4
- Issue 5
- Issue 6
- Issue 7
- Issue 8
- Issue 9

## P2
再做：
- Issue 10
- Issue 11
- Issue 12
- Issue 13

## P3
再做：
- Issue 14
- Issue 15
- Issue 16
- Issue 17
- Issue 18
- Issue 19

## P4
最后做：
- Issue 20
- Issue 21
- Issue 22

---

## 11. 建议的 Issue 模板

每个 issue 建议固定包含：

### 标题
一句话概括

### 背景
它属于哪个 milestone，解决什么问题

### 输入
依赖哪些表、文档、前置 issue

### 输出
新增哪些代码、哪些表行为、哪些运行结果

### 验收标准
至少 3 条可验证条件

### 非目标
明确本 issue 不负责什么

---

## 12. DoD（Definition of Done）

一个 issue 只有满足下面条件才算 Done：

1. 代码已提交
2. 至少一个真实样本跑通过
3. 对应表里能看到正确结果
4. run / quality / checkpoint 状态可查
5. 文档或注释已补齐最小说明

---

## 13. 风险点

### 风险 1：source 文件与 API 差异处理不一致
缓解：
- 所有 mapping 统一走已冻结的 field mapping 文档

### 风险 2：先写 collector，后补 schema
缓解：
- 所有 collector 必须依赖已冻结 schema 和 migration

### 风险 3：过早做 gap repair
缓解：
- 先让 rolling checkpoint 推进稳定，再做自动回补

### 风险 4：Gold 提前复杂化
缓解：
- Gold 只做 replay-ready，不做实验结果宽表

---

## 14. 最终交付状态

当本 backlog 完成后，Phase 1 应达到：

1. 独立 research database 已建成
2. candles / funding 历史文件可自动入仓
3. candles / funding API 增量可持续更新
4. checkpoint / quality / run tracking 可工作
5. Silver canonical truth 已建立
6. Gold replay bars 可生成

到这个状态，Phase 1 可以正式收口，并进入：

- Phase 2 研究与回放能力产品化
- Phase 3 live attribution
