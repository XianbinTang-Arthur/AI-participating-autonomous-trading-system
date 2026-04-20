# P1-D OKX REST 批量回填方案设计 (2026-04-20)

> 项目定位声明: 本文件默认服从 AATS 的统一目标. 详见 [项目定位声明](../../docs/project_positioning.md).

**状态**: 纯设计 / 不执行 / 决策文档
**实施修正** (2026-04-20 Stage 5 实际执行): OKX 实测 OI history **只保留 60 天 (不是设计推断的 90 天)**;
LS ratio 5m period **只保留 2 天 (不是 30 天)**, 但 **1H period 可保留 30 天**(Stage 5 未拉, 本文档后已补).
Mark price endpoint 正确 URL 是 `/api/v5/market/history-mark-price-candles` (不是设计里的
`mark-price-candles-history`).
**Scope**: 用 OKX REST 批量回填 `open-interest history`、`trades history` 等 tick/bar 级历史到 RDP,
补齐 P1-D Phase 2A 回归所需的特征基线
**边界**: **不发任何 OKX API 请求; 不改生产代码; 仅读公开文档 + 已有 collector 代码**
**作者**: P1-D 预览 agent
**前置**:
- `docs/design/p1d_microstructure_feasibility_2026_04_19.md` §2.4 (历史数据 retention)
- `aats/data_platform/collectors/rolling/candles_api_collector.py` (现有 REST 分页 pattern)
- `aats/data_platform/collectors/backfill/candles_backfill_collector.py` (现有 backfill pattern)
- `scripts/rdp_deep_backfill_api.py` / `rdp_deep_backfill_funding.py` (深度回填脚本)

---

## TL;DR — 决策建议

| 数据源 | 可用性 | 建议 |
|---|---|---|
| **Candles (history-candles)** | 可用 | **已实现**, `scripts/rdp_deep_backfill_api.py` 可拉数年数据 |
| **Funding rate (funding-rate-history)** | 可用 | **已实现**, `rdp_deep_backfill_funding.py`, 见现有 3 月数据 |
| **Open-interest history (`/api/v5/rubik/stat/contracts/open-interest-history`)** | **可用**, 粒度 5m/15m/30m/1H/2H/4H, 100 条/页. **实测 OKX 实际只给 60 天**(不是 90, 设计高估) | **新建 backfill collector**, 补 60 天 |
| **Mark-price history (`/api/v5/market/mark-price-candles-history`)** | 可用 (candle-like) | **新建 collector**, 补 30-60 天 1m mark candles |
| **Trades history (`/api/v5/market/history-trades`)** | **受限**, `after`/`before` 按 tradeId 而非 ts, 每页 100, **无官方时间范围 query** | **不推荐大规模回填**; 对历史 trades **建议买 Tardis.dev / Kaiko** |
| **Long-short ratio (`/api/v5/rubik/stat/contracts/long-short-account-ratio`)** | 可用 | 现已 5min 实时 poll, 历史数据用 begin/end 可回填 |

**Go / No-Go 建议**:

- **GO**: 做 **OI history** 和 **mark-price history** 的 backfill collector, 成本低, rows 可控
- **CONDITIONAL**: **long-short-ratio history** 值得补, 但需要确认 begin/end pagination 可用;
  现有 `LongShortRatioPoller` 只取 latest, 回填需独立 backfill 脚本
- **NO-GO (自建)**: **Trades history**. OKX `history-trades` 的 pagination 机制不是 ts-based,
  实际上只能从 "最近 tradeId" 反向遍历, 回填 30-60 天 ≈ 120M-240M rows, 以 OKX public rate
  limit **20 req / 2s / IP** 节流, 单 IP 单 symbol 需要 ~200 小时不间断请求. 而 Tardis.dev
  历史 trades $300-600 一次性, Kaiko 类似价位 — **性价比明显胜出**.

**推荐路径**:

