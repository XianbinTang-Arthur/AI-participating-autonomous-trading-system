# P1-D Microstructure Phase 1A 实施设计 (2026-04-20)

> **状态**: W0 pre-work 调研产出 / 实施 runway 文档
> **Scope**: 为 W1-W2 Phase 1A agent 提供照表施工的实施方案
> **前置文档**:
> - `docs/design/p1d_microstructure_feasibility_2026_04_19.md`（1061 行,已批准 GO）
> - `docs/design/p1d_kickoff_decisions_2026_04_19.md`（Q1=VIP0, Q3=不发 NATS, Q4=BTC-only, Q5=Path B done, Q6=允许提前 NO-GO, Q8=允许切 5m）
> **调研边界**: 纯只读 + 设计, 不改生产代码 / 不 deploy / 不发 OKX API 请求
> **作者**: P1-D Phase 1A 实施设计 agent
> **交付给**: W1-W2 Phase 1A 实施 agent

---

## TL;DR — 关键建议

1. **新 collector 独立容器化**，命名 `aats-microstructure-collector`，照 `aats-liquidations-daemon` 成熟范式（Q3 决策 "不发 NATS, 只写 DB" 天然契合）。虽然 §1 实测 `aats-market` 有富余（稳态 5-8% CPU），**独立容器**换取的故障隔离 + 可独立 scale + 对主 snapshot 流零干扰，远胜于共享进程省下的那点儿容器启动成本。
2. **Bronze 不存 raw JSON payload 整包**，只存结构化字段 + 关键原始 id/ts，存储 30 天。每天 ~200MB trades + ~70MB orderbook 可接受（aats_research DB 当前 56MB,容量充足）。
3. **Silver ETL 用独立 scheduler，不复用 daily_ingest 的 cron**。microstructure 需要 **每 15 分钟**触发（与决策 tick 同步），daily_ingest 是每日 04:00 UTC 跑一次的 candles/funding 增量，时间尺度和依赖完全不同；混进去会让 cron schedule 互相干扰、失败诊断困难。
4. **3 张 Bronze 表用 natural-key UNIQUE + ON CONFLICT DO NOTHING** 做 DB 级幂等，复用 liquidations collector 的现成范式。
5. 监控方面，**microstructure metrics 走现有 MetricsRegistry → metrics_bridge → OTel Counter → Prometheus(:9464) → Grafana** 路径（已打通），不引入新组件。Path C Fix 3 的三个告警（fee_drift / cost_margin / BLOCKED）**同一批**接入，顺便关闭 Path C 收尾。

---

## § 1. aats-market 容器资源 baseline（实测 5 次采样）

### 1.1 采样数据

在项目同步稳定、应用 idle 8 分钟后采样（无人工触发事件）：

| 采样编号 | T+ 时刻 | CPU % | Mem | Net I/O cumulative | Block I/O | PIDs |
|---|---|---|---|---|---|---|
| #1 | 0 min | 5.37% | 131.9 MiB / 1.5 GiB (8.59%) | 66.0 MB / 43.1 MB | 0B / 81.9 kB | 27 |
| #2 | ~3 min | 7.59% | 132.2 MiB (8.61%) | 75.2 MB / 49.2 MB | 0B / 81.9 kB | 27 |
| #3 | ~5 min | 7.05% | 132.2 MiB (8.61%) | 77.1 MB / 50.6 MB | 0B / 81.9 kB | 27 |
| #4 | ~5.5 min | 5.17% | 132.3 MiB (8.61%) | 79.1 MB / 51.9 MB | 0B / 81.9 kB | 27 |
| #5 | ~6.5 min | 7.31% | 132.2 MiB (8.60%) | 81.8 MB / 53.6 MB | 0B / 81.9 kB | 27 |

**均值**: CPU ≈ **6.5% (1 core)**, Mem ≈ 132 MB / 1536 MB limit ≈ **8.6%**, Net in rate ≈ **39 KB/s**, Net out rate ≈ **27 KB/s**。

内存几乎零波动（全程 132.2 ± 0.4 MiB）—— aats-market 纯粹是 WS 消费 + normalizer + NATS publish，内存稳态极低。

### 1.2 对照组：aats-decision 同一时段（发现热点不在 market）

| 容器 | CPU | Mem | Net I/O |
|---|---|---|---|
| aats-decision | **85.10%** | 823.5 MiB / 1.5 GiB (53.61%) | 13.5 GB / 125 MB |
| aats-execution | 3.46% | 226.8 MiB (14.76%) | 2.66 GB / 154 MB |
| aats-gateway | 3.92% | 153.1 MiB (4.98%) | 83.1 MB / 5.76 MB |
| aats-liquidations-daemon | 0.02% | 58.3 MiB (11.39%) | 71.9 KB / 83.5 KB |
| aats-rdp-daemon | 0.00% | 55.2 MiB (3.59%) | 926 KB / 1.24 MB |

**判断**: **aats-decision 是整个系统的 CPU 热点**（85% 一核）。aats-market 远远不是瓶颈 —— 它有 93% CPU + 91% memory headroom。

### 1.3 Phase 1A 新增资源增量预估（依据可行性报告 §4.4）

| 资源 | 预估增量 | 对 aats-market 余量影响（若共享容器） |
|---|---|---|
| CPU (collector + WS parsing + buffer flush) | +10-15% 1 core peak, +4-8% 平均 | 总占用 15-25%，仍有富余 |
| Mem (buffer 500 rows + DB pool + normalizer state) | +40-80 MB | 总占用 170-210 MB / 1.5 GB，余量充足 |
| Net in (trades-all + bbo-tbt + books5) | +150-250 KB/s 平均, 峰值 ~500 KB/s | aats-market 当前 39 KB/s，总 190-290 KB/s，带宽无压力 |
| DB connection pool | +3-5 连接 | Postgres max_connections=200，当前使用 ≤50，足够 |
| DB write IOPS | +20-50 / s 平均（带缓冲），峰值 ~200 / s | Postgres aats_research 56 MB 空闲容量巨大 |

### 1.4 结论

> **aats-market 容器的资源完全可以承载 Phase 1 collector**，但我**建议仍走独立容器** `aats-microstructure-collector`，理由见 §2.3。

**可行性评分**: **GO (独立容器)** — 不是因为资源不足，而是运维与演进考虑。

---

## § 2. 现有 WebSocket 订阅框架盘点

### 2.1 关键文件 + 继承层次

```
aats/services/market_gateway/
├── okx_websocket.py
│   ├── class OKXWebSocketConsumerBase          ← 通用基类（跨进程复用）
│   │     │ N-connection asyncio.gather
│   │     │ reconnect + exponential backoff
│   │     │ 订阅 ack timeout 检测 (10s)
│   │     │ application-level keepalive (idle ping / pong timeout / market stale)
│   │     │ OKX control-plane 分类（subscribe / notice / error）
│   │     └─> 子类实现 _connection_specs()
│   │
│   └── class OKXPublicWebSocketClient(Base)    ← market 进程专用
│         双连接：public (tickers/mark/funding/OI) + business (candles)
│         按 allowed_symbols × 衍生品/现货 分流
│
├── okx_normalizer.py
│   └── class OKXMarketSnapshotNormalizer
│         apply_message() → MarketSnapshot
│         硬编码空: recent_trades=[] (L462)
│         硬编码 top-1: orderbook_depth = {bids:[[best_bid,bid_size]], asks:[[best_ask,ask_size]]} (L446-449)
│
└── gateway.py
    └── class MarketDataGateway (glue)
        _handle_okx_message → publish_snapshot → NATS market.snapshots
        REST fallback + gap backfill

aats/data_platform/collectors/
└── liquidations_ws_collector.py
    ├── class OKXLiquidationsWSClient(OKXWebSocketConsumerBase) ← 第二个复用实例
    │     单连接 public, 订阅 channel=liquidation-orders instType=SWAP
    │
    └── class LiquidationsCollector (glue)
        buffer (100 rows / 5s flush) → write_liquidation_batch()
        → INSERT ... ON CONFLICT DO NOTHING（DB-level 幂等）

scripts/
└── liquidations_ws_daemon.py
    ├── AATSSettings.model_validate({}) 绕过 profile 校验路径
    ├── signal handler (SIGTERM / SIGINT)
    ├── heartbeat file /tmp/aats_liquidations_heartbeat
    └── asyncio.run(amain(args))
```

### 2.2 订阅管理点（改动最小路径）

三种方案：

| 方案 | 改动范围 | Trade-off |
|---|---|---|
| A. 在 `OKXPublicWebSocketClient._subscription_args()` 里加 `books5`/`bbo-tbt`/`trades-all` | 改 1 文件 1 方法，共享现有 2 条连接 | 污染 market snapshot 流；数据流进同一 normalizer，要改 `OKXMarketSnapshotNormalizer`；**aats-market 进程内** buffer 300-500 msg/s 有可能挤占 NATS publish 延迟 |
| B. 在 `OKXPublicWebSocketClient` 里加第 3 条 connection 专跑 microstructure 频道 | 同进程多连接，normalizer 分流 | 仍在 aats-market 进程内，故障隔离弱 |
| C. **新 collector 类继承 OKXWebSocketConsumerBase**，独立容器 | 新增 1 文件 ~350 行 + 新 entrypoint 脚本 + compose 服务条目 | 完全复用基类框架（reconnect / keepalive / ack timeout），故障彻底隔离，完美契合 Q3 "不发 NATS 只写 DB" |

**我的判断**: 方案 C。

**三个主观理由**:

1. **Q3 决策天然指向独立容器** —— 既然"不发 NATS 只写 DB"，那么 collector 不需要共享 market 进程的 `MarketSnapshotPublisher`；寄生在同进程内反而是 contract violation，等于还能发 NATS 只是约定不发，引入幽灵依赖。
2. **liquidations 范式已验证** —— `aats-liquidations-daemon` 跑了 13h+，累积 9113 条数据，58 MB 稳定内存 / 0.02% CPU。零事故。照抄即可。
3. **Phase 2 演进路径清晰** —— Phase 2B 末尾可能扩到 ETH/SOL，到时再加 `books` 全档订阅或独立 symbol 分片，独立容器里随时可加，不污染主交易链路。

### 2.3 代码改动估算（方案 C 最终推荐）

| 改动 | 文件 | 行数估算 |
|---|---|---|
| 新 collector 主类 | `aats/data_platform/collectors/microstructure_ws_collector.py`（新文件） | ~350 行（对标 liquidations 272 行 + 3 channel parsing + 3 buffer/flush） |
| 新 daemon entrypoint | `scripts/microstructure_ws_daemon.py`（新文件） | ~160 行（对标 liquidations daemon 151 行 + 3 inst subscription args） |
| 新 compose 服务 | `deploy/wsl2-dev/docker-compose.aats.derivatives-live.yml` 追加 | ~35 行（对标 aats-liquidations-daemon 服务定义） |
| 新 migration SQL | `aats/data_platform/migrations/batch_b_05_microstructure.sql`（新文件） | ~180 行（8 张表 DDL + 索引） |
| Migration runner 追加 | `aats/data_platform/migrations/_batch_b.py` | +2 行（把 `batch_b_05_microstructure` 加入 `BATCH_B_STAGES` tuple） |
| ORM model 追加 | `aats/data_platform/rdp_models.py` | +约 200 行（8 张表的 ORM class） |
| Silver ETL 函数 | `aats/data_platform/merge/microstructure_silver_merger.py`（新文件） | ~450 行（5 个 build_silver_* 函数） |
| Silver ETL entrypoint | `scripts/rdp_build_microstructure_silver.py`（新文件） | ~180 行（15m scheduler + 5 ETL 调用） |
| 单元测试 | `tests/unit/data_platform/test_microstructure_*.py` | ~600 行（至少 15 场景） |
| 集成测试 | `tests/integration/test_microstructure_pipeline.py` | ~150 行 |
| **合计** | | **~2300 行新增 + 2 行修改** |

