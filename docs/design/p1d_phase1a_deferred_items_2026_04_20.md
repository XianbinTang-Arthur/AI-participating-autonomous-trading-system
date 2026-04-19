# P1-D Phase 1A 遗留事项 / Phase 2A 待办清单

> **作者**: P1-D Phase 1A Stage 4 实施 agent · 2026-04-20
> **范围**: 把 Stage 1-3 完工报告里标注为 "technical debt" / "Phase 2A decision" 的 3 项遗留固化下来,供 Phase 2A kickoff 时直接领用。
> **不包含**: Phase 1A 已完成的 feature, 以及 Phase 1B (recommendation 生成 + review UI) 的新 scope。
> **前置**:
> - `docs/review/p1d_phase1a_stage3_completion_2026_04_20.md` §8.2
> - `docs/design/p1d_phase1a_implementation_design_2026_04_20.md` §5 (字段定义)

---

## 遗留事项总览

| # | 项目 | 触发条件 | 工作量估算 | 所在文件 |
|---|------|---------|-----------|---------|
| 1 | `price_change_bps` / `oi_price_regime` 历史 mid_ref | Phase 1A 48h 运行完有 ≥10 bar 历史 | 0.5 人天 | `aats/data_platform/merge/microstructure_silver_merger.py::_build_oi_funding_metrics` |
| 2 | `whale_threshold` 固定 2.0 → 1h rolling p99 | Phase 1A 稳定跑完 7 天 | 0.5 人天 | `microstructure_silver_merger.py::_build_trade_flow` |
| 3 | `intensity_z_7d` / `funding_z_score_7d` 冷启动 NULL | Phase 1A 跑完 7 天自动解除 | 0 人天 (自动) | 不涉及代码改动, 仅监控配置 |
| 4 | **所有 aats 进程 Prometheus :9464/:9465 `connection refused`** | T+1h 巡检 (2026-04-20) 发现 | 0.5-1 人天 | `aats/bootstrap/telemetry.py` / `aats/bootstrap/metrics_bridge.py` |

**总工期**: **1.5-2 人天** + 1 周等待数据积累。

---

## 遗留 1. `price_change_bps` + `oi_price_regime` 历史 mid_ref

### 1.1 设计 vs 现状

**设计 §5.3 要求**:
```sql
price_change_bps         NUMERIC(12, 4),    -- 15m log-return * 10000
oi_price_regime          TEXT,               -- 'trend_long', 'trend_short',
                                             --   'short_cover', 'long_cover',
                                             --   'mixed', 'flat'
```

**Phase 1A 现状** (Stage 3 完工报告 §8.2):
- `price_change_bps` 固定 NULL
- `oi_price_regime` 只有 3 类 (`trend_long` / `long_cover` / `flat`), 基于 `oi_delta` 符号

**Gap 原因**: `price_change_bps = (current_mid - prev_mid) / prev_mid * 10000` 需要 **上一个 15m bar 的 mid_price_ref**。Phase 1A 首次 bar 时没有历史, 只好置 NULL。Stage 3 作者选择暂时让整个 15m 的所有 bar 都 NULL,避免给 reviewer 半成品。

### 1.2 Phase 2A 实现方案 (≈30 行 SQL)

在 `_build_oi_funding_metrics` 里加一次子查询读 prev bar:

```python
# 在 _build_oi_funding_metrics 的 UPSERT 前, 先查上一 bar 的 mid
prev_mid_query = text("""
    SELECT mid_price_last
    FROM silver.market_orderbook_metrics_15m
    WHERE symbol = :symbol AND ts = :prev_bar
""")
prev_row = session.execute(
    prev_mid_query,
    {"symbol": symbol, "prev_bar": bar_start_ts - timedelta(minutes=15)},
).fetchone()
prev_mid = prev_row.mid_price_last if prev_row else None

# 算 price_change_bps
if prev_mid and mid_price_ref and prev_mid > 0:
    log_return = math.log(float(mid_price_ref) / float(prev_mid))
    price_change_bps = Decimal(log_return * 10000).quantize(Decimal("0.0001"))
else:
    price_change_bps = None
```

### 1.3 oi_price_regime 6 类扩展