1. **Phase 1 (本周)**: 实现 OI history backfill (60-90 天, 1H period) → 2 人日
2. **Phase 1 (本周)**: 实现 mark-price history backfill (30 天, 1m period) → 1 人日
3. **Phase 1 末**: 评估 long-short-ratio history 回填可行性 → 0.5 人日
4. **Phase 2 前**: 若真需要 trades history, **买 Tardis.dev 一次性 $300-600** 搞定, 不自建爬虫

---

## § 1. OKX API 可用性分析

### 1.1 覆盖矩阵

| 数据源 | endpoint | 粒度选项 | 支持 begin/end | 每页 limit | 历史最远 | VIP 要求 | 认证 |
|---|---|---|---|---|---|---|---|
| Candles history | `/api/v5/market/history-candles` | 1m/5m/15m/30m/1H/4H/1D | after/before by ts(ms) | 100 | **多年** (实测 4 年+) | 无 | 公开 |
| Funding rate history | `/api/v5/public/funding-rate-history` | 8h native (每期 1 行) | before/after by ts(ms) | 100 | **多年** | 无 | 公开 |
| OI history | `/api/v5/rubik/stat/contracts/open-interest-history` | 5m/15m/30m/1H/2H/4H | begin/end by ts(ms) | **100** | 待实测, 推断**≥ 90 天** | 无 | 公开 |
| Mark-price candles history | `/api/v5/market/mark-price-candles-history` | 1m/3m/5m/15m/30m/1H/2H/4H/6H/12H/1D | after/before by ts(ms) | 100 | **~30 天** (推断) | 无 | 公开 |
| Trades history | `/api/v5/market/history-trades` | 逐笔 | **after/before by tradeId** (不是 ts!) | 100 | **未公开明确上限**, 社群反馈短 | 无 | 公开 |
| Liquidations | `/api/v5/public/liquidation-orders` | 事件 | before/after | 100 | **仅 7 天** (P1-D §2.2 验证) | 无 | 公开 |
| Long-short ratio | `/api/v5/rubik/stat/contracts/long-short-account-ratio` | 5m/1H | begin/end by ts(ms) | 100 | 待实测, 推断 **≥ 30 天** | 无 | 公开 |
| Taker volume | `/api/v5/rubik/stat/taker-volume` | 5m/1H | begin/end | 100 | 待实测 | 无 | 公开 |

**重要观察**:

- **history-candles / funding-rate-history / OI history / rubik-stat**: pagination 是 **ts-based**,
  支持无缝时间范围回填.
- **history-trades**: pagination 是 **tradeId-based**, 每次只能基于前一页的 tradeId 往前/后走.
  这意味着**没有"直接跳到某个时间"的能力**, 只能从当前最近 trade 一路连续翻 N 页.
- **liquidation-orders**: OKX 明确只保留 7 天, 已用 WS collector 实时采 (`liquidations_ws_collector.py`).

### 1.2 Rate limits (OKX v5 公开文档)

| 类别 | limit |
|---|---|
| Public non-WS (candles/funding/OI/stats) | **20 req / 2s / IP** (10 req/s avg) |
| 带 instId 的 non-stats public | 可能稍紧 (需要 backoff 策略) |
| 429 处理 | 指数 backoff 1s → 2s → 4s → 8s → 30s cap; 连 3 个 429 触发 `--rate-limit-sleep` 翻倍 |
| IP ban | 持续超限可能触发临时 block (30s-5min) |

### 1.3 已经就绪的 pattern 复用

AATS 现有:
- `scripts/rdp_deep_backfill_api.py` — candles 深度回填 (已可拉 300 pages × 100 bars = 30K bars = ~312 天 15m)
- `scripts/rdp_deep_backfill_funding.py` — funding 深度回填 (默认 rate_limit 0.15s/req = 6.7 req/s)
- `aats/data_platform/collectors/rolling/funding_api_collector.py` — checkpoint + run_registry 幂等模板
- `aats/data_platform/jobs/checkpoint_manager.py` — 通用 checkpoint 表 (resume 用)
- `aats/data_platform/jobs/run_registry.py` — 通用 ingest_run + run_item 状态机

**结论**: 本次设计**只需要扩展这些 pattern 到新 endpoint**, 不是从零搭.