### 2.4 订阅隔离方案（彻底不污染现有 snapshot 流）

| 隔离边界 | 方案 | 验证点 |
|---|---|---|
| 进程 | `aats-microstructure-collector` 独立容器，独立 entrypoint | `docker ps` 看两个容器，相互不 depends_on |
| DB 连接池 | 新 collector 走 `ResearchPlatformSettings.RDP_DATABASE_URL`（同 aats_research 库，但独立 pool size 5） | `SELECT * FROM pg_stat_activity WHERE application_name = 'microstructure-daemon'` |
| NATS | **完全不连 NATS**（Q3 决策） | `docker exec aats-microstructure-collector netstat -an \| grep 4222` 应为空 |
| WS 连接 | 独立的 `OKXWebSocketConsumerBase` 实例，用同一个 public URL 但三个不同 inst-scoped 订阅 | OKX 日志字段 `connection=microstructure` 区分于 `connection=public/business` |
| OKX API rate limit | 新订阅 3 条（trades-all × 1 symbol, bbo-tbt × 1, books5 × 1） | 远低于 480/h 限额 |

---

## § 3. RDP Bronze/Silver 层现状盘点

### 3.1 现有数据层表清单（aats_research DB, 2026-04-19 实测）

```
DB size: 56 MB (极小)
```

| Schema | 表数 | 样例 |
|---|---|---|
| `meta` | 6 | dataset_manifests, ingest_runs, ingest_run_items, ingest_checkpoints, quality_reports, raw_source_files |
| `staging` | 10 | market_{swap,spot}_candles_{1m,5m,15m,1h} × 8 + market_swap_funding + **raw_liquidations** |
| `bronze` | 9 | market_{swap,spot}_candles_{1m,5m,15m,1h} × 8 + market_swap_funding |
| `silver` | 9 | 同 bronze 命名模式 |
| `gold` | 8 | market_{swap,spot}_replay_bars_{1m,5m,15m,1h} × 8 |
| `governance` | 19 | parameter_sets, recommendations, active_decisions, apply_saga_operations, rdp_daemon_heartbeat, system_config, ... |
| `research` | 3 | experiments, experiment_summaries, parameter_scan_runs |
| **合计** | **64** | |

### 3.2 命名规范（必须对齐）

**Candles/funding 规范**（工厂函数生成）：
- Staging: 列 `staging_row_id BIGSERIAL PRIMARY KEY + (symbol, ts)` 无 PK 约束（允许重复）
- Bronze: 列 `PRIMARY KEY (symbol, ts)` 严格去重
- Silver: 列 `PRIMARY KEY (symbol, ts)` + `dataset_version` 索引
- Gold: 同 Silver + `build_run_id` 索引

**公共字段模板**（所有 bronze/silver 表必带）：
```sql
ingest_run_id    UUID        NOT NULL
dataset_version  TEXT        NOT NULL
quality_flags    TEXT[]      NOT NULL DEFAULT '{}'::text[]
created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
```

**索引模板**:
```sql
idx_<pfx>_<inst>_<domain>_<tf>_<col>
-- pfx ∈ {stg, brz, slv, gld}
-- 例: idx_brz_swap_candles_15m_ts
```

### 3.3 Liquidations 风格（我们的主要参考）

```sql
-- staging.raw_liquidations 关键设计点：
-- 1. PK = BIGINT autoincrement (id), 不是 (symbol, ts) 组合
-- 2. Natural-key UNIQUE: (inst_id, ts, side, bk_px, sz)
-- 3. 原始整包 → raw_payload JSONB（但只存每行 detail，不存整个 OKX message）
-- 4. received_at 独立于 ts 以便诊断 WS 时差
```

### 3.4 Migration batch 编号惯例 + 下一个可用编号

现有 batch B 已用编号：
- `batch_b_01_core_schema.sql` (scope 列 + saga + heartbeat + system_config)
- `batch_b_02_profile_research.sql`
- `batch_b_03_cost_calibration.sql`
- `batch_b_04_sleeve_advice.sql`

**下一个可用编号**: **`batch_b_05_microstructure.sql`**。

同步更新：
- `batch_b_05_rollback.sql`（必须配套）
- `aats/data_platform/migrations/_batch_b.py` 的 `BATCH_B_STAGES` tuple 追加 `"batch_b_05_microstructure"`

### 3.5 索引策略判断

基于现有 bronze candles 的规范：
- 非 staging 层 PK = `(symbol, ts)` 已覆盖最常用查询
- 额外 `idx_brz_<tbl>_ts` 为 time-range 查询优化（cross-symbol）
- `idx_brz_<tbl>_run` 为 ingest_run_id 追溯

**新 microstructure bronze 表的索引加码**:
- **trades 表额外加 `idx_brz_market_trades_sym_trade_id`** — 因为 OKX `tradeId` 是 natural key，查重/溯源都靠它
- **orderbook 表 (symbol, ts) 组合索引已足够**，不加冗余 ts 索引（数据量大，多索引成本 15-25% 写入 IOPS）

### 3.6 Retention 策略（对齐 Path B）

Path B 已启用 14 天 retention。microstructure Bronze 层**独立**设置 30 天（因为 Phase 2A 回归需要 ≥3 周窗口）：

```sql
-- 由 scripts/rdp_microstructure_retention.py 定时清理
-- 不走 Path B 的 event_store 归档路径（Q6 明确两者独立）
DELETE FROM bronze.market_trades         WHERE ts < NOW() - INTERVAL '30 days';
DELETE FROM bronze.market_orderbook_bbo  WHERE ts < NOW() - INTERVAL '14 days';
DELETE FROM bronze.market_orderbook_books5 WHERE ts < NOW() - INTERVAL '14 days';
```

Silver 层保留 1 年（小，<100 MB/年）。

---

## § 4. Prometheus/Grafana 监控框架盘点

### 4.1 监控链路（已打通）

```
业务代码中调用:
  registry = container.registry()       # aats.bootstrap.metrics.MetricsRegistry
  registry.increment("metric_name")

↓ (30s 定时 sync)

aats/bootstrap/metrics_bridge.py
  MetricsRegistry.snapshot()
  → OTel Counter.add(delta)              # name = f"aats_{metric_name}"

↓

aats/bootstrap/telemetry.py
  PrometheusMetricReader (port 9464)

↓ (每 30s Prometheus scrape)

deploy/wsl2-dev/prometheus/prometheus.yml
  targets:
    - aats-gateway:9464
    - aats-market:9464
    - aats-decision:9464
    - aats-execution:9464
  scrape_interval: 30s
  storage retention: 48h

↓

aats-prometheus:9090

↓ (Grafana Prometheus datasource)

aats-grafana:3000
  provisioning/dashboards/files/AATS/*.json
  provisioning/alerting/rules.yml       # 当前仅用 Loki 做 SEV1-SEV3 日志告警
  provisioning/alerting/contactpoints.yml  # Stage 9 只写 Grafana log-only，Stage 10 才接 SMTP
```

### 4.2 现有命名规范（必须对齐）

实际在项目中 grep 到的规范：
- **Counter 类指标** 通过 MetricsRegistry 暴露，metrics_bridge 自动加 `aats_` 前缀
- 例: `registry.increment("decision_cycles")` → Prometheus 中名为 `aats_decision_cycles_total`
- Grafana 告警 rules.yml 用的是 **Loki logQL**（查 JSON log），**不是** PromQL 查 Prometheus counter

**结论**: 新 microstructure metrics 走 MetricsRegistry 路径，名字 **不加 `aats_` 前缀**（bridge 自动加）。

### 4.3 Phase 1A 新增 metrics 清单

所有 metrics 是 Counter（单调递增），用 delta 推到 OTel；Gauge 场景用 log 事件 + Loki 查询代替。

| metric name | 类型 | 含义 | 示例标签 |
|---|---|---|---|
| `microstructure_ws_connected` | Gauge (log-based) | WS 连接健康 | `channel={trades,bbo,books5}` |
| `microstructure_ws_connect_total` | Counter | 成功建立 WS 连接累计次数 | |
| `microstructure_ws_reconnect_total` | Counter | 断线重连累计次数 | |
| `microstructure_ws_messages_total` | Counter | 收到消息累计条数 | `channel=...` |
| `microstructure_ws_last_message_seconds_ago` | Gauge (log-based) | 最近一条消息的延迟（秒） | `channel=...` |
| `microstructure_bronze_rows_written_total` | Counter | Bronze 表写入行数累计 | `table={trades,bbo,books5}` |
| `microstructure_bronze_flush_total` | Counter | Bronze flush 批次累计 | `table=..., reason={max_rows,timeout,shutdown}` |
| `microstructure_bronze_flush_errors_total` | Counter | Bronze flush 失败累计 | `table=...` |
| `microstructure_silver_etl_runs_total` | Counter | Silver ETL 运行累计 | `table=..., status={success,failed}` |
| `microstructure_silver_etl_duration_seconds` | Histogram (log-based) | Silver ETL 耗时 | `table=...` |
| `microstructure_silver_rows_missing_bar_total` | Counter | Silver ETL 因 Bronze 无数据而空写累计 | `table=...` |

**告警规则**（Grafana rules.yml 追加，走 Loki pattern）：

```yaml
# SEV2: Microstructure collector 断连超过 2 分钟
- uid: sev2-micro-ws-down
  title: "SEV2: Microstructure WS Disconnected"
  expr: |
    sum(count_over_time({container="aats-microstructure-collector"}
        |= "microstructure_ws_disconnected" [2m]))
    >= 1

# SEV2: Bronze 15 分钟无新行写入
- uid: sev2-micro-bronze-stale
  title: "SEV2: Microstructure Bronze Stale"
  # 用 Grafana Loki+expression threshold，查 "bronze_row_inserted" 日志行
  # 15 分钟计数必须 > 500（trades 2M/day = ~21k/15min 远超）

# SEV3: Silver ETL 连续 2 次失败
- uid: sev3-micro-silver-etl-fail
  title: "SEV3: Silver ETL Repeated Failures"
  expr: |
    sum(count_over_time({job="aats"}
        |= "silver_etl_failed" [15m]))
    >= 2
```

### 4.4 Path C Fix 3 合并（顺便覆盖）

Path C Fix 3 需要的三个告警（来自 `docs/design/path_c_fix3_monitoring_alerts_followup_2026_04_19.md`）：

| 告警 | 事件 grep pattern | 严重度 |
|---|---|---|
| fee_drift_detected | `"fee_drift"` in 结构化 log | SEV2 |
| cost_margin_negative | `"cost_margin_negative"` | SEV2 |
| blocked_action_spike | `count("action=blocked") >= 20 in 15m` | SEV3 |