结合 `oi_delta` 符号 + `price_change_bps` 符号的四象限 + 强度:

```python
def _compute_regime(oi_delta: Decimal | None, price_change_bps: Decimal | None) -> str:
    if oi_delta is None or price_change_bps is None:
        return "flat"
    oi_sign = 1 if oi_delta > 0.005 else (-1 if oi_delta < -0.005 else 0)
    px_sign = 1 if price_change_bps > 5 else (-1 if price_change_bps < -5 else 0)
    # (oi, px) 四象限 + "mixed" 边界带
    if oi_sign == 0 or px_sign == 0:
        return "mixed" if (oi_sign != 0 or px_sign != 0) else "flat"
    if oi_sign == 1 and px_sign == 1:
        return "trend_long"           # OI ↑ + price ↑ = 新多头
    if oi_sign == 1 and px_sign == -1:
        return "trend_short"          # OI ↑ + price ↓ = 新空头
    if oi_sign == -1 and px_sign == 1:
        return "short_cover"          # OI ↓ + price ↑ = 空头回补
    if oi_sign == -1 and px_sign == -1:
        return "long_cover"           # OI ↓ + price ↓ = 多头清盘
    return "mixed"
```

### 1.4 触发条件

Phase 1A 48h 跑完 (T+48h 之后) Silver 有 ≥ 192 bar 历史。Phase 2A kickoff 第一件事就能做。

**单元测试**: 5 cases,每个 regime + 1 个 flat 边界。

---

## 遗留 2. `whale_threshold` 固定 2.0 contracts → 1h rolling p99

### 2.1 设计 vs 现状

**设计 §5.2 要求**:
```
whale_threshold_applied  NUMERIC(18, 8),    -- 15m 窗口用的阈值（溯源）
```

**Phase 1A 现状** (Stage 3 完工报告 §8.2):
```python
# microstructure_silver_merger.py 顶部常量
_WHALE_SIZE_FALLBACK = Decimal("2.0")  # contracts
```

Gap: Stage 3 实现完全用 `_WHALE_SIZE_FALLBACK`,没真 rolling。理由:冷启动时 1h rolling p99 样本不足,会给极低阈值让每笔 trade 都是 "whale", 数据失真。Phase 1A 保守用固定值。

### 2.2 Phase 2A 实现方案 (≈40 行 SQL)

```python
# 在 _build_trade_flow 里, 算 whale_threshold 前:
threshold_query = text("""
    WITH recent_trades AS (
        SELECT sz FROM bronze.market_trades
        WHERE symbol = :symbol
          AND ts >= :bar_start - INTERVAL '1 hour'
          AND ts < :bar_start
    )
    SELECT
        COUNT(*) AS n_samples,
        percentile_cont(0.99) WITHIN GROUP (ORDER BY sz) AS p99
    FROM recent_trades
""")
row = session.execute(
    threshold_query,
    {"symbol": symbol, "bar_start": bar_start_ts},
).fetchone()

if row.n_samples >= 500:  # enough samples for p99 stability
    whale_threshold = Decimal(row.p99).quantize(Decimal("0.00000001"))
    flags.append("whale_threshold_rolling_p99")
else:
    whale_threshold = _WHALE_SIZE_FALLBACK
    flags.append("whale_threshold_fallback_cold_start")
```

### 2.3 双阈值策略

**更聪明的做法**: 计算 1h rolling p99, 但与 `_WHALE_SIZE_FALLBACK` 取 `max()`。这样:
- 冷启动 (n_samples < 500): 用 fallback 2.0
- 正常市 (1h 内 BTC 交易 ~100K 笔, p99 ≈ 5-15 contracts): 用 rolling p99
- 极端低流动 (p99 < 2.0): 保护性取 fallback

```python
whale_threshold = max(
    Decimal(row.p99).quantize(Decimal("0.00000001")) if row.n_samples >= 500 else Decimal("0"),
    _WHALE_SIZE_FALLBACK,
)
```

### 2.4 触发条件 + 验证

- Phase 1A 稳定跑完 7 天 (bronze.market_trades 有 ~18M 行)
- Phase 2A 首次 regression 发现 "whale count 失真" 时加急