---

## § 2. 目标与 scope 设定

### 2.1 我们真正需要什么

按 P1-D §5.1 + 本预览回归 §3 findings 反推:

| 特征 | horizon | 数据需求 |
|---|---|---|
| OI delta + 价量联合 | 15m | 60-90 天 × 1H OI 即可 (本身低频) |
| Mark price vs mid basis | 15m | 30 天 × 1m mark candles |
| Long-short ratio 极值事件 | 15m | 30 天 × 5m ratio |
| Trade flow aggression (taker buy/sell) | 15m | **60 天逐笔 trades** — 上百 M rows |
| Liquidation cascade | 15m | 已有 WS 实时采 |

**优先级**:
- P0: OI history (最容易, 预期 R² > basis/funding, 见 P1-D §5.1 表)
- P0: Mark-price (做 basis 的必备, 现 `basis_z` 我们已经用 spot close 代替, 但 **真正的 basis 是 perp - mark**, spot-perp 是跨交易所风险)
- P1: Long-short history
- P2: Trades history — **买不自建**

### 2.2 量级估算 (BTC-USDT-SWAP 单 symbol)

| 数据源 | row 估算 | bytes/row | 总量 | 下载时长 |
|---|---|---|---|---|
| OI 1H × 90 天 | 2160 rows | ~120 B | ~260 KB | <1 min |
| OI 15m × 60 天 | 5760 rows | ~120 B | ~700 KB | <1 min |
| Mark 1m × 30 天 | 43200 rows | ~150 B | ~6.5 MB | ~2 min |
| Long-short 5m × 30 天 | 8640 rows | ~80 B | ~700 KB | <1 min |
| Trades 60 天 (假设 30 msg/s avg) | **~155M rows** | ~80 B | **~12 GB** | **~200 h @ 20 req/2s** |

**结论**: 前三个加起来 **~10 MB / 5 分钟下载**, 是无痛的; trades 是 12 GB / 8+ 天下载, **不自建**.

---

## § 3. 各 endpoint 设计 pseudo-code

**注**: 以下都参考 `scripts/rdp_deep_backfill_api.py` 骨架,
同样走 staging → bronze → silver 三层.

### 3.1 OI history backfill collector

```python
# aats/data_platform/collectors/backfill/oi_history_backfill_collector.py

API_PATH = "/api/v5/rubik/stat/contracts/open-interest-history"
API_LIMIT = 100

def collect_oi_history(
    session: Session, settings: ResearchPlatformSettings,
    symbol: str, period: str = "1H",
    target_days: int = 90,
    rate_limit_sleep: float = 0.15,
    max_pages: int = 300,
) -> str:
    """拉 OI history 回填到 bronze.market_oi_history.
    
    pagination: begin/end 都是 ts(ms). OKX 返回 newest-first.
    策略: 先查 bronze.market_oi_history 里已有最早 ts,
    然后从那往更早方向分页 (end=最早_ts - 1).
    """
    inst_type = instrument_type_for_symbol(symbol)
    run_id = create_ingest_run(session, run_type="backfill", 
                               dataset_domain="oi_history", ...)
    
    earliest_existing = query_earliest_ts(session, symbol)  # already in DB
    target_earliest = utc_now() - timedelta(days=target_days)
    cursor_end_ms = _ts_ms(earliest_existing) if earliest_existing else _ts_ms(utc_now())
    
    all_rows: list[dict] = []
    with httpx.Client() as client:
        for page in range(max_pages):
            params = {
                "instId": symbol,
                "period": period,
                "end": str(cursor_end_ms),  # "earlier than cursor"
                "limit": str(API_LIMIT),
            }
            resp = client.get(f"{settings.okx_rest_url}{API_PATH}", 
                              params=params, timeout=settings.okx_timeout_seconds)
            if resp.status_code == 429:
                # exponential backoff
                time.sleep(min(30.0, rate_limit_sleep * 2**page))
                continue
            resp.raise_for_status()
            body = resp.json()
            if body.get("code") != "0":
                raise RuntimeError(f"OKX error: {body}")
            data = body.get("data", [])
            if not data:
                break
            # each row: [ts, oi, oiCcy] (OKX schema; verify on first request)
            for item in data:
                all_rows.append({
                    "ts": _ms_to_dt(int(item[0])),
                    "oi": Decimal(item[1]),
                    "oi_ccy": Decimal(item[2]) if len(item) > 2 else None,
                })
            oldest_ts_ms = min(int(d[0]) for d in data)
            if oldest_ts_ms <= _ts_ms(target_earliest):
                break
            cursor_end_ms = oldest_ts_ms - 1
            time.sleep(rate_limit_sleep)
    
    write_bronze_oi_history(session, symbol, period, all_rows, run_id)
    finish_ingest_run(session, run_id, status="succeeded")
    return run_id
```