这三个**同一批**加入 `rules.yml`，不另开一个 PR。

---

## § 5. Silver 表详细 schema (5 张)

所有表统一规范：
- Schema = `silver`
- `PRIMARY KEY (symbol, ts)` 其中 ts = 15m bar 起点 (UTC aligned)
- 共用 `ingest_run_id / dataset_version / quality_flags / created_at / updated_at`
- `quality_flags` 允许值: `['etl_failed', 'partial_data', 'gap_filled_with_nulls', 'stale_source', 'whale_threshold_reinit']`

### 5.1 silver.market_orderbook_metrics_15m

```sql
CREATE TABLE IF NOT EXISTS silver.market_orderbook_metrics_15m (
    symbol                   TEXT         NOT NULL,
    ts                       TIMESTAMPTZ  NOT NULL,

    -- BBO level (from bbo-tbt sampled @ 1Hz into bronze)
    bbo_imbalance_mean       NUMERIC(12, 8),
    bbo_imbalance_std        NUMERIC(12, 8),
    bbo_imbalance_last       NUMERIC(12, 8),
    bbo_samples_n            INTEGER      NOT NULL DEFAULT 0,

    -- Top-5 level (from books5 sampled @ 2Hz into bronze)
    top5_bid_depth_ccy       NUMERIC(28, 10),
    top5_ask_depth_ccy       NUMERIC(28, 10),
    top5_imbalance_mean      NUMERIC(12, 8),
    top5_imbalance_ema       NUMERIC(12, 8),
    top5_weighted_imbalance  NUMERIC(12, 8),
    books5_samples_n         INTEGER      NOT NULL DEFAULT 0,

    -- Spread metrics
    spread_bps_mean          NUMERIC(12, 4),
    spread_bps_max           NUMERIC(12, 4),
    spread_bps_min           NUMERIC(12, 4),

    -- Mid anchor for downstream joins
    mid_price_last           NUMERIC(20, 10),

    -- 共用 footer
    ingest_run_id            UUID         NOT NULL,
    dataset_version          TEXT         NOT NULL,
    quality_flags            TEXT[]       NOT NULL DEFAULT '{}'::text[],
    created_at               TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ  NOT NULL DEFAULT now(),

    PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_slv_micro_orderbook_15m_ts
    ON silver.market_orderbook_metrics_15m (ts);
CREATE INDEX IF NOT EXISTS idx_slv_micro_orderbook_15m_ver
    ON silver.market_orderbook_metrics_15m (dataset_version);
```

**估算**: 16 列 × ~8-14 bytes = ~170 bytes/row × 96 row/day = 16 KB/day/symbol → 30d ~500 KB, 365d ~6 MB。可忽略。

**UPSERT 模板**:
```sql
INSERT INTO silver.market_orderbook_metrics_15m
    (symbol, ts, bbo_imbalance_mean, ..., ingest_run_id, dataset_version)
VALUES (...)
ON CONFLICT (symbol, ts) DO UPDATE SET
    bbo_imbalance_mean = EXCLUDED.bbo_imbalance_mean,
    ...
    updated_at = EXCLUDED.updated_at;
```

### 5.2 silver.market_trade_flow_15m

```sql
CREATE TABLE IF NOT EXISTS silver.market_trade_flow_15m (
    symbol                   TEXT         NOT NULL,
    ts                       TIMESTAMPTZ  NOT NULL,

    -- Volume (ccy = quote currency = USDT for BTC-USDT-SWAP)
    total_volume_ccy         NUMERIC(28, 10),
    buy_volume_ccy           NUMERIC(28, 10),
    sell_volume_ccy          NUMERIC(28, 10),
    trade_count              INTEGER      NOT NULL DEFAULT 0,

    -- Aggressor flow (taker = OKX side field 的语义)
    taker_buy_ratio          NUMERIC(12, 8),       -- buy_vol / (buy+sell)
    trade_flow_imbalance     NUMERIC(12, 8),       -- (buy - sell) / (buy + sell)
    log_tfi                  NUMERIC(12, 8),       -- log(buy/sell), clip ±5

    -- Size distribution
    mean_trade_size          NUMERIC(18, 8),
    p50_trade_size           NUMERIC(18, 8),
    p95_trade_size           NUMERIC(18, 8),
    p99_trade_size           NUMERIC(18, 8),
    max_trade_size           NUMERIC(18, 8),

    -- Whale detection (size > rolling_1h.p99 threshold)
    whale_threshold_applied  NUMERIC(18, 8),       -- 15m 窗口用的阈值（溯源）
    whale_count              INTEGER      NOT NULL DEFAULT 0,
    whale_buy_volume_ccy     NUMERIC(28, 10),
    whale_sell_volume_ccy    NUMERIC(28, 10),
    whale_direction          NUMERIC(12, 8),       -- (whale_buy - whale_sell) / total_whale_volume

    -- Aggressiveness
    vwap                     NUMERIC(20, 10),
    mid_price_ref            NUMERIC(20, 10),      -- bar close mid (from orderbook silver)
    vwap_minus_mid_bps       NUMERIC(12, 4),       -- +: taker buy 主导

    ingest_run_id            UUID         NOT NULL,
    dataset_version          TEXT         NOT NULL,
    quality_flags            TEXT[]       NOT NULL DEFAULT '{}'::text[],
    created_at               TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ  NOT NULL DEFAULT now(),

    PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_slv_micro_trade_flow_15m_ts
    ON silver.market_trade_flow_15m (ts);
CREATE INDEX IF NOT EXISTS idx_slv_micro_trade_flow_15m_ver
    ON silver.market_trade_flow_15m (dataset_version);
```

**估算**: 21 列 × ~8-14 bytes = ~220 bytes/row × 96 rows/day = 21 KB/day → 30d 640 KB, 365d 8 MB。

### 5.3 silver.market_oi_funding_metrics_15m

```sql
CREATE TABLE IF NOT EXISTS silver.market_oi_funding_metrics_15m (
    symbol                   TEXT         NOT NULL,
    ts                       TIMESTAMPTZ  NOT NULL,

    -- OI (from WS open-interest @ 3s, bucketed to 15m window)
    oi_open                  NUMERIC(28, 10),
    oi_close                 NUMERIC(28, 10),
    oi_high                  NUMERIC(28, 10),
    oi_low                   NUMERIC(28, 10),
    oi_delta                 NUMERIC(18, 10),       -- (close - open) / open
    oi_samples_n             INTEGER      NOT NULL DEFAULT 0,

    -- EMA-20 of 15m bars (rolling, source from self-previous rows)
    oi_ema_20                NUMERIC(28, 10),
    oi_delta_vs_ema          NUMERIC(18, 10),

    -- Price-OI joint regime
    price_change_bps         NUMERIC(12, 4),        -- 15m log-return * 10000
    oi_price_regime          TEXT,                  -- 'trend_long', 'trend_short', 'short_cover', 'long_cover', 'mixed', 'flat'

    -- Funding (from WS funding-rate, 1/min updates, last-value-wins per bar)
    funding_rate_current     NUMERIC(18, 12),
    funding_rate_next_est    NUMERIC(18, 12),
    funding_z_score_7d       NUMERIC(12, 6),        -- (cur - μ_7d) / σ_7d, rolling
    funding_deviation_30d    NUMERIC(18, 12),       -- |cur| - |median_30d|
    minutes_to_next_funding  INTEGER,               -- 0-480

    -- Mark / basis (from WS mark-price + orderbook silver's mid)
    mark_price               NUMERIC(20, 10),
    mid_price_ref            NUMERIC(20, 10),       -- from orderbook silver
    basis_bps                NUMERIC(12, 4),        -- (mark - mid) / mid * 10000

    ingest_run_id            UUID         NOT NULL,
    dataset_version          TEXT         NOT NULL,
    quality_flags            TEXT[]       NOT NULL DEFAULT '{}'::text[],
    created_at               TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ  NOT NULL DEFAULT now(),

    PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_slv_micro_oi_funding_15m_ts
    ON silver.market_oi_funding_metrics_15m (ts);
CREATE INDEX IF NOT EXISTS idx_slv_micro_oi_funding_15m_ver
    ON silver.market_oi_funding_metrics_15m (dataset_version);
```

**注意**: OI 和 funding 的 Bronze 源暂不独立建表（已在 market_gateway 订阅并走 `MarketSnapshot` 生命周期内存）。Phase 1A 采用 **aats-microstructure-collector 独立订阅** `open-interest` + `funding-rate` + `mark-price` **额外**一份，写入一个 **staging.market_oi_funding_ticks** 中间表（见 §6.4）。

**估算**: 22 列 × ~10 = ~220 bytes × 96/day = 21 KB/day → 30d ~640 KB。

### 5.4 silver.market_volume_profile_15m

```sql
CREATE TABLE IF NOT EXISTS silver.market_volume_profile_15m (
    symbol                   TEXT         NOT NULL,
    ts                       TIMESTAMPTZ  NOT NULL,

    -- 本 bar volume (来源: trade_flow silver 的 total_volume_ccy + trade_count)
    volume_ccy               NUMERIC(28, 10),
    trade_count              INTEGER      NOT NULL DEFAULT 0,

    -- Seasonal baseline (dow × hod × 15min slot) 4-week rolling
    expected_volume_ccy      NUMERIC(28, 10),       -- rolling mean
    expected_volume_std      NUMERIC(28, 10),       -- rolling stddev
    volume_z_score           NUMERIC(12, 6),
    volume_spike_flag        BOOLEAN      NOT NULL DEFAULT FALSE,
    dow_hod_slot             TEXT,                  -- 'mon_13:00' etc, 溯源 baseline key

    -- Interaction (与 TFI 交叉，用于 regression convenience)
    vol_weighted_tfi         NUMERIC(14, 8),

    -- Baseline cold-start diagnostic
    baseline_sample_weeks    INTEGER      NOT NULL DEFAULT 0,  -- 0-4，不足 4 时 z_score=NULL

    ingest_run_id            UUID         NOT NULL,
    dataset_version          TEXT         NOT NULL,
    quality_flags            TEXT[]       NOT NULL DEFAULT '{}'::text[],
    created_at               TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ  NOT NULL DEFAULT now(),

    PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_slv_micro_volume_profile_15m_ts
    ON silver.market_volume_profile_15m (ts);
CREATE INDEX IF NOT EXISTS idx_slv_micro_volume_profile_15m_ver
    ON silver.market_volume_profile_15m (dataset_version);
```

**估算**: ~150 bytes × 96/day = 14 KB/day → 30d ~430 KB。

**冷启动处理**: 首 4 周 `baseline_sample_weeks < 4`，`volume_z_score` 填 NULL，`quality_flags += 'partial_baseline'`。

### 5.5 silver.market_liquidation_metrics_15m

