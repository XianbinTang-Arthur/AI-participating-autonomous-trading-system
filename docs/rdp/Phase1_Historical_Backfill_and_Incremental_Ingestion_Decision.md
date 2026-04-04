# Phase 1 历史补齐与增量采集的最终设计决策

## 1. 目标

Phase 1 的市场数据接入必须同时覆盖两类能力：

1. **历史补齐（Historical Backfill）**
2. **增量采集（Rolling Incremental Ingestion）**

二者都属于 Phase 1 的核心范围，缺一不可。  
平台必须既能补齐长历史，又能在历史补齐完成后持续更新近期数据，使研究、回放和后续 live 归因始终基于统一口径的数据仓库。

---

## 2. 总体原则

### 2.1 双通道模型
Phase 1 明确采用双通道采集架构：

- **历史补齐通道**：负责导入 OKX 官方历史下载文件
- **增量采集通道**：负责通过 OKX REST API 持续拉取最新数据

这两条通道共享同一套：
- PostgreSQL 主仓库
- 标准化规则
- 分层模型
- 质量检查机制
- canonical schema

但在调度方式、输入粒度、checkpoint 语义上分开设计。

---

## 3. 历史补齐（Historical Backfill）设计决策

### 3.1 数据来源
历史补齐的数据来源为 **OKX 官方 Historical Data 下载页**提供的历史文件。

### 3.2 输入粒度
OKX 官网历史下载当前以**日维度或月维度文件包**提供数据。  
因此，历史补齐必须按**文件批次导入模型**设计，而不是按“任意起止时间窗口回拉模型”设计。

### 3.3 调度原语
历史补齐任务的最小调度单元为：

- **source file**

例如：
- `BTC-USDT-SWAP-candlesticks-2026-04-01.zip`
- `ETH-USDT-SWAP-fundingrates-2026-03.zip`

而不是抽象时间窗。

### 3.4 入库流程
历史文件进入平台后的标准流程为：

`source_file -> staging -> bronze -> silver -> gold(可选)`

### 3.5 文件追踪要求
每一个历史文件都必须在 `meta.raw_source_files` 中登记，至少记录：

- 数据类型
- 交易品种
- 交易对
- 时间粒度（day/month）
- 覆盖起止日期
- 文件路径
- checksum
- 下载时间
- parse 状态
- ingest 状态

### 3.6 生命周期定位
历史补齐的职责是：
- 补齐长历史
- 建立基础数据资产
- 为后续 replay / 参数扫描提供完整样本窗

历史补齐不是持续运行任务，而是：
- 一次性
- 分阶段
- 可重跑
的 batch pipeline。

---

## 4. 增量采集（Rolling Incremental Ingestion）设计决策

### 4.1 数据来源
增量采集的数据来源为 **OKX 官方 REST API**。

### 4.2 职责
增量采集负责：
- 持续获取最新 candles
- 持续获取最新 funding
- 发现缺口
- 自动回补近期缺失窗口
- 保持仓库数据处于持续更新状态

### 4.3 运行方式
增量采集由 **Research Data Platform 独立服务进程内部的常驻 scheduler** 驱动，长期运行，不依赖人工触发。

### 4.4 Checkpoint 粒度
增量采集的 checkpoint / watermark 以如下维度维护：

- `dataset_domain`
- `instrument_type`
- `symbol`
- `timeframe`

其中 funding 因无 K 线周期，可允许 `timeframe = NULL`。

### 4.5 调度 cadence
Phase 1 的默认调度策略如下：

#### Candles
- `1m`：每 1 分钟
- `5m`：每 5 分钟
- `15m`：每 15 分钟
- `1H`：每 1 小时

#### Funding
- 每 15 分钟检查一次 funding 更新
- 以 funding timestamp 做幂等写入

### 4.6 缺口处理
增量采集必须支持：
- gap detection
- 自动回补
- gap 状态记录到 checkpoint 与 quality report

默认策略为：

> **自动回补，但以近期窗口优先。**

### 4.7 入库流程
增量 API 返回数据进入平台后的标准流程为：

`api_response -> staging -> bronze -> silver -> gold(可选)`

---

## 5. 历史补齐与增量采集的关系

### 5.1 架构关系
历史补齐与增量采集是两套独立 pipeline，但共用统一的数据仓库与 canonical schema。

### 5.2 优先级关系
当历史补齐与增量采集覆盖窗口发生重叠时：

> **增量采集优先。**

这意味着：
- 平台优先保证最新数据可用
- backfill 不得阻塞 rolling ingestion
- backfill 对近期热点窗口应尽量避让
- 最终通过 merge 规则收敛到统一 canonical 数据

### 5.3 写入策略
Phase 1 统一采用：

> **staging -> validate -> merge**

不允许外部采集数据直接写入 canonical target tables。

### 5.4 数据收敛规则
- Bronze：记录结构化来源数据，保留来源痕迹
- Silver：平台唯一 canonical truth
- Gold：面向 replay/研究的可重建层

历史文件源和 API 源最终都必须收敛到同一套 Silver 语义。

---

## 6. 调度与任务状态机

### 6.1 调度部署方式
Research Data Platform 作为**独立服务进程**运行，调度器内嵌于该独立服务，不挂靠交易平台进程。

### 6.2 任务状态机
Phase 1 引入最小任务状态机，至少支持：

- `pending`
- `running`
- `succeeded`
- `failed`
- `retrying`
- `backfilling`

### 6.3 任务分类
任务至少分为：

- `backfill`
- `rolling`
- `gap_repair`
- `gold_build`

### 6.4 任务追踪
所有任务必须写入：
- `meta.ingest_runs`
- `meta.ingest_run_items`

并记录：
- 输入来源
- 时间窗口或 source file
- 读取行数
- staging/bronze/silver/gold 写入行数
- 错误信息
- 任务状态

---

## 7. Phase 1 的明确范围

### 7.1 Phase 1 必须支持
#### 数据域
- Candles
- Funding

#### 品种范围
- `BTC-USDT`
- `ETH-USDT`
- `BTC-USDT-SWAP`
- `ETH-USDT-SWAP`

#### 时间周期
- `1m`
- `5m`
- `15m`
- `1H`

### 7.2 Phase 1 的目标状态
Phase 1 完成时，应达到：

1. 历史文件可以被平台批次导入
2. API 增量采集可以持续运行
3. 缺口可以自动发现并回补
4. 所有数据进入统一 PostgreSQL 主仓库
5. Silver 层成为后续 replay / 参数扫描 / 归因的唯一标准输入

---

## 8. Phase 1 的非目标

以下内容不属于本阶段完成标准：

- 完整的 live-vs-replay 差异归因
- trades / orderbook 全量接入
- execution realism 建模
- Web UI 数据平台
- Airflow 等外部调度系统接入

这些能力属于后续阶段。

---

## 9. 结论

Phase 1 的市场数据接入不采用单一路径，而采用：

> **历史文件批次补齐 + REST API 持续增量采集** 的双通道模型。

其中：

- 历史补齐按**日/月文件包**为单位导入
- 增量采集按 **dataset_domain + instrument_type + symbol + timeframe** 维度维护 checkpoint
- 两条通道统一进入 PostgreSQL 主仓库
- 统一使用 staging -> validate -> merge 写入模式
- 最终统一收敛到 Silver canonical schema

这一定义是 Phase 1 数据平台实现的基础约束，不应在后续实现中被随意修改。