**回归测试**:
- 在 Phase 2A agent 的 commissioning 期,对比新旧 threshold 产生的 `whale_count` 差异
- 期望新 whale_count 明显降低 (旧固定 2.0 产生虚警,新 rolling p99 只标真正的大单)
- Grafana 加一个 stat 展示 `whale_threshold_applied` 的 24h 移动窗口

### 2.5 风险

**regime shift**:极端低流动阶段 (例如 OKX 维护或公告日),1h p99 可能骤降。回退到 `_WHALE_SIZE_FALLBACK` 的 `max()` 保护能处理。

---

## 遗留 3. `intensity_z_7d` / `funding_z_score_7d` 冷启动 NULL

### 3.1 现状 (Stage 3 完工报告 §8.2)

- `silver.market_liquidation_metrics_15m.intensity_z_7d`: 7d rolling z-score,冷启动首 7 天 NULL
- `silver.market_oi_funding_metrics_15m.funding_z_score_7d`: 同上

**这不是 bug**,设计就是如此,但 Stage 4 的 Grafana dashboard 没特别标注冷启动状态,容易被误解。

### 3.2 Phase 2A 不需要代码改动

**不写代码**。Phase 1A 跑满 7 天后自动解除。

### 3.3 Phase 2A 需要的 Grafana / 监控工作

**Grafana dashboard 上给这 2 个字段加 'partial baseline' 状态面板**:

在 `p1d_microstructure.json` Panel 4 下面加一个 stat panel:
```json
{
  "title": "Baseline Maturity",
  "targets": [
    {
      "rawSql": "SELECT
        (SELECT COUNT(*) FROM silver.market_oi_funding_metrics_15m
         WHERE funding_z_score_7d IS NOT NULL
           AND ts >= NOW() - INTERVAL '24 hours') AS funding_z,
        (SELECT COUNT(*) FROM silver.market_liquidation_metrics_15m
         WHERE intensity_z_7d IS NOT NULL
           AND ts >= NOW() - INTERVAL '24 hours') AS intensity_z
      "
    }
  ]
}
```

冷启动期显示 "funding_z=0 / intensity_z=0 (cold start, ready at T+7d)";满 7 天后显示 "funding_z=96 / intensity_z=96 (mature)"。

### 3.4 工作量

- **代码**: 0 行
- **Grafana JSON 改动**: ~15 行 (1 panel)
- **总工期**: 1 小时