```sql
CREATE TABLE IF NOT EXISTS silver.market_liquidation_metrics_15m (
    symbol                   TEXT         NOT NULL,
    ts                       TIMESTAMPTZ  NOT NULL,

    -- Counts
    long_liq_count           INTEGER      NOT NULL DEFAULT 0,
    short_liq_count          INTEGER      NOT NULL DEFAULT 0,

    -- Notional (bk_px * sz, USD approximation for BTC-USDT-SWAP)
    long_liq_notional_usd    NUMERIC(28, 10),
    short_liq_notional_usd   NUMERIC(28, 10),
    liq_imbalance            NUMERIC(12, 8),        -- (long_usd - short_usd) / total_usd
    max_single_liq_usd       NUMERIC(28, 10),

    -- Cascade detection
    cascade_flag             BOOLEAN      NOT NULL DEFAULT FALSE,  -- count > cascade_threshold
    cascade_threshold_used   INTEGER,                              -- 溯源阈值
    intensity_z_7d           NUMERIC(12, 6),                       -- rolling z-score of notional sum

    ingest_run_id            UUID         NOT NULL,
    dataset_version          TEXT         NOT NULL,
    quality_flags            TEXT[]       NOT NULL DEFAULT '{}'::text[],
    created_at               TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ  NOT NULL DEFAULT now(),

    PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_slv_micro_liq_metrics_15m_ts
    ON silver.market_liquidation_metrics_15m (ts);
CREATE INDEX IF NOT EXISTS idx_slv_micro_liq_metrics_15m_ver
    ON silver.market_liquidation_metrics_15m (dataset_version);
```

**来源**: 读 `staging.raw_liquidations`（liquidations-daemon 已在采，Phase 1A **无需新订阅**）。

**估算**: ~150 bytes × 96/day = 14 KB/day → 30d ~430 KB。

### 5.6 Silver 层合计存储（单 symbol BTC-USDT-SWAP）

| 表 | 30 天 | 365 天 |
|---|---|---|
| orderbook_metrics_15m | ~500 KB | ~6 MB |
| trade_flow_15m | ~640 KB | ~8 MB |
| oi_funding_metrics_15m | ~640 KB | ~8 MB |
| volume_profile_15m | ~430 KB | ~5 MB |
| liquidation_metrics_15m | ~430 KB | ~5 MB |
| **合计** | **~2.6 MB** | **~32 MB** |

可忽略。

---

## § 6. Bronze 表详细 schema (3 张) + 1 个 staging tick 表

### 6.1 bronze.market_trades

**来源**: OKX `trades-all` WS 频道。

```sql
CREATE TABLE IF NOT EXISTS bronze.market_trades (
    symbol                   TEXT         NOT NULL,
    ts                       TIMESTAMPTZ  NOT NULL,                -- OKX trade.ts (ms → utc)
    trade_id                 TEXT         NOT NULL,                -- OKX tradeId, string
    px                       NUMERIC(20, 10) NOT NULL,
    sz                       NUMERIC(28, 10) NOT NULL,
    side                     TEXT         NOT NULL,                -- 'buy' or 'sell' (taker side per OKX)
    raw_payload              JSONB,                                 -- 仅保留 OKX detail，不含 arg

    ingest_run_id            UUID         NOT NULL,
    received_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),

    PRIMARY KEY (symbol, ts, trade_id),
    CONSTRAINT chk_brz_trades_side CHECK (side IN ('buy', 'sell'))
);
CREATE INDEX IF NOT EXISTS idx_brz_market_trades_ts
    ON bronze.market_trades (ts);
CREATE INDEX IF NOT EXISTS idx_brz_market_trades_sym_ts
    ON bronze.market_trades (symbol, ts);
-- trade_id 不加独立索引：PK 里已含，且 symbol 是强过滤
-- (symbol, ts) 索引支持 ETL 的 bar 窗口扫描（这是热路径）
```

**估算**:
- Row size ~110 bytes (incl JSONB overhead 40-60 bytes avg)
- BTC-USDT-SWAP 正常交易日 30-100 trades/s avg → ~2.6M/day upper bound
- **~300 MB/day/symbol raw**, 30 天 ~9 GB (compressed ~3 GB with Postgres TOAST)

**UPSERT 幂等**:
```sql
INSERT INTO bronze.market_trades (...) VALUES (...)
ON CONFLICT (symbol, ts, trade_id) DO NOTHING;
```
`tradeId` 是 OKX 全局唯一递增整数，重连重发 100% 命中 PK 去重。

### 6.2 bronze.market_orderbook_bbo

**来源**: OKX `bbo-tbt` WS 频道（10ms 推送），**客户端限流采样 1 Hz**（buffer 写 1 行/秒/symbol）。

```sql
CREATE TABLE IF NOT EXISTS bronze.market_orderbook_bbo (
    symbol                   TEXT         NOT NULL,
    ts                       TIMESTAMPTZ  NOT NULL,                -- 采样时刻（客户端）
    source_ts                TIMESTAMPTZ  NOT NULL,                -- OKX 推送原 ts
    bid_px                   NUMERIC(20, 10) NOT NULL,
    bid_sz                   NUMERIC(28, 10) NOT NULL,
    ask_px                   NUMERIC(20, 10) NOT NULL,
    ask_sz                   NUMERIC(28, 10) NOT NULL,
    -- 便利性计算字段（避免 Silver ETL 每次重算）
    mid                      NUMERIC(20, 10) GENERATED ALWAYS AS ((bid_px + ask_px) / 2) STORED,
    spread                   NUMERIC(20, 10) GENERATED ALWAYS AS (ask_px - bid_px) STORED,
    imbalance                NUMERIC(18, 10) GENERATED ALWAYS AS (
        CASE WHEN (bid_sz + ask_sz) > 0
             THEN (bid_sz - ask_sz) / (bid_sz + ask_sz)
             ELSE 0 END
    ) STORED,

    ingest_run_id            UUID         NOT NULL,
    received_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),

    PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_brz_market_orderbook_bbo_ts
    ON bronze.market_orderbook_bbo (ts);
```

**估算**:
- Row size ~130 bytes (7 NUMERIC + 3 generated stored)
- 1 Hz × 86400 s = 86400 rows/day → **~11 MB/day** (compressed ~5 MB)
- 14 天 ~170 MB, 30 天 ~340 MB

### 6.3 bronze.market_orderbook_books5

**来源**: OKX `books5` WS 频道（100ms 推送），**客户端限流采样 2 Hz** (500ms)。

```sql
CREATE TABLE IF NOT EXISTS bronze.market_orderbook_books5 (
    symbol                   TEXT         NOT NULL,
    ts                       TIMESTAMPTZ  NOT NULL,                -- 采样时刻
    source_ts                TIMESTAMPTZ  NOT NULL,                -- OKX 推送原 ts
    -- 5 个级别展平（避免 JSONB 解析成本）
    bid_px_1                 NUMERIC(20, 10) NOT NULL,
    bid_sz_1                 NUMERIC(28, 10) NOT NULL,
    bid_px_2                 NUMERIC(20, 10),
    bid_sz_2                 NUMERIC(28, 10),
    bid_px_3                 NUMERIC(20, 10),
    bid_sz_3                 NUMERIC(28, 10),
    bid_px_4                 NUMERIC(20, 10),
    bid_sz_4                 NUMERIC(28, 10),
    bid_px_5                 NUMERIC(20, 10),
    bid_sz_5                 NUMERIC(28, 10),
    ask_px_1                 NUMERIC(20, 10) NOT NULL,
    ask_sz_1                 NUMERIC(28, 10) NOT NULL,
    ask_px_2                 NUMERIC(20, 10),
    ask_sz_2                 NUMERIC(28, 10),
    ask_px_3                 NUMERIC(20, 10),
    ask_sz_3                 NUMERIC(28, 10),
    ask_px_4                 NUMERIC(20, 10),
    ask_sz_4                 NUMERIC(28, 10),
    ask_px_5                 NUMERIC(20, 10),
    ask_sz_5                 NUMERIC(28, 10),

    ingest_run_id            UUID         NOT NULL,
    received_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),

    PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_brz_market_orderbook_books5_ts
    ON bronze.market_orderbook_books5 (ts);
```

**估算**:
- Row size ~280 bytes (20 NUMERIC)
- 2 Hz × 86400 s = 172800 rows/day → **~48 MB/day** (compressed ~25 MB)
- 14 天 ~700 MB, 30 天 ~1.4 GB

### 6.4 staging.market_oi_funding_ticks (不是 bronze)

**为什么放 staging**: 和 `staging.raw_liquidations` 一样，这是每 tick 插入的原始流，Silver ETL 直接 group-by 聚合成 Silver，**不需要**独立 bronze 精简层。

```sql
CREATE TABLE IF NOT EXISTS staging.market_oi_funding_ticks (
    id                       BIGSERIAL    PRIMARY KEY,
    ts                       TIMESTAMPTZ  NOT NULL,                -- OKX 推送 ts
    symbol                   TEXT         NOT NULL,
    tick_type                TEXT         NOT NULL,                -- 'oi' | 'funding' | 'mark'
    oi                       NUMERIC(28, 10),                      -- when tick_type='oi'
    oi_ccy                   NUMERIC(28, 10),
    funding_rate             NUMERIC(18, 12),                      -- when tick_type='funding'
    next_funding_rate        NUMERIC(18, 12),
    next_funding_time        TIMESTAMPTZ,
    mark_px                  NUMERIC(20, 10),                      -- when tick_type='mark'

    received_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),

    CONSTRAINT chk_staging_oif_type CHECK (tick_type IN ('oi', 'funding', 'mark'))
);
CREATE INDEX IF NOT EXISTS ix_staging_market_oif_sym_ts
    ON staging.market_oi_funding_ticks (symbol, ts);
CREATE INDEX IF NOT EXISTS ix_staging_market_oif_type_ts
    ON staging.market_oi_funding_ticks (tick_type, ts);
-- BIGSERIAL id PK 避免 (ts, symbol, tick_type) 同一 ms 多 tick 造成冲突
```

**估算**:
- OI @ 3s + funding @ 1/min + mark @ 200ms-10s average
- ~20000 + 1440 + 300000 ticks/day = ~320k/day
- Row size ~95 bytes → **~30 MB/day**, 14 天 ~420 MB

### 6.5 Bronze/staging 合计（单 symbol BTC-USDT-SWAP，14 天）

| 表 | 14 天 | 30 天 |
|---|---|---|
| bronze.market_trades | ~4.2 GB | ~9 GB |
| bronze.market_orderbook_bbo | ~170 MB | ~340 MB |
| bronze.market_orderbook_books5 | ~700 MB | ~1.4 GB |
| staging.market_oi_funding_ticks | ~420 MB | ~900 MB |
| **合计** | **~5.5 GB** | **~11.6 GB** |

建议 retention：
- `market_trades`: **30 天**（回归需要）
- `market_orderbook_bbo`: **14 天**
- `market_orderbook_books5`: **14 天**
- `market_oi_funding_ticks`: **7 天**（Silver 已聚合完整信息）

### 6.6 批量写入模式（对齐 liquidations 现成范式）

```python
class MicrostructureBronzeBuffer:
    def __init__(self, table: str, flush_max_rows: int, flush_max_seconds: float):
        self._rows: list[dict] = []
        self._lock = asyncio.Lock()
        self._flush_max_rows = flush_max_rows
        self._flush_max_seconds = flush_max_seconds

    async def add(self, row: dict) -> bool:
        async with self._lock:
            self._rows.append(row)
            return len(self._rows) >= self._flush_max_rows

    async def flush(self) -> int:
        # Swap-and-release：锁里只做列表交换，DB I/O 在锁外
        async with self._lock:
            if not self._rows:
                return 0
            to_write, self._rows = self._rows, []
        with get_session() as session:
            result = session.execute(
                text(f"INSERT INTO {self._table} (...) VALUES (...) ON CONFLICT DO NOTHING"),
                to_write,
            )
            return result.rowcount or 0
```