**新 migration 需要**:

```sql
-- batch_b_08_oi_history_bronze.sql (新增, 不碰 staging.market_oi_funding_ticks)
CREATE TABLE IF NOT EXISTS bronze.market_oi_history (
    symbol       TEXT NOT NULL,
    ts           TIMESTAMPTZ NOT NULL,
    period       TEXT NOT NULL,  -- '5m' / '15m' / '1H' / ...
    oi           NUMERIC(28, 10) NOT NULL,
    oi_ccy       NUMERIC(28, 10),
    ingest_run_id UUID NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, ts, period)
);
CREATE INDEX IF NOT EXISTS ix_brz_oi_history_sym_ts ON bronze.market_oi_history(symbol, ts);
```

**下游 silver 聚合**: 如用 1H period 回填, 15m bar 可直接 forward-fill 到最近 1H 点;
或单独实现 silver.market_oi_history_15m 聚合 (aggregation 同 `microstructure_silver_merger` 套路).

### 3.2 Mark-price history backfill

```python
# aats/data_platform/collectors/backfill/mark_price_history_backfill_collector.py

API_PATH = "/api/v5/market/mark-price-candles-history"

def collect_mark_price_history(
    session: Session, settings: ResearchPlatformSettings,
    symbol: str, period: str = "1m",
    target_days: int = 30,
    rate_limit_sleep: float = 0.15,
):
    """Mark price candles 历史回填.
    
    pagination 同 history-candles: after=ts_ms (earlier-than), before=ts_ms (newer-than).
    OKX 返回 newest-first.
    """
    # ... (完全相同的 pattern as rdp_deep_backfill_api.py 但换 endpoint 和表)
```

**新 bronze 表**:

```sql
CREATE TABLE IF NOT EXISTS bronze.market_mark_price_history (
    symbol     TEXT NOT NULL,
    ts         TIMESTAMPTZ NOT NULL,
    period     TEXT NOT NULL,
    mark_open  NUMERIC(20, 10),
    mark_high  NUMERIC(20, 10),
    mark_low   NUMERIC(20, 10),
    mark_close NUMERIC(20, 10) NOT NULL,
    confirm    BOOLEAN,
    ingest_run_id UUID NOT NULL,
    PRIMARY KEY (symbol, ts, period)
);
```

### 3.3 Long-short ratio history backfill

```python
# aats/data_platform/collectors/backfill/long_short_ratio_backfill.py

API_PATH = "/api/v5/rubik/stat/contracts/long-short-account-ratio"

def collect_ls_ratio_history(
    session: Session, settings: ResearchPlatformSettings,
    ccy: str = "BTC",  # 注意: rubik 端点用 ccy 不是 instId
    period: str = "5m",
    target_days: int = 30,
    rate_limit_sleep: float = 0.15,
):
    """Long-short account ratio 历史回填.
    
    参数: ccy (非 instId!), period (5m/1H), begin/end by ts(ms).
    """
    run_id = ...
    end_ms = _ts_ms(utc_now())
    all_rows = []
    with httpx.Client() as client:
        for page in range(max_pages):
            params = {"ccy": ccy, "period": period,
                      "end": str(end_ms), "limit": "100"}
            resp = client.get(f"{settings.okx_rest_url}{API_PATH}", params=params)
            # ...
            if oldest_in_page <= _ts_ms(target_earliest):
                break
            end_ms = oldest_in_page - 1
            time.sleep(rate_limit_sleep)
    # write bronze.market_ls_ratio_history
```