可以并入 Phase 2A 第一个 PR (feature PR for #1 + dashboard refresh)。

---

## 遗留 4. Prometheus scrape 全部 `aats` 进程 `connection refused`

**发现时间**: Phase 1A deploy T+1h 巡检 (2026-04-20)

### 4.1 现状

```
up{instance="aats-gateway:9464",job="aats"}                        = 0
up{instance="aats-decision:9464",job="aats"}                       = 0
up{instance="aats-market:9464",job="aats"}                         = 0
up{instance="aats-execution:9464",job="aats"}                      = 0
up{instance="aats-microstructure-collector:9465",job="aats-microstructure"} = 0
up{instance="redis-exporter:9121",job="redis"}                     = 1 (normal)
```

Prometheus 容器内 `wget http://aats-gateway:9464/metrics` 直接 `Connection refused`。
所有 aats 主进程和新 microstructure-collector 的 HTTP metrics server 都没启。

### 4.2 关键定性

- **不是 Phase 1A 回归** — gateway / market / decision / execution 的 :9464 也 refused,说明 OTel Prometheus 桥接**很早就断了**,Phase 1A Stage 4 加的 microstructure 只是跟着同一个坏范式
- **不影响数据采集** — Bronze/Silver 表直接 DB insert,不走 Prometheus
- **影响观测** — Grafana P1-D dashboard (4 panel) 当前无数据;Gate 5 (Silver ETL p95 < 10s) 和 Gate 10 (dashboard 可视化) 无法通过 Grafana 验证,但 Gate 5 可 DB 查 `meta.ingest_runs.duration_ms` 替代验证

### 4.3 可能原因

候选 (需 Phase 2A 调研确认):

1. **`aats/bootstrap/telemetry.py::configure_telemetry()`** 注册了 PrometheusMetricReader 但没调 `prometheus_client.start_http_server(port)`
2. **OTEL_EXPORTER_PROMETHEUS_HOST / _PORT env vars** 在主 4 进程 compose service 未设置 (microstructure-collector compose 确实设了,仍 refused → 可能不只是 env 缺失)
3. **MetricsRegistry → metrics_bridge.create_bridge()** 只挂了 Meter, 没挂 PrometheusMetricExporter
4. **process_lifecycle.py** 可能没调 configure_telemetry 在 process role 特定的初始化路径

### 4.4 Phase 2A 实现方案 (≈0.5-1 人天)

**Step 1**: 盘点现有 telemetry 代码
- Read `aats/bootstrap/telemetry.py` + `metrics_bridge.py` + `process_lifecycle.py`
- 找到 4 主进程和 daemon 的 telemetry 初始化路径
- 确认 PrometheusMetricReader + HTTP server 之间的接线

**Step 2**: 修复 HTTP server 启动
- 若是缺 `start_http_server(port)` → 加进 telemetry 初始化
- 若是 env var 缺失 → 在 compose 4 主进程 service 加 `OTEL_EXPORTER_PROMETHEUS_HOST/PORT`
- 若是 OTel 版本升级 API 变化 → 参考 opentelemetry-python docs/changelog

**Step 3**: 验证 all 5 targets up=1 后, Grafana dashboard 正常显示

### 4.5 验收标准

- [ ] `curl http://prometheus-host:9090/api/v1/query?query=up{job="aats"}` 返回 5 条 value=1
- [ ] P1-D Grafana dashboard 4 panel 有数据
- [ ] 没触发现有 alert rule (等数据填充后)

### 4.6 触发条件

**立即 trigger**: Phase 2A kickoff Day 1 前置工作 (Prometheus 没修好会影响 Phase 2A 的监控面板 + Gate 5 自动化验证)。

### 4.7 风险

- 改动 `telemetry.py` 属于 bootstrap 路径, 误操作可能影响所有 4 主进程启动
- **缓解**: 改完先在 dev env 全量跑单元测试 + 一次完整 deploy 确认无回归
- **回退**: git revert + redeploy, 数据采集不受影响

---

## Phase 2A kickoff 建议顺序

按依赖和难度,Phase 2A Stage 1 建议实现顺序:

1. **Day 0 (前置)**: 遗留 #4 — Prometheus scrape 修复 (否则 #3 监控 + Gate 5 自动验证无法做)
2. **Day 1**: 遗留 #1 — `price_change_bps` + `oi_price_regime` 6 类扩展 (1 PR)
3. **Day 2 AM**: 遗留 #3 — Grafana dashboard baseline maturity panel (1 PR)
4. **Day 2 PM**: 遗留 #2 — whale_threshold rolling p99 (1 PR)

Phase 2A Stage 2+ 才是真正的 regression study (多 horizon / 模型扩展),不属于本文档范围。

---

## 不纳入本文档的事项

以下 3 项是 Stage 1-3 的 scope-out 决策,**不是遗留**,避免 Phase 2A 误当成 TODO:

1. **bbo-tbt 采样率 1Hz → 10Hz**: 附录 E #5 决策 "Phase 2A regression 后再评估",不是 debt。
2. **daemon 合并 (microstructure + liquidations)**: 附录 E #2/8 决策 "不合并",除非 60004 触发。
3. **多 horizon silver 表 (_1m / _5m)**: 附录 E #4 决策 "不预留",需要时 Phase 2A 新建 `batch_b_07_microstructure_multi_horizon`。

---

## 签署

- **作者**: P1-D Phase 1A Stage 4 agent · 2026-04-20
- **审批**: 待用户 Phase 2A kickoff 时 review
- **更新条件**: 若 Phase 1A 48h 稳定性观察期内发现新 debt,追加到本文档
- **更新记录**:
  - 2026-04-20 T+1h 巡检: 追加遗留 #4 (Prometheus scrape 全量 connection refused)
  - 注: 今次已用 batch_b_07 命名另一个 migration (ingest_runs CHECK 扩展)。若 Phase 2A 加多 horizon silver 表, 命名改为 `batch_b_08_microstructure_multi_horizon` (替代上文"不纳入本文档的事项" #3 提到的旧命名)