**Flush 阈值**（按数据频率调优）：
- trades: `flush_max_rows=500, flush_max_seconds=3.0`
- bbo: `flush_max_rows=100, flush_max_seconds=5.0` (1 Hz，60s 内 60 行)
- books5: `flush_max_rows=200, flush_max_seconds=2.0` (2 Hz，60s 内 120 行)
- oi_funding_ticks: `flush_max_rows=100, flush_max_seconds=3.0`

---

## § 7. Silver ETL 函数设计

### 7.1 总入口

```python
# aats/data_platform/merge/microstructure_silver_merger.py

@dataclass(frozen=True, slots=True)
class SilverMicrostructureResult:
    symbol: str
    bar_start: datetime
    tables_written: dict[str, int]       # { 'orderbook_metrics_15m': 1, 'trade_flow_15m': 1, ... }
    quality_flags: list[str]             # 归一化 flag, 所有 5 张表共用
    duration_seconds: float
    error: str | None = None


async def build_silver_microstructure_15m(
    *,
    session: Session,
    symbol: str,
    bar_start_ts: datetime,                # bar 起点（UTC 对齐 15m boundary）
    bar_end_ts: datetime,                  # bar 终点 = bar_start + 15min
    ingest_run_id: str,
    dataset_version: str = "v1.0",
) -> SilverMicrostructureResult:
    """
    Silver 层 15m 聚合构建。幂等：同 (symbol, bar_start_ts) 可重复调用。

    实现策略：
    1. 5 个子函数各自构建 1 张表，一个 try/except 边界
    2. 任一表失败打 quality_flags=['etl_failed:{table}']，其他表仍尝试写
    3. 全部成功才标记 ingest_run.status='succeeded'
    """
    start = time.monotonic()
    flags: list[str] = []
    written: dict[str, int] = {}

    try:
        written['orderbook_metrics_15m'] = await _build_orderbook_metrics(
            session=session, symbol=symbol,
            bar_start=bar_start_ts, bar_end=bar_end_ts,
            ingest_run_id=ingest_run_id, dataset_version=dataset_version,
            flags=flags,
        )
    except Exception as exc:
        flags.append(f'etl_failed:orderbook_metrics')
        log.exception("orderbook_metrics build failed")

    try:
        written['trade_flow_15m'] = await _build_trade_flow(
            session=session, symbol=symbol,
            bar_start=bar_start_ts, bar_end=bar_end_ts,
            ingest_run_id=ingest_run_id, dataset_version=dataset_version,
            flags=flags,
        )
    except Exception as exc:
        flags.append(f'etl_failed:trade_flow')

    try:
        written['oi_funding_metrics_15m'] = await _build_oi_funding_metrics(...)
    except Exception as exc:
        flags.append(f'etl_failed:oi_funding_metrics')

    try:
        written['volume_profile_15m'] = await _build_volume_profile(...)
    except Exception as exc:
        flags.append(f'etl_failed:volume_profile')

    try:
        written['liquidation_metrics_15m'] = await _build_liquidation_metrics(...)
    except Exception as exc:
        flags.append(f'etl_failed:liquidation_metrics')

    return SilverMicrostructureResult(
        symbol=symbol,
        bar_start=bar_start_ts,
        tables_written=written,
        quality_flags=flags,
        duration_seconds=time.monotonic() - start,
    )
```

### 7.2 5 个 build_silver_* 函数

每个函数独立：
- 读对应 Bronze/staging 表 `WHERE symbol=X AND ts >= bar_start AND ts < bar_end`
- 计算聚合（SQL 内 aggregation 优先，复杂处理才 Python）
- UPSERT Silver 表（ON CONFLICT DO UPDATE）
- 返回行数（0 或 1，对 15m bar 语义）

示例（`_build_orderbook_metrics`）：

```python
async def _build_orderbook_metrics(
    *, session, symbol, bar_start, bar_end,
    ingest_run_id, dataset_version, flags,
) -> int:
    # 1. BBO 聚合 - 用 SQL window function
    bbo_row = session.execute(text("""
        SELECT
          AVG(imbalance)  AS imb_mean,
          STDDEV(imbalance) AS imb_std,
          (array_agg(imbalance ORDER BY ts DESC))[1] AS imb_last,
          COUNT(*) AS n,
          AVG(spread) FILTER (WHERE spread > 0) AS spread_mean,
          MAX(spread) AS spread_max,
          MIN(spread) FILTER (WHERE spread > 0) AS spread_min,
          (array_agg(mid ORDER BY ts DESC))[1] AS mid_last
        FROM bronze.market_orderbook_bbo
        WHERE symbol = :sym AND ts >= :start AND ts < :end
    """), dict(sym=symbol, start=bar_start, end=bar_end)).fetchone()

    if bbo_row.n == 0:
        flags.append('orderbook_bbo_no_data')

    # 2. Books5 聚合 - 用 SQL 按 level 分别求 sum
    books5_row = session.execute(text("""
        SELECT
          AVG(bid_sz_1 + COALESCE(bid_sz_2,0) + ... + COALESCE(bid_sz_5,0)) AS bid_depth,
          AVG(ask_sz_1 + ... + COALESCE(ask_sz_5,0)) AS ask_depth,
          AVG(...imbalance formula...) AS imb_mean,
          COUNT(*) AS n
        FROM bronze.market_orderbook_books5
        WHERE symbol = :sym AND ts >= :start AND ts < :end
    """), dict(sym=symbol, start=bar_start, end=bar_end)).fetchone()

    # 3. EMA 计算 - 读上一条 silver row 做 recursion (EMA @ 15m = prev * α + curr * (1-α))
    # 幂等保证：即便重跑，读的还是当前 silver 表上一行，结果一致

    # 4. UPSERT
    session.execute(text("""
        INSERT INTO silver.market_orderbook_metrics_15m
          (symbol, ts, bbo_imbalance_mean, ..., ingest_run_id, dataset_version, quality_flags)
        VALUES (...)
        ON CONFLICT (symbol, ts) DO UPDATE SET
          bbo_imbalance_mean = EXCLUDED.bbo_imbalance_mean, ...
    """), {...})
    return 1
```

### 7.3 调用点设计

**不复用 daily_ingest**，新 scheduler：

```python
# scripts/rdp_build_microstructure_silver.py

def main():
    """每 15 分钟跑一次的 scheduler 入口。

    通过 cron 触发:
        */15 * * * * cd /app && python scripts/rdp_build_microstructure_silver.py

    或者加入 RDP task_queue workflow 作为 'microstructure_silver_15m'，
    daemon 定时轮询执行。推荐方案是 task_queue（与其他 RDP workflow 统一）。
    """
    settings = ResearchPlatformSettings()
    now = utc_now()
    # 计算最近一个**已完成**的 15m bar（向前 1 个 bar 保证数据齐）
    bar_end = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    bar_start = bar_end - timedelta(minutes=15)
    # 更安全：再向前 1 bar，保证 bronze 数据全部到位
    bar_end = bar_start
    bar_start = bar_start - timedelta(minutes=15)

    run_id = create_ingest_run(...)
    try:
        with get_session() as session:
            result = asyncio.run(build_silver_microstructure_15m(
                session=session, symbol='BTC-USDT-SWAP',
                bar_start_ts=bar_start, bar_end_ts=bar_end,
                ingest_run_id=run_id,
            ))
            session.commit()
        finish_ingest_run(run_id, status='succeeded')
        log.info("silver etl ok: %s", result)
    except Exception as exc:
        finish_ingest_run(run_id, status='failed', error_message=str(exc))
        raise
```

### 7.4 幂等性 + 失败处理

**幂等保证**:
- bar_start_ts 对齐 15m boundary → 同一 bar 的输入 window 确定
- Bronze/staging 扫描是纯读取 → 无 side effect
- EMA 递归读 silver 表 **本身** → 若 silver 已有上一行，EMA 稳定；若没有（冷启动），第一行用 SMA-20 做 seed，quality_flags += 'ema_seed_from_sma'
- UPSERT `ON CONFLICT DO UPDATE` → 同 (symbol, ts) 多次跑结果一致

**失败处理**:
- 单张 Silver 表失败只打 flag，不回滚其他表（session per table）
- Scheduler 自己不重试；由 `governance.rdp_task_queue` 的 workflow `microstructure_silver_15m` 统一 retry（已有 Bug 6 机制：earliest_start_at + 15min）
- 连续 2 次失败触发 SEV3 告警（见 §4.3）

### 7.5 多 horizon scanning 的 hook

可行性报告 §5 要求 **Phase 2A 同时做 60s/5m/15m/1h 回归**。Phase 1A **只建 15m silver 表**，但要为 Phase 2A 留 hook：

```python
# bronze 表本身支持任意聚合窗口（按 ts query 即可）
# Phase 2A 需要的 60s/5m horizon 的函数，在 Phase 2A 独立建
# 设计 signature 保持一致:
async def build_silver_microstructure_Nm(..., bar_start_ts, bar_end_ts, ...)
# 只是 bar 宽度不同

# Phase 2A 新增的 silver 表命名:
#   silver.market_trade_flow_1m    (60s 桶)
#   silver.market_trade_flow_5m
# 保持同样的 UPSERT 模式
```

---

## § 8. 单元测试覆盖规划

至少 18 个场景（对齐可行性报告 §7 + kickoff Q6 的"提前 NO-GO 需要可验证结果"要求）。

**测试文件结构**:

```
tests/unit/data_platform/
├── test_microstructure_ws_client.py
├── test_microstructure_parse.py
├── test_microstructure_buffer.py
├── test_microstructure_bronze_write.py
├── test_microstructure_silver_orderbook.py
├── test_microstructure_silver_trade_flow.py
├── test_microstructure_silver_oi_funding.py
├── test_microstructure_silver_volume_profile.py
├── test_microstructure_silver_liquidation.py
└── test_microstructure_silver_pipeline.py

tests/integration/
└── test_microstructure_pipeline_e2e.py        # testcontainers Postgres
```

### 8.1 Collector 类测试（5 场景）

1. **test_ws_client_subscribe_args** — 构造 3 个 subscription args 正确（channel + instId 格式）
2. **test_ws_client_reconnect_on_stale** — mock server 30s 无推送 → 断开重连（复用 base 类的机制，验证 propagation）
3. **test_trades_message_parse_normal** — OKX 实际 trades-all payload 示例 → 正确解析出 LiquidationRow-like 结构
4. **test_trades_message_parse_malformed** — 缺 `tradeId` / 非 numeric `px` / 未知 `side` 单条 drop + warning，其他行继续
5. **test_bbo_message_sampling_1hz** — 连续 10 个 10ms 推送进来 → 只产生 1 个 bronze row（限流验证）

### 8.2 Bronze 写入测试（4 场景）