### 3.4 Trades history — **NOT RECOMMENDED** (附带评估)

即使要自建, pseudo-code:

```python
# 警告: 此方案性价比不推荐, 仅供理解
API_PATH = "/api/v5/market/history-trades"
# pagination: after=tradeId (旧于), before=tradeId (新于). 每页 100 条.
# NO direct time-range query — 只能从最近 tradeId 一路翻到目标时间.

def collect_trades_history(session, settings, symbol, target_days=60):
    """逐笔 trades 回填 (极慢).
    
    步骤:
      1. 无参数调用一次, 拿最新 100 笔的 tradeId 列表
      2. 设 cursor_after = 该批最老的 tradeId
      3. 循环: after=cursor_after → 返回更老 100 笔 → cursor_after = 新的最老 tradeId
      4. 直到某批最新 trade 的 ts <= target_start_ts 为止
    
    rate limit: 20 req/2s → 10 req/s 安全 → 1000 req/min → 100K trades/min
    60 天 30 msg/s avg = ~155M trades → 需要 ~26 小时纯下载
    实际因 429 / backoff → ~200 小时
    """
    ...
```

**关键 caveat**: 经多家社区反馈 (ccxt Issue #18830 等), OKX `history-trades` 的"历史"实际覆盖
**不如名字暗示的深远**, 往后翻超过 ~数千笔时可能返回空, **无法保证**能真的拿到 60 天前的 trades.

---

## § 4. 错误处理 & 幂等性设计

### 4.1 错误分类和响应

| 错误 | 检测 | 响应 |
|---|---|---|
| 429 rate limit | `resp.status_code == 429` 或 body.code='50011' | 指数 backoff (1s→30s), 同 ip 3 次后 `rate_limit_sleep *= 2` |
| 网络超时 | `httpx.TimeoutException` | 重试 3 次, 每次 backoff 2×; 3 次都失败 → 标 run_item fail, 记 error |
| OKX 错误 code | `body.code != "0"` | 记 run_item failure, **不**无脑重试 (code 可能是参数错误) |
| 返回空 data | `len(data) == 0` | 正常结束 pagination (已触达数据末端) |
| 部分 missing data (time gap) | `detect_gaps()` 检查 ts 序列 | 标 silver quality_flags=['bronze_gap'], 不 block |
| DB 写失败 | sqlalchemy IntegrityError | 回滚 batch, 记 ingest_run 失败, alertmanager 告警 |

### 4.2 resume 机制 (checkpoint)

沿用现有 `aats.data_platform.jobs.checkpoint_manager`:

```python
# 每写一批 bronze 后 advance checkpoint
upsert_checkpoint(
    session,
    dataset_domain="oi_history",
    instrument_type="SWAP",
    symbol=symbol,
    timeframe=period,
    last_successful_ts=oldest_ts_just_written,
    next_expected_ts=oldest_ts_just_written - _TF_DELTA[period],  # 更早方向的下一根
    last_ingest_run_id=run_id,
)
```

重启时从 checkpoint 读 `last_successful_ts` 继续往更早走.

### 4.3 UPSERT 幂等

所有 bronze 表 PK 都是 `(symbol, ts, period)` (或 `(symbol, ts, trade_id)` for trades),
INSERT ... ON CONFLICT PK DO **NOTHING** (不是 UPDATE — OKX 回传的历史数据不会变, 覆盖没意义).

---

## § 5. 分批策略

### 5.1 按 symbol × period × 时间段切片

| 任务 | 切片粒度 | 每片估算 rows |
|---|---|---|
| OI 1H × 90天 | 1 task / symbol (~2160 rows) | 22 API req / task |
| Mark 1m × 30天 | 1 task / symbol (~43200 rows) | 432 API req / task |
| LS 5m × 30天 | 1 task / ccy (~8640 rows) | 87 API req / task |
| Trades 60天 (假如做) | **不切片, 单线长跑**, checkpoint 必备 | ~1.55M req / task |

### 5.2 并行策略 (适用 OI/Mark/LS, **不**适用 Trades)

- OI/Mark/LS 的历史都是低 req 数, 可以**顺序跑**, 总耗时 < 10 min
- 不建议并发多 symbol 因为 OKX rate limit 是 per-IP 的, 并发并不加速
- 若真要并发, 需要多个出口 IP (WSL2 环境一般不具备 — **避免**)

---

## § 6. 资源监控 & 告警

### 6.1 下载过程 metric (Prometheus)

复用现有 `aats.data_platform.operations.*`:

```python
OKX_BACKFILL_REQ_COUNTER = Counter(
    "okx_rest_backfill_requests_total",
    "REST requests sent to OKX",
    ["endpoint", "status"],  # status: success/429/5xx/timeout
)
OKX_BACKFILL_ROWS_COUNTER = Counter(
    "okx_rest_backfill_rows_total",
    "Rows ingested from REST backfill",
    ["table", "symbol"],
)
OKX_BACKFILL_DURATION_HIST = Histogram(
    "okx_rest_backfill_duration_seconds",
    "Duration of single page request",
    ["endpoint"],
)
OKX_BACKFILL_LAG_GAUGE = Gauge(
    "okx_rest_backfill_progress_ratio",
    "Fraction of target_days completed",
    ["task"],
)
```

### 6.2 告警 (沿用 Grafana alert pattern)

- `rate_limit_429_hit_rate > 10%` over 5min → warn
- `backfill_duration_seconds{endpoint=X} p95 > 60s` → warn (可能网络问题)
- `rows_total` 长时间 0 增长但 task 还没 finish → critical (卡住)
- `ingest_run.status = failed` → critical page

### 6.3 资源 baseline

| 资源 | OI+Mark+LS 总和 | Trades (假设) |
|---|---|---|
| 网络 outbound | ~50-100 KB (req payload) × 540 = 50 MB | 数 GB |
| 网络 inbound | ~10 MB | ~12 GB |
| CPU | <5% 1 core × 5min | <15% 1 core × 8 天 |
| DB write IOPS | ~100 IOPS × 5min | 30-50 IOPS × 8 天 持续 |
| Disk (bronze) | ~10 MB | ~12 GB |

---

## § 7. 3rd party 对比

### 7.1 Tardis.dev (https://tardis.dev)

**Pros**:
- 所有 OKX 公开数据 (trades, books, OI, funding, liquidations) 都有历史
- 精确 tick-level, 按秒的 orderbook snapshot
- $300-600 一次性买 3-6 月 BTC 数据
- CSV/JSON 下载, 无 API rate limit 痛

**Cons**:
- 非官方源, 数据一致性 vs OKX 官方 API 可能有 delta (Tardis 自己从 WS 抓)
- 需要一次性打款, 不是 subscription

### 7.2 Kaiko (https://www.kaiko.com)

**Pros**:
- 机构级, 多交易所统一 schema
- 高级指标 (VPIN, microstructure stats 已计算好)

**Cons**:
- $800+ / 月 subscription (相对贵)
- 非 tick-level, 部分数据是 aggregated

### 7.3 CoinGlass / Amberdata

**Pros**: 多指标, 分析报告质量高
**Cons**: 数据多为 aggregated 不 tick level; 免费 tier 历史有限

### 7.4 性价比总结

| 方案 | 成本 | 覆盖 | 推荐度 |
|---|---|---|---|
| 自建 OI/Mark/LS backfill | **0** (开发 3 人日) | 60-90 天 | **强烈推荐** |
| 自建 Trades backfill | 开发 2-3 人日 + 8+ 天下载 + 不稳定 | 短 (OKX 限制不明) | **不推荐** |
| Tardis.dev Trades 历史 | **$300-600 一次性** | 3-6 月精确 | **推荐** for P1-D Phase 2A |
| Kaiko 订阅 | $800+/月 | 全面 | 仅 P2+ 大规模研究可考虑 |

---

## § 8. 实施路线图

### 8.1 Phase 1A (本周, 3 人日)

| 任务 | 产出 | 工时 |
|---|---|---|
| 新 migration `batch_b_08_oi_history_bronze.sql` (+ rollback) | SQL 迁移 + 幂等 | 0.25 d |
| 新 `collectors/backfill/oi_history_backfill_collector.py` | Python module | 0.75 d |
| 新 `scripts/rdp_deep_backfill_oi_history.py` CLI | CLI entry | 0.25 d |
| unit tests (testcontainers) | ≥80% coverage | 0.75 d |
| 新 migration `batch_b_09_mark_price_history_bronze.sql` | 类似 | 0.25 d |
| 新 `mark_price_history_backfill_collector.py` + CLI | Python + CLI | 0.5 d |
| docs/operations runbook 更新 | 操作手册 | 0.25 d |

**退出条件**:
- [ ] 90天 × BTC-USDT-SWAP 1H OI 回填完成 (~2160 rows) 无 error
- [ ] 30天 × BTC-USDT-SWAP 1m mark candles 回填完成 (~43200 rows)
- [ ] 可 resume (中途 kill + 重启继续) 测试通过
- [ ] rate-limit 429 handling 单测覆盖

### 8.2 Phase 1B (本周末, 1 人日)

| 任务 | 产出 | 工时 |
|---|---|---|
| 评估 long-short history 可用性 (read-only, 单页测试) | report | 0.25 d |
| 若可用 → 新 `ls_ratio_history_backfill` | collector + CLI | 0.5 d |
| docs 更新 | update | 0.25 d |

### 8.3 Phase 2 gate

**决策点**: 回填完成 → 让 Phase 2A 用新 bronze 做回归.
- 若 OI-联合特征 R² ≥ 0.01 → continue to Phase 2B
- 若 R² < 0.005 → **考虑**购买 Tardis.dev trades 做更精细 order-flow 特征, 再 evaluate

### 8.4 如果必须上 Trades history 自建 (不推荐)

**时间**: 2-3 人日 dev + 8-10 天下载 (BTC 单 symbol, 60 天)
**风险**:
- OKX `history-trades` 无明确历史深度, 可能爬到 10 天前就返回空
- 单 IP 超限会被短暂 ban, 需要 IP 池 (违反 OKX ToS 风险)

**强烈建议**: 走 Tardis.dev 路径.

---

## § 9. 绝对禁止清单

与本方案设计对齐 AATS 红线:

1. ❌ 不在 production 部署未经测试的 backfill collector
2. ❌ 不跳过 checkpoint 机制直接裸跑 (大型回填必须 resume-safe)
3. ❌ 不并发多 IP 绕 rate limit (ToS 违反)
4. ❌ 不把 trades history 当作 "快速回填" 承诺给 Phase 2A — 时间上做不到
5. ❌ 不改 `aats/services/*` 生产代码 (backfill 全部在 `aats/data_platform/collectors/backfill/` 加新文件)

---

## § 10. 疑问 / 需用户决策

1. **OI history 精度**: 1H 够吗还是要 15m? 15m 会让 rows ×4, 但仍然快 (<5 min)
2. **Mark price 1m 必要性**: 用 1m vs 5m, 前者 5× 数据量, 但对真正的 basis 精度 marginally 更好
3. **Trades 是否真的需要?** — P1-D 可行性 §1.2 说 taker buy/sell ratio 是 **第二重要** 特征 (仅次 OBI).
   如果等 WS 实时采集 30 天, Phase 2A 回归可能本来就够用; 不需要 Tardis.dev.
4. **是否愿意一次性 $300-600 买 Tardis.dev?** 若愿意, Phase 1B-C 时间可以压缩 2 周
5. **先做 OI 还是先做 LS history?** 按 P1-D §5.1 OI 特征 R² 期望高于 LS, 所以 OI 优先.

---

## § 11. 参考文献 & 源码索引

**AATS 现有代码 (reuse)**:
- `scripts/rdp_deep_backfill_api.py` (candles 深度回填, 本设计的母版)
- `scripts/rdp_deep_backfill_funding.py` (funding 深度回填)
- `aats/data_platform/collectors/rolling/candles_api_collector.py` (REST pattern 模板)
- `aats/data_platform/collectors/backfill/candles_backfill_collector.py`
- `aats/data_platform/jobs/checkpoint_manager.py` (resume 机制)
- `aats/data_platform/jobs/run_registry.py` (ingest_run + run_item 状态机)
- `aats/data_platform/operations/failure_registry.py` (错误持久化)

**OKX 文档**:
- Public API Guide v5: https://www.okx.com/docs-v5/en/
- Upcoming changes log: https://www.okx.com/docs-v5/log_en/
- OKX 历史数据下载页面: https://www.okx.com/en-us/historical-data (手工下载 CSV, 不是 API)

**SDK 参考 (endpoint 路径 & 参数验证)**:
- python-okx (official-ish): https://github.com/okxapi/python-okx
- tiagosiebler/okx-api (TypeScript): https://github.com/tiagosiebler/okx-api

**3rd party 数据源**:
- Tardis.dev (tick-level): https://tardis.dev
- Kaiko (institutional): https://www.kaiko.com
- CoinGlass (aggregated): https://www.coinglass.com

**已知限制公共讨论**:
- ccxt #18830: https://github.com/ccxt/ccxt/issues/18830 (OKX history-trades 只能返回 recent trades 的证据)

---

## § 12. 附录 A: 与 §7 现有 collector 兼容性矩阵

| 组件 | 本方案复用程度 | 需改动 |
|---|---|---|
| `run_registry` | 100% | 加新 `run_type='backfill_oi'` / `backfill_mark` / `backfill_ls` |
| `checkpoint_manager` | 100% | 加新 `dataset_domain` 值 |
| `failure_registry` | 100% | 无 |
| `quality_monitor` | 新 silver 表需加 quality rule | 新 config entry |
| `metrics_framework` | 加 3 组新 Prometheus label | 新 metric 定义 |
| `RDP scheduler` (workflow_scheduler) | 新增 backfill workflow (低频, 每周 1 次检查 gap) | 新 scheduled_workflow row |

## § 13. 附录 B: 响应格式参考 (来自 SDK 代码推断, 实测前为假设)

### OI history response
```json
{
  "code": "0",
  "msg": "",
  "data": [
    ["1714000000000", "45678.123", "123.456"],  // [ts_ms, oi, oiCcy]
    ["1713996400000", "45680.567", "123.789"],
    ...
  ]
}
```

### Mark-price candles history response
```json
{
  "code": "0",
  "data": [
    ["1714000000000", "68000.5", "68050.2", "67950.1", "68020.3", "1"],
    // [ts_ms, open, high, low, close, confirm]
    ...
  ]
}
```

### LS ratio history response
```json
{
  "code": "0",
  "data": [
    ["1714000000000", "1.234"],  // [ts_ms, longShortRatio]
    ...
  ]
}
```

**注**: 首次实测需保留 raw_payload 做 schema drift 检测, 一旦确认 schema 稳定 可转为 structured-only.

---

## § 14. 签署

- **设计日期**: 2026-04-20
- **设计范围**: 纯文档, **不含任何代码执行, 不含 OKX API 请求**
- **依据**: AATS 现有 collector pattern + OKX 公开 SDK 代码 + P1-D §2 feasibility
- **推荐决策**:
  - **立即实施**: OI history + Mark-price history backfill (Phase 1A 3 人日)
  - **下周考虑**: LS ratio history backfill (Phase 1B 1 人日)
  - **推荐购买**: Tardis.dev $300-600 一次性 trades 历史, 取代自建 trades backfill
- **不推荐**: 自建 trades history 爬虫 (ROI 极低, 8+ 天下载, 数据深度不可靠)
- **Phase 2A gate**: 上述 3 个 backfill 跑通后, 可让 Phase 2A 回归用新 bronze 扩展特征集