6. **test_bronze_trades_batch_insert_happy** — 500 行 batch → 500 行入库（测 UPSERT-DO-NOTHING 路径）
7. **test_bronze_trades_idempotent_on_conflict** — 重复 flush 同一 batch → rowcount = 0，第二次调用不报错
8. **test_bronze_bbo_generated_columns** — INSERT 一行 → mid / spread / imbalance 自动计算正确
9. **test_bronze_flush_sql_error_drops_batch** — mock session.execute raise → log.exception + 不阻塞下一个 batch

### 8.3 Silver ETL 测试（7 场景）

10. **test_silver_orderbook_happy** — 插入 60 行 bbo + 30 行 books5 → silver row 的 mean/std 等聚合字段 match 预期
11. **test_silver_orderbook_empty_bar** — Bronze 无数据 → silver row 仍写入（所有 metric=NULL + quality_flags=['orderbook_bbo_no_data']）
12. **test_silver_trade_flow_whale_detection** — 构造 trades 含 size > p99 的 → whale_count 正确
13. **test_silver_oi_funding_ema_cold_start** — silver 表无上一行 → ema 用 SMA seed + flag='ema_seed_from_sma'
14. **test_silver_volume_profile_baseline_cold_start** — 历史不足 4 周 → z_score=NULL + flag='partial_baseline'
15. **test_silver_liquidation_metrics_from_staging** — 直接读 staging.raw_liquidations（liquidations-daemon 已在采）→ silver row 正确
16. **test_silver_pipeline_idempotent** — 同一 (symbol, bar_start) 跑两次 → silver 表无重复 row (PK 幂等)

### 8.4 监控 metrics 测试（2 场景）

17. **test_metrics_microstructure_ws_connected_gauge** — 启动 collector mock → `metric_registry.snapshot()` 含 `microstructure_ws_connect_total > 0`
18. **test_metrics_bronze_flush_counter** — 1 次 flush N 行 → `microstructure_bronze_rows_written_total += N`

### 8.5 集成测试（testcontainers Postgres）

E2E: 启动 testcontainers Postgres → apply batch_b_05 migration → mock OKX WS server 推 synthetic payload → collector 写 bronze → scheduler 跑 silver ETL → assert silver 5 张表各有 1 行且字段合理。

---

## § 9. W1-W2 2 人周详细 WBS

目标精度：每个 deliverable 0.5-1 人天。

### W1 Day 1 — 监控 + Migration skeleton

- **目标交付物**:
  - `aats/data_platform/migrations/batch_b_05_microstructure.sql` — 8 张表 DDL + 索引（先只写骨架）
  - `aats/data_platform/migrations/batch_b_05_rollback.sql` — 逐张 DROP TABLE
  - `aats/data_platform/migrations/_batch_b.py` — `BATCH_B_STAGES` tuple append
  - `aats/data_platform/rdp_models.py` — 8 张表 ORM class
  - `deploy/wsl2-dev/grafana/provisioning/alerting/rules.yml` 追加 microstructure + Path C Fix 3 告警
- **验收标准**:
  - 本地 Postgres: `python scripts/rdp_init_db.py` 无错误，8 张表创建成功
  - `docker exec aats-postgres psql aats_research -c "\dt bronze.*"` 能看到 3 张新表
  - rollback SQL dry-run 通过
- **阻塞风险**:
  - migration 同 `batch_b_04_sleeve_advice` 冲突（不同 stage 不应冲突，但需 rebase）

### W1 Day 2 — OKX WS collector 主类

- **目标交付物**:
  - `aats/data_platform/collectors/microstructure_ws_collector.py` — 新文件 (~350 行)
    - `class MicrostructureWSClient(OKXWebSocketConsumerBase)` — 继承基类
    - `_connection_specs()` 返回 1 个 connection × 3 channel subscribe
    - `parse_trades_message(msg)` / `parse_bbo_message(msg)` / `parse_books5_message(msg)` / `parse_oi_funding_mark(msg)`
    - `class MicrostructureCollector` (glue)
    - 4 个独立 `MicrostructureBronzeBuffer` 实例 (trades/bbo/books5/oi_funding_ticks)
- **验收标准**:
  - 能 `from aats.data_platform.collectors.microstructure_ws_collector import MicrostructureCollector` 无 import 错误
  - 单元测试 1-5 全过
- **阻塞风险**: OKX `trades-all` 和 `bbo-tbt` 真实 payload schema 与文档微妙差异（Phase 1A 不发 OKX API，只能用 fixture 测；实际连线可能发现字段差异，在 W1 Day 4 的稳定性测暴露）

### W1 Day 3 — Daemon entrypoint + compose

- **目标交付物**:
  - `scripts/microstructure_ws_daemon.py` — 对标 `liquidations_ws_daemon.py` (~160 行)
  - `deploy/wsl2-dev/docker-compose.aats.derivatives-live.yml` 追加 `aats-microstructure-collector` service
    - 镜像 `aats-base:dev`（复用）
    - command `["python", "scripts/microstructure_ws_daemon.py"]`
    - healthcheck `/tmp/aats_microstructure_heartbeat` mtime < 60s
    - memory 512M limit (足够)
    - restart unless-stopped
  - `aats/bootstrap/settings.py` 若需新增配置（例如 `microstructure_symbols`）
- **验收标准**:
  - `bash scripts/deploy.sh --skip-commit --profile derivatives-live` 成功启动
  - `docker ps` 显示 `aats-microstructure-collector` healthy
  - `docker logs aats-microstructure-collector` 看到 `starting microstructure_ws_daemon (inst_ids=['BTC-USDT-SWAP'])`
- **阻塞风险**: first real WS subscription 可能因为 OKX 返回非预期 error code 被 reject，要在日志里看 `okx_ws_subscription_error`

### W1 Day 4 — Collector 单元测试 + 本地 24h 稳定性

- **目标交付物**:
  - `tests/unit/data_platform/test_microstructure_*.py` 场景 1-9 全部通过
  - 启动 `aats-microstructure-collector` 运行 ≥ 20 小时连续不 restart
  - 检查 Bronze 三张表 row count:
    - `bronze.market_trades`: ≥ 500000 rows (BTC 均 30 trades/s × 20h)
    - `bronze.market_orderbook_bbo`: ~72000 rows (1 Hz × 20h)
    - `bronze.market_orderbook_books5`: ~144000 rows (2 Hz × 20h)
- **验收标准**:
  - 单元测试 coverage 上升 ≥ 5% (collector 模块 ≥ 80% line coverage)
  - 20h 运行期内 `microstructure_ws_reconnect_total` ≤ 10（允许网络抖动但不频繁断线）
  - 容器内存稳定 < 250 MB（无泄漏）
- **阻塞风险**: 真实 WS 数据量可能超预期（如 BTC 极端行情时 200-500 trades/s），flush 跟不上 → buffer 内存膨胀 → OOM。Mitigation: buffer size 有 hard cap，超 5000 行直接 drop 并记 critical log。

### W1 Day 5 — Silver migration + ETL skeleton

- **目标交付物**:
  - `aats/data_platform/merge/microstructure_silver_merger.py` — 骨架 (~200 行)
    - 5 个 `_build_*` 函数 stub 返回 0
    - `build_silver_microstructure_15m()` 总入口逻辑
  - `scripts/rdp_build_microstructure_silver.py` — scheduler entrypoint (~180 行)
  - 单元测试 15-16（silver pipeline idempotent）先通过 mock stub
- **验收标准**:
  - 手动触发 `python scripts/rdp_build_microstructure_silver.py` 成功跑完
  - `silver.market_*_15m` 5 张表有 1 行（全 NULL 因为 stub 返回空）
- **阻塞风险**: 无

### W2 Day 1 — Silver ETL 5 个 build_* 实现（orderbook + trade_flow + volume_profile）

- **目标交付物**: 实现 `_build_orderbook_metrics`、`_build_trade_flow`、`_build_volume_profile`
  - 包括 seasonal baseline 冷启动逻辑
  - 包括 EMA 递归逻辑
- **验收标准**:
  - 单元测试 10-14 通过
  - 手动跑 scheduler：silver 前 3 张表关键字段 non-NULL
- **阻塞风险**: 
  - 4-week rolling baseline SQL 较复杂，容易出 timezone / DST-like 错误（UTC 不会有 DST，但需要严格 UTC 对齐验证）

### W2 Day 2 — Silver ETL 5 个 build_* 实现（oi_funding + liquidation）

- **目标交付物**: 实现 `_build_oi_funding_metrics`、`_build_liquidation_metrics`
- **验收标准**:
  - 单元测试 15-16 + 全部 silver 单元测试通过
  - 手动跑 scheduler：5 张 Silver 表全部 non-NULL 字段合理
- **阻塞风险**:
  - `staging.raw_liquidations` 的 inst_id 格式 vs Silver 的 symbol 映射（需要 `inst_id = symbol` 直接相等验证）

### W2 Day 3 — 集成测试 + E2E 数据流验证

- **目标交付物**:
  - `tests/integration/test_microstructure_pipeline_e2e.py`
  - WSL2 testcontainers 中跑通：
    - Postgres 启动
    - batch_b_01...05 全量 migration
    - Mock OKX WS server (用 `websockets.asyncio.server` 搭)
    - 发 N 条 synthetic message
    - Collector 写 Bronze
    - Scheduler 跑 Silver ETL
    - Assert 5 张 Silver 表各 1 行 + quality_flags 符合预期
- **验收标准**:
  - WSL2 `pytest tests/integration/test_microstructure_pipeline_e2e.py -x` 通过
  - 单元 + 集成测试合计 coverage ≥ 75%（microstructure 模块）
- **阻塞风险**: testcontainers 在 WSL2 的 mock WS server 端口绑定问题（需要在 WSL2 容器网络里，已有参考集成测试可借鉴）

### W2 Day 4 — 监控指标接线 + Grafana dashboard

- **目标交付物**:
  - 在 collector + scheduler 关键路径加 `registry.increment(...)` 调用（§4.3 全部 metrics）
  - `deploy/wsl2-dev/grafana/provisioning/dashboards/files/AATS/p1d_microstructure.json`
    - 4 个 panel:
      - WS 连接状态 + message rate
      - Bronze row count (每 15min)
      - Silver ETL duration + success rate
      - Storage growth (per table)
  - Grafana 告警 rules.yml 的 microstructure 3 条告警 + Path C Fix 3 的 3 条告警 生效
- **验收标准**:
  - Grafana UI `http://localhost:3000` 打开 dashboard 能看到数据
  - 手动触发断线 → SEV2 告警 fire
- **阻塞风险**: Loki label selector 需要精确（`job="aats"` 而非 `container="aats-microstructure-collector"`）—— Loki 只用 promtail job label

### W2 Day 5 — Buffer + code review + 上线 + 48h 稳定性验证

- **目标交付物**:
  - Buffer 时间：W1-W2 累积的技术债 / TODO 清理
  - Code review 修复（用户 review 本文档后指出的问题）
  - `bash scripts/deploy.sh --profile derivatives-live --commit "feat(p1d): Phase 1A microstructure collector + silver ETL"`
  - 48h 稳定性监控窗口启动
- **验收标准**:
  - Phase 1A 验收 Gate（见 §11）全部通过
  - 用户发起 W3 Phase 1B kickoff session
- **阻塞风险**: 48h 窗口内出现罕见 OKX 状态（如极端行情、交易所维护）导致 collector 失败 → 需要快速诊断

### WBS 总结

| Week | Day | 主题 | 主交付物 | 验收 |
|---|---|---|---|---|
| W1 | 1 | 监控 + Migration skeleton | SQL + rollback + ORM + Grafana rules | migration dry-run 过 |
| W1 | 2 | WS collector 主类 | microstructure_ws_collector.py | unit 1-5 过 |
| W1 | 3 | Daemon + compose | daemon + compose service | docker healthy |
| W1 | 4 | Collector unit + 20h 稳定性 | 单元测试 + 实跑 | 20h + 无泄漏 |
| W1 | 5 | Silver skeleton | merger 骨架 + scheduler | 手动跑通 |
| W2 | 1 | ETL 实现 (orderbook/trade_flow/volume) | 3 个 _build_ 函数 | unit 10-14 过 |
| W2 | 2 | ETL 实现 (oi_funding/liquidation) | 2 个 _build_ 函数 | unit 15-16 过 |
| W2 | 3 | 集成测试 E2E | testcontainers pipeline | WSL2 pytest 过 |
| W2 | 4 | 监控 + dashboard | metrics + grafana dashboard | Grafana UI 可见 |
| W2 | 5 | Buffer + 上线 + 48h 稳定性 | deploy + gate 验证 | §11 全 check |

---

## § 10. 风险清单（Phase 1A 特定）

可行性报告 §7 已列通用风险（OKX 稳定性 / 成本 vs edge / 过度工程 / regime drift / Path C 协同）。本节**只列 Phase 1A 实施特有的**：

### 10.1 OKX `books5` WS cluster 历史可用性

**数据点**: OKX 历史 status page（https://okxstatus.com）2023-2025 年公开记录了 3-5 次 public WS cluster 短暂中断（每次 15-120 min）。可行性报告 §7.1 已提及。

**Phase 1A 特定影响**: 24h 稳定性验收中若命中，会影响 Day 4 验收结果。

**Mitigation**:
- Day 4 接受 1 次 < 30min reconnect 事件
- `microstructure_ws_reconnect_total` < 10 放宽到 "无连续 reconnect loop > 5 min"

### 10.2 `aats-market` 现有连接数 vs OKX WS 频道限制

**OKX 限制**: 每 IP 3 个 public connections 并发。aats-market 现用 2 (public + business)。liquidations-daemon 用 1。新 microstructure collector 也 1 → **总 4 个 public connections 从同一个 WSL2 IP 发起**。

**风险**: 超过 3 个可能被 OKX reject 第 4 个 / 5 个。

**Mitigation**:
- **Phase 1A 的 microstructure collector 只开 1 个 public connection**（所有 3 个频道放一条连接，OKX 明确支持）
- 如果真的超限，OKX 会返回 error code 60004 (Too many connections) → `_last_error` 会捕捉 → SEV2 告警 → 运维介入

**补充调研**（调研中未能验证）: OKX 是否按**ip 段**还是**单 ip** 计算 connection limit，我没能在官方文档找到明确说明。Day 3 上线前建议 W1 agent 先 curl 一下 `/api/v5/market/ticker` 确认 aats-microstructure-collector 的出口 IP 与其他容器一致（应该都 NAT 到 WSL2 的同一 IP，所以风险真实存在）。

### 10.3 Bronze 表写入对 PostgreSQL 磁盘 IO 的瞬时压力

**数据点**: 当前 aats_research 56 MB, Postgres aats-postgres 容器 Block I/O 21 MB / 82 MB (read/write) 累计，运行 13h+ = **write rate ~1.75 KB/s**。

**Phase 1A 新增**: trades 30-100 rows/s @ flush_max=500 rows (3s 周期) → batched write ~4 KB per batch × 0.3/s = ~1.2 KB/s 追加。books5 类似。**总 write rate 翻到 ~5-10 KB/s**，相对 Postgres shared_buffers=768MB 毫无压力。

**Mitigation**:
- 必要时追加 `CREATE INDEX CONCURRENTLY` 避免建索引阻塞
- `log_min_duration_statement=500` 已启用，慢查询可见

### 10.4 Silver ETL cron 与现有 RDP daily_ingest 时间冲突

**现有 cron**: `0 4 * * *` 每日 04:00 UTC 跑 candles+funding daily_ingest。

**新增**: 每 15 分钟跑 microstructure silver ETL。**04:00, 04:15, 04:30, 04:45 有 4 次重叠**。

**Mitigation**:
- 两个 scheduler **不共享 ingest_run**（workflow 不同 → `rdp_task_queue.workflow` 唯一索引不冲突）
- daily_ingest 写 bronze/silver candles；microstructure silver ETL 写 silver.market_*_15m；**不同表无写锁冲突**
- Postgres 连接池 200 max，各自 10-20，富余足

### 10.5 对 event_store 归档（Path B）的潜在影响

**Path B 合同**: `event_store.strategy.baseline_assessment` 等 topic 扩 retention 到 14 天（当前已 merged 等累积）。

**P1-D 的 Bronze/Silver 层**: 完全独立于 `event_store`，**不写 event_store** → Path B 不受影响。

**反向确认**: 我在 Phase 1A scheduler 的 workflow 注册时**不要**写 `event_store.strategy.*` topic，仅用 RDP ingest_run tracking。

---

## § 11. Phase 1A 验收 Gate

硬指标（每条可编程 verification）：

- [ ] **连续 48h 无间断采集 BTC-USDT-SWAP 3 个频道**
  - Verify: `microstructure_ws_reconnect_total` 无连续 5 min 增长，`max(microstructure_ws_last_message_seconds_ago)` < 60s
- [ ] **Silver 表每 15min 有新 row**
  - Verify: `SELECT COUNT(*), MAX(ts) FROM silver.market_orderbook_metrics_15m WHERE symbol='BTC-USDT-SWAP' AND ts >= NOW() - INTERVAL '24 hours'` >= 96 行 且 `MAX(ts) >= NOW() - INTERVAL '30 min'`
- [ ] **Bronze `market_trades` 24h row count 符合预期**
  - Verify: `SELECT COUNT(*) FROM bronze.market_trades WHERE symbol='BTC-USDT-SWAP' AND ts >= NOW() - INTERVAL '24 hours'` 在 1,000,000 ~ 5,000,000 范围内（2.6M expected）
- [ ] **Bronze 三张表 quality_flags 无 'etl_failed' 标记**
  - Verify: `SELECT COUNT(*) FROM silver.market_orderbook_metrics_15m WHERE 'etl_failed' = ANY(quality_flags) AND ts >= NOW() - INTERVAL '24 hours'` = 0
- [ ] **Silver ETL 平均耗时 < 10s/run**
  - Verify: Grafana dashboard `microstructure_silver_etl_duration_seconds` p95 < 10s
- [ ] **新容器资源占用在预算内**
  - Verify: `aats-microstructure-collector` CPU < 30% 1 core (avg 48h), memory < 250 MB (peak 48h)
- [ ] **所有单元测试通过**
  - Verify: `pytest tests/unit/data_platform/test_microstructure_*.py -x -q` exit 0, >=18 passed
- [ ] **集成测试通过**
  - Verify: WSL2 `pytest tests/integration/test_microstructure_pipeline_e2e.py -x -q` exit 0
- [ ] **rollback SQL 可用**
  - Verify: staging 环境跑 `python -m aats.data_platform.migrations._batch_b rollback --stages batch_b_05_microstructure` 无错误
- [ ] **Grafana dashboard 可视化**
  - Verify: 打开 `http://localhost:3000/d/p1d-microstructure` 4 个 panel 都有数据且非空

---

## § 12. 疑问清单（Claude 无法自决）

1. **W1 Agent 是否独立 review 我的 §2.3 代码行数估算？** 如果 collector 类实际 > 500 行，可能说明需要拆 `_TradesParser` / `_OrderbookParser` / `_OIFundingParser` 子类，估算要调整。**建议用户让 W1 agent 先写 collector 主类骨架（Day 2 结束时）再 update WBS**。

2. **aats-microstructure-collector 容器是否需要和 aats-liquidations-daemon 合并为一个 "raw-ingest-daemon"？** 当前两个独立容器，各占 58 MB。合并后内存节省 ~50 MB，但故障域合并是反向 trade-off。**建议继续分开**，但这个判断可以由用户复核。

3. **Phase 1A 是否需要写入 `meta.ingest_runs` / `meta.ingest_run_items` 追溯？** 现有 `daily_ingest` 走这个路径，microstructure Silver ETL 也走 —— 这会给 `rdp_task_queue` 多一批 workflow 记录。**默认：跟 daily_ingest 保持一致，走 ingest_runs 路径**，但如果 15 分钟一次的 run 太噪声（5000+ /month），可能要改成只记 failed run。

4. **是否要给 Phase 2A 预留 silver `_1m` / `_5m` 表的 migration？** Phase 1A 只建 `_15m` 表；Phase 2A 决定是否做多 horizon 时再加。**默认：Phase 1A 不预留**，避免空表 noise。如果用户担心 Phase 2A 要额外一次 migration 烦，可以把 `batch_b_05` 拆成 `batch_b_05a_phase1_15m_only.sql` 和 `batch_b_05b_phase2_multi_horizon.sql`（前者 Phase 1A 跑，后者 Phase 2A 决定跑不跑）。

5. **bbo-tbt 的客户端 1Hz 采样是否足够？** 可行性报告 §4.1 建议 1s；我按这个写的。**但 OKX 原推送 10ms，理论上能做 100Hz**。采样率 10-100× 带来的存储膨胀 vs. 是否捕捉到毫秒级 OFI 信号的额外 R²，是 Phase 2 的优化方向。**默认 Phase 1 走 1Hz**，Phase 2A regression 后再评估是否要提升到 10 Hz。

6. **staging.market_oi_funding_ticks 和现有 aats-market 进程的 open-interest / funding-rate / mark-price 订阅是否重复？** 是的。aats-market 订的是供主交易链路即时用的；microstructure-collector 订的是供 RDP Silver 聚合用的。**两份独立订阅接受 redundancy**，因为 Q3 明确 "不发 NATS，只写 DB"，新 collector 不从 aats-market 读 NATS。另一方案是 aats-market 的 open-interest handler 多推一份到 DB，但那污染 aats-market 进程责任边界，不推荐。

7. **Silver ETL 在 rdp_task_queue workflow 下怎么命名？** 建议 `microstructure_silver_15m`，配合 `ix_rdp_task_one_active_per_workflow` 唯一索引自动串行化（同 workflow 同时只有 1 个 pending+running task）。用户如果有 naming convention 反馈请指出。

8. **Phase 1A 的 `microstructure_ws_daemon` 是否要和 `liquidations_ws_daemon` 合并为 `raw_ingest_ws_daemon`？** 从代码量 (2 × 150 行) 和未来演进（Phase 2B 可能加 ETH/SOL）看，现在分开没问题。**建议分开**。

---

## 附录 A. 关键文件路径清单（W1 Agent 照表施工）

**Phase 1A 要创建**:
- `aats/data_platform/migrations/batch_b_05_microstructure.sql`
- `aats/data_platform/migrations/batch_b_05_rollback.sql`
- `aats/data_platform/collectors/microstructure_ws_collector.py`
- `aats/data_platform/merge/microstructure_silver_merger.py`
- `scripts/microstructure_ws_daemon.py`
- `scripts/rdp_build_microstructure_silver.py`
- `tests/unit/data_platform/test_microstructure_ws_client.py`
- `tests/unit/data_platform/test_microstructure_parse.py`
- `tests/unit/data_platform/test_microstructure_buffer.py`
- `tests/unit/data_platform/test_microstructure_bronze_write.py`
- `tests/unit/data_platform/test_microstructure_silver_orderbook.py`
- `tests/unit/data_platform/test_microstructure_silver_trade_flow.py`
- `tests/unit/data_platform/test_microstructure_silver_oi_funding.py`
- `tests/unit/data_platform/test_microstructure_silver_volume_profile.py`
- `tests/unit/data_platform/test_microstructure_silver_liquidation.py`
- `tests/unit/data_platform/test_microstructure_silver_pipeline.py`
- `tests/integration/test_microstructure_pipeline_e2e.py`
- `deploy/wsl2-dev/grafana/provisioning/dashboards/files/AATS/p1d_microstructure.json`

**Phase 1A 要修改**:
- `aats/data_platform/migrations/_batch_b.py` (+2 行：`BATCH_B_STAGES` 追加)
- `aats/data_platform/rdp_models.py` (+约 200 行：8 张表 ORM class)
- `deploy/wsl2-dev/docker-compose.aats.derivatives-live.yml` (+ ~35 行 service 定义)
- `deploy/wsl2-dev/grafana/provisioning/alerting/rules.yml` (+ ~90 行：microstructure 3 条 + Path C Fix 3 3 条)

**Phase 1A 绝对不改**:
- `aats/services/market_gateway/*` (保持 market 进程不变)
- `aats/services/decision_engine/*`
- `aats/services/execution_engine/*`
- `aats/schemas/market.py` (MarketSnapshot schema 不动)
- `aats/bootstrap/settings.py` 核心字段（可追加新 config，但不改现有字段签名）

---

## 附录 B. 现有代码关键锚点

| 主题 | 文件 | 关键行/类 |
|---|---|---|
| OKX WS 基类 | `aats/services/market_gateway/okx_websocket.py` | `class OKXWebSocketConsumerBase` (L63-521) |
| OKX normalizer (空 recent_trades) | `aats/services/market_gateway/okx_normalizer.py` | `recent_trades=[]` L462, `orderbook_depth` top-1 L446-449 |
| 现有 market gateway | `aats/services/market_gateway/gateway.py` | `class MarketDataGateway` + `_handle_okx_message` L517 |
| Liquidations collector 范式 | `aats/data_platform/collectors/liquidations_ws_collector.py` | `class LiquidationsCollector` + `write_liquidation_batch` |
| Liquidations daemon 范式 | `scripts/liquidations_ws_daemon.py` | `amain()` signal handler + heartbeat file |
| Liquidations compose 服务 | `deploy/wsl2-dev/docker-compose.aats.derivatives-live.yml` | `aats-liquidations-daemon` L69-101 |
| Batch B migration runner | `aats/data_platform/migrations/_batch_b.py` | `BATCH_B_STAGES` tuple L33 |
| 现有 staging.raw_liquidations | `aats/data_platform/rdp_models.py` | `class RawLiquidationsModel` L431 |
| Candle/funding ORM 工厂 | `aats/data_platform/rdp_models.py` | `_make_candle_model` / `_make_funding_model` L268-374 |
| Silver merger 范式 | `aats/data_platform/merge/silver_merger.py` | `merge_candles_to_silver` / `merge_funding_to_silver` |
| Rolling API collector 范式 | `aats/data_platform/collectors/rolling/candles_api_collector.py` | `collect_candles_incremental()` |
| Daily ingest 入口 | `scripts/rdp_run_daily_ingest.py` | `main()` argparser |
| RDP task queue | `aats/data_platform/rdp_models.py` | `class RdpTaskQueueModel` L828, workflow 唯一索引 L834 |
| Ingest run tracking | `aats/data_platform/jobs/run_registry.py` | `create_ingest_run` / `finish_ingest_run` |
| Metrics registry | `aats/bootstrap/metrics.py` | `class MetricsRegistry` |
| Metrics bridge → Prometheus | `aats/bootstrap/metrics_bridge.py` | `create_bridge()` → OTel Counter |
| Telemetry 配置 | `aats/bootstrap/telemetry.py` | `class TelemetryConfig` + `configure_telemetry()` |
| Prometheus 采集 | `deploy/wsl2-dev/prometheus/prometheus.yml` | 4 个 target 9464 |
| Grafana alerting | `deploy/wsl2-dev/grafana/provisioning/alerting/rules.yml` | SEV1/2/3 格式模板 |
| Grafana dashboards | `deploy/wsl2-dev/grafana/provisioning/dashboards/files/AATS/*.json` | 现有 2 个 (operations, logs_overview) |
| Compose 共用 pattern | `deploy/wsl2-dev/docker-compose.aats.yml` | `x-aats-build` / `x-aats-common-env` YAML anchors |

---

## 附录 C. 我的三个主观判断

### C.1 "独立容器 > 共享 aats-market" 的力度比数据显示的更强

**表面数据**: aats-market CPU 5-8% 富余 93%，容器化本身的固定成本是 1 份 Python runtime ≈ 50 MB memory。

**更深层判断**: 就算 aats-market 富余 99% CPU，我仍推荐独立容器。原因：

1. **故障归因清晰性**: microstructure collector WS 断连 或 buffer flush 失败时，独立容器的 error log 隔离是 debug 效率的数量级差别。liquidations-daemon 的 13h+ 零事故也是因为容器级别隔离。
2. **Phase 演进成本**: Phase 2B 可能扩 ETH/SOL，Phase 3 可能接 VIP3 的 `books` 全档。共享 aats-market 的每一次加订阅都要在同一个进程的 normalizer 里加 branch，代码污染累积。独立容器就可以克隆多份或做 replica。
3. **生产安全文化**: AATS 是真金白银长期运营。数据采集 sidecar 挂了不应该影响主交易；共享进程违反这条核心原则。

### C.2 Bronze 三张表设计里 "trades PK 不含 trade_id" 是错的，我故意加了

可行性报告 §3.2 的 Bronze trades schema 写的是 `PRIMARY KEY (symbol, ts, trade_id)`。我在 §6.1 保留了这个 PK。**为什么不改成 `PRIMARY KEY (symbol, ts)` + `trade_id UNIQUE`**？因为：

- OKX 同一 ts 可能有多笔 trade（尤其 liquidation cascade 时高频）
- `trade_id` 是 OKX 主键，是 natural 去重，PK 必须含
- **选 `(symbol, ts, trade_id)` 作 PK 是设计良好**：组合索引天然加速 `WHERE symbol=X AND ts BETWEEN a AND b` 热路径，且防止重连期 OKX 重发同一 trade 重复入库

### C.3 Silver ETL 不走 daily_ingest 的 cron 是坚决的主张

看起来 daily_ingest 是 "同一个 RDP 数据采集系统的一部分"，但**时间尺度完全不兼容**：

| 维度 | daily_ingest | microstructure silver ETL |
|---|---|---|
| 触发 | 每日 04:00 UTC 一次 | 每 15 分钟一次（96 次/天） |
| 数据源 | OKX REST `history-candles` 拉回溯 | 本地 Bronze 表 (刚刚写入的) |
| 执行时长 | 分钟级 (4 symbol × 5 tf × 多页) | 秒级 (5 张表简单聚合) |
| 失败影响 | 一天数据推迟 | 一个 15m bar 缺（可补跑） |
| 重试策略 | daily 下次跑 | 下一个 15m bar 仍跑 (bar 独立) |
| 依赖 | OKX REST API 可用 | 本地 DB 可用 |

混进同一个 cron / workflow 会导致：
- daily_ingest 的 slow run 阻塞 microstructure 的 fresh bar
- microstructure 的高频写锁和 daily_ingest 的 large batch 争 IO
- 监控 dashboard 里两个概念的 failure rate 混合难以诊断

**所以独立 scheduler + 独立 workflow 名字是 correct separation of concerns**。

---

## 附录 D. 签署

- **调研日期**: 2026-04-19
- **前置证据**:
  - `docs/design/p1d_microstructure_feasibility_2026_04_19.md` (1061 行，已批准)
  - `docs/design/p1d_kickoff_decisions_2026_04_19.md` (136 行，已批准)
- **调研边界遵守**:
  - 未修改任何生产代码
  - 未 commit 任何 aats/**/*.py / configs/**/* / deploy/**/*
  - 未 deploy
  - 未发 OKX API 请求（仅 WebFetch OKX 公开文档范围）
  - 未读取 .env 凭证文件内容
  - DB 查询仅只读 + 小窗口 LIMIT
- **产出**: 本文件 `docs/design/p1d_phase1a_implementation_design_2026_04_20.md`
- **下一步**: 用户 review → approval → W1 agent 照此文档执行 Phase 1A 2 人周 WBS
- **疑问反馈**: §12 列出的 8 项建议用户在 kickoff W1 前确认

---

## 附录 E. §12 疑问决策确认（2026-04-19 晚间）

用户决策（原文）: "Q3 + Q4 都走 agent default。 全部 8 个疑问采纳 default，
W1 agent 直接照表施工，不再做任何决策。"

**8 项疑问最终决策（W1 agent 照此施工，不再请示）**:

| # | 疑问 | 最终决策 |
|---|---|---|
| 1 | W1 Agent 独立 review §2.3 代码行数估算？ | **接受**：W1 Day 2 结束时骨架完成后 update WBS |
| 2 | collector 与 liquidations-daemon 合并为 raw-ingest-daemon？ | **不合并**：保持独立容器 `aats-microstructure-collector` |
| 3 | Phase 1A 写入 `meta.ingest_runs` 追溯？ | **走 default**：跟 daily_ingest 保持一致，全量记 run；若未来 5000+/month 过噪声再改"只记 failed" |
| 4 | 预留 Phase 2A `_1m` / `_5m` 多 horizon 表 migration？ | **不预留**：Phase 2A 需要时加 `batch_b_06_microstructure_multi_horizon`；不做技术债预付 |
| 5 | bbo-tbt 客户端采样 1Hz vs 10Hz？ | **Phase 1 走 1Hz**；Phase 2A regression 后评估是否 10Hz |
| 6 | `staging.market_oi_funding_ticks` 与 aats-market 订阅重复？ | **接受 redundancy**：Q3 "不发 NATS" 的代价；优于污染 aats-market 责任边界 |
| 7 | Silver ETL workflow name？ | **`microstructure_silver_15m`**：配合 `ix_rdp_task_one_active_per_workflow` 唯一索引自动串行化 |
| 8 | `microstructure_ws_daemon` 与 `liquidations_ws_daemon` 合并？ | **不合并**：保持独立 daemon，便于 Phase 2B ETH/SOL 扩展 |

**签署**: Claude Opus 4.7（代表用户决策转达）· 2026-04-19

**W1 agent 启动协议**: 用户开启新 session 说 "启动 P1-D W1 Phase 1A" 时，
按本文档 §9 WBS 分阶段 spawn（建议每 2-3 day 一个 agent，不一次啃完 10 天
工作量以保证 code quality）。每阶段完成后用户 review + merge 后再启下一段。
