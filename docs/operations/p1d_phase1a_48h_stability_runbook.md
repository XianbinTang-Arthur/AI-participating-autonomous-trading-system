# P1-D Phase 1A 48h Stability Runbook

> **历史观察窗口（2026-08-22 核对）**：本文只记录 2026-04 P1-D Phase 1A 首次上线后的 48 小时验收，时间、阈值和重启命令不代表当前操作规范。当前健康/恢复流程见 `platform_runbook.md`、`operator_checklist.md` 和根目录 `DEPLOYMENT.md`。

> **目的**: 首次 deploy 之后的 48 小时观察窗口,机械化验证 §11 的 10 个 Phase 1A 验收 Gate,识别故障并有 playbook 应对。
>
> **适用**: P1-D Phase 1A 首次 deploy 后 T+0 ~ T+48h。之后 Phase 1A 宣告 "完成",进入日常运维 (由 operator_checklist.md 接棒)。
>
> **作者**: P1-D Phase 1A Stage 4 实施 agent · 2026-04-20
> **前置**: 设计文档 §11,pre-deploy checklist (本目录下同名文件)
> **前置**: Stage 4 Grafana dashboard `http://localhost:3000/d/p1d-microstructure`

---

## 0. 目的 vs 非目的

**目的**:
- 验证 Phase 1A 10 条验收 Gate 全通过
- 捕捉 "理论 OK, 实际跑 48h 才暴露" 的 edge case (OKX 异常行情 / 交易所维护 / 缓存渗漏)
- 如果命中故障,用 playbook 快速响应不误伤主交易

**非目的**:
- 不是长期监控 (48h 后改为被动告警即可)
- 不替代日常 operator_checklist.md 的每日巡检
- 不做 Phase 2A feature 验证 (whale_threshold 动态化等)

---

## 1. Checkpoint 时间表

| Checkpoint | 目标 | 允许偏差 |
|-----------|------|---------|
| **T+1h** | Bronze 采集稳定, 无 crash | 1 次 < 30 min 内的 WS reconnect 可接受 |
| **T+6h** | Silver ETL 跑过至少 4 次, 无 'etl_failed' flag | 允许 1 次 bar 延迟进 queue |
| **T+24h** | 1 天整全流 round-trip, §11 Gate 3/4 可初步验证 | 1-2 次 short-lived error 可接受 |
| **T+48h** | §11 全部 10 gate 过, Phase 1A 宣告完工 | 无 |

---

## 2. §11 验收 Gate 逐条可编程验证

每个 gate 的 verification 命令, **从 Grafana dashboard (优先) 或 直接 SQL / Prometheus (兜底) 读**。

### Gate 1: 连续 48h 无间断采集 BTC-USDT-SWAP 3 个频道

**设计指标**: `microstructure_ws_reconnect_total` 无连续 5 min 增长, `max(microstructure_ws_last_message_seconds_ago)` < 60s

**Verification**:
```bash
# Prometheus query (Grafana dashboard Panel 1 中也有)
curl -s 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query=increase(aats_microstructure_ws_reconnect_total[5m])' \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print('max 5m reconnect_rate in last 48h:', max((float(r['value'][1]) for r in d['data']['result']), default=0))"

# 直接查 Loki 看 48h 内的断连事件数
# 期望 < 5 (可接受每 10h 一次短暂 reconnect)
```

**Grafana**: Panel 1 "WS Message Rate + Reconnect Rate" — reconnect line < 5/h 持续

**失败响应**: 见 §3.1

### Gate 2: Silver 表每 15min 有新 row

**设计指标**: `SELECT COUNT(*), MAX(ts) FROM silver.market_orderbook_metrics_15m WHERE symbol='BTC-USDT-SWAP' AND ts >= NOW() - INTERVAL '24 hours'` >= 96 行 且 `MAX(ts) >= NOW() - INTERVAL '30 min'`

**Verification**:
```bash
docker exec aats-postgres psql -U admin -d aats_live_derivatives -c "
  SELECT
    COUNT(*) AS bars_24h,
    MAX(ts) AT TIME ZONE 'UTC' AS latest_bar,
    EXTRACT(EPOCH FROM (NOW() - MAX(ts)))/60 AS minutes_since
  FROM silver.market_orderbook_metrics_15m
  WHERE symbol = 'BTC-USDT-SWAP'
    AND ts >= NOW() - INTERVAL '24 hours';
"
# 期望: bars_24h >= 96, minutes_since < 30
```

**Grafana**: Panel 4 右下 stat "Silver 15m bars produced (24h)" — green threshold 96

**失败响应**: 见 §3.2 (Silver ETL 未跑 → 多半是 workflow_scheduler 没识别 custom frequency, 走 pre-deploy checklist §2 的 fallback)

### Gate 3: Bronze `market_trades` 24h row count 符合预期

**设计指标**: `SELECT COUNT(*) FROM bronze.market_trades ... 24h ...` 在 1M ~ 5M 范围

**Verification**:
```bash
docker exec aats-postgres psql -U admin -d aats_live_derivatives -c "
  SELECT COUNT(*) AS trades_24h
  FROM bronze.market_trades
  WHERE symbol = 'BTC-USDT-SWAP'
    AND ts >= NOW() - INTERVAL '24 hours';
"
# 期望: trades_24h in [1000000, 5000000], BTC 常态 ~2.6M
```

**Grafana**: Panel 2 "Bronze trades row count (24h)" stat — green 1M-5M

**失败响应**:
- count < 1M → WS 采集掉了,见 §3.1
- count > 5M → BTC 行情极端 (cascade 清算日),不是 bug,继续观察

### Gate 4: Silver 表 quality_flags 无 'etl_failed'

**Verification**:
```bash
docker exec aats-postgres psql -U admin -d aats_live_derivatives -c "
  SELECT
    'orderbook' AS table_name,
    COUNT(*) FILTER (WHERE 'etl_failed:orderbook_metrics' = ANY(quality_flags)) AS failed_count
  FROM silver.market_orderbook_metrics_15m
  WHERE ts >= NOW() - INTERVAL '24 hours'
  UNION ALL
  SELECT 'trade_flow', COUNT(*) FILTER (WHERE 'etl_failed:trade_flow' = ANY(quality_flags))
  FROM silver.market_trade_flow_15m WHERE ts >= NOW() - INTERVAL '24 hours'
  UNION ALL
  SELECT 'oi_funding', COUNT(*) FILTER (WHERE 'etl_failed:oi_funding_metrics' = ANY(quality_flags))
  FROM silver.market_oi_funding_metrics_15m WHERE ts >= NOW() - INTERVAL '24 hours'
  UNION ALL
  SELECT 'volume_profile', COUNT(*) FILTER (WHERE 'etl_failed:volume_profile' = ANY(quality_flags))
  FROM silver.market_volume_profile_15m WHERE ts >= NOW() - INTERVAL '24 hours'
  UNION ALL
  SELECT 'liquidation', COUNT(*) FILTER (WHERE 'etl_failed:liquidation_metrics' = ANY(quality_flags))
  FROM silver.market_liquidation_metrics_15m WHERE ts >= NOW() - INTERVAL '24 hours';
"
# 期望: 所有 failed_count = 0
```

**Grafana**: Panel 3 右侧 stat "Silver 'etl_failed' count (24h)" — 期望 0

**失败响应**: 见 §3.2

### Gate 5: Silver ETL 平均耗时 < 10s/run

**Verification**:
```bash
# Loki p95 duration (Grafana Panel 3 left 就是这个查询)
curl -G 'http://localhost:3100/loki/api/v1/query_range' \
  --data-urlencode 'query=quantile_over_time(0.95, {job="aats"} |= "silver_microstructure_etl" | regexp "duration=(?P<d>[0-9.]+)s" | unwrap d [15m])' \
  --data-urlencode "start=$(date -u -d '-1 hour' +%s)" \
  --data-urlencode "end=$(date -u +%s)" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); 
    vals = [float(v[1]) for r in d['data']['result'] for v in r['values']]; 
    print(f'p95={max(vals) if vals else None:.3f}s ({len(vals)} samples)')"
# 期望: p95 < 10s
```

**Grafana**: Panel 3 左侧 "Silver ETL Duration (p95 / avg)" — p95 < 10s 水平线

**失败响应**: 见 §3.3

### Gate 6: 新容器资源占用在预算内

**Verification**:
```bash
# 最简单: docker stats 抓 60s 快照
docker stats --no-stream aats-microstructure-collector | awk 'NR==2 {print "cpu:", $3, "mem:", $4}'
# 期望: cpu < 30%, mem < 250M
```

**Grafana**: 暂未加 per-container stats panel (可以在后续 iteration 补)

**失败响应**: 见 §3.4

### Gate 7: 所有单元测试通过

```bash
cd ~/aats
source ~/aats-venv/bin/activate
pytest tests/unit/data_platform/test_microstructure_*.py -x -q
# 期望: 119 passed
```

### Gate 8: 集成测试通过

```bash
cd ~/aats
source ~/aats-venv/bin/activate
AATS_RUN_POSTGRES_INTEGRATION=1 pytest \
  tests/integration/data_platform/test_microstructure_pipeline_e2e.py -x -q
# 期望: 5 passed
```

### Gate 9: Rollback SQL 可用

**不要在 live 环境上真跑 rollback**! 只做 staging 或新建 container 验证。预检 checklist §6.3 Level 2 / 3 保留应急路径。Stage 4 E2E 集成测的 `test_migration_forward_rollback_idempotent` 已经证明 rollback 路径 OK。

### Gate 10: Grafana dashboard 可视化

打开 `http://localhost:3000/d/p1d-microstructure`, 确认:
- [ ] Panel 1 "WS Message Rate" 有数据曲线
- [ ] Panel 2 "Bronze Rows Written per 15m" 4 条线 (trades/bbo/books5/oif)
- [ ] Panel 3 "Silver ETL Duration" 非空 (首 15 min 可能空)
- [ ] Panel 4 "Storage Growth" 显示 8+ 张表体积

---

## 3. 失败响应 Playbook

### 3.1 Gate 1 / 3 失败: WS 采集掉了

**诊断**:
```bash
docker logs aats-microstructure-collector 2>&1 | tail -100 | grep -E "okx_ws_disconnected|subscription_error|60004"
```

**决策树**:
- 如果看到 **60004 Too many connections**:
  - 立即停 microstructure-collector: `docker stop aats-microstructure-collector`
  - 启动 pre-deploy checklist §1.2 的方案 A (合并 daemon),Phase 1A 延后 1 天上线
- 如果看到 **subscription_error 其他 code**:
  - 大概率 OKX 频道名 typo 或 instType 错误,检查 collector 的 `_connection_specs` 返回值
- 如果看到 **网络错误 (EOF / timeout)**:
  - OKX 公共 WS cluster 不稳,等 30 min 自动恢复
  - 连续 > 2h 不恢复 → 检查 OKX 状态页 `https://okxstatus.com`
- 如果是 **DB 连接池爆满**:
  - `docker exec aats-postgres psql -c "SELECT COUNT(*) FROM pg_stat_activity;"` 看是否 > 180 (upper limit 200)
  - 重启 microstructure-collector 释放连接

### 3.2 Gate 2 / 4 失败: Silver ETL 未跑 / 失败

**诊断步骤 1**: 任务是否入队?
```bash
docker exec aats-postgres psql -U admin -d aats_live_derivatives -c "
  SELECT workflow, status, COUNT(*), MAX(created_at)
  FROM governance.rdp_task_queue
  WHERE workflow = 'microstructure_silver_15m'
  GROUP BY workflow, status;
"
# 期望: status='completed' 的 count > 0 (过去几小时至少几行)
```

- 如果 count=0: **scheduler 根本没入队** → apply pre-deploy checklist §2.3 的 `workflow_scheduler.py` patch
- 如果 status='failed' 多: 看失败原因

**诊断步骤 2**: 失败原因
```bash
docker exec aats-postgres psql -U admin -d aats_live_derivatives -c "
  SELECT last_error FROM governance.rdp_task_queue
  WHERE workflow = 'microstructure_silver_15m' AND status = 'failed'
  ORDER BY created_at DESC LIMIT 3;
"
```

- `STDDEV_SAMP no such function`: SQLite polyfill 漏了,但生产是 PG 不应该是这个
- `column "..." does not exist`: Silver schema 与 ETL SQL 不一致,需 debug
- `duplicate key`: UPSERT 逻辑 broken,需 debug (test_idempotency 应已 catch)

**诊断步骤 3**: ETL log
```bash
docker logs aats-rdp-daemon 2>&1 | tail -200 | grep silver_microstructure
```

### 3.3 Gate 5 失败: ETL 耗时 > 10s

**诊断**:
```bash
# 哪个 _build_* 慢
docker logs aats-rdp-daemon 2>&1 | grep silver_microstructure_etl | tail -5
# 看 written dict: 如果某一张表耗时明显大,针对性优化
```

**常见根因**:
- `bronze.market_trades` 数据暴涨 (极端行情) → Phase 2A 加 partitioning
- PG autovacuum 跑慢 → 手动 `VACUUM ANALYZE bronze.market_trades`
- 4-week baseline SQL (volume_profile) 慢 → 加 `CREATE INDEX` 或改算法

**临时措施**: 把 `scripts/rdp_build_microstructure_silver.py --timeout 600` (5 min → 10 min),避免 timeout killed

### 3.4 Gate 6 失败: 资源超预算

- CPU > 30%: 多半 parser 瓶颈,看哪个 channel 消息率异常高 (可能 BTC 极端行情)
- Memory > 250M: buffer 漏放,手动 `docker exec ... python -c 'print(buf.buffered())'` 看 buffered 行数
- 如连续 2h 超限,重启 `docker restart aats-microstructure-collector`,收集 profile

---

## 4. 日常巡检节奏 (48h 窗口内)

**T+1h 首次巡检** (5 min, CLI):
```bash
# 1 分钟看 Grafana dashboard
xdg-open http://localhost:3000/d/p1d-microstructure
# 1 分钟看容器 health
docker ps --format "table {{.Names}}\t{{.Status}}" | grep aats
# 3 分钟看 DB 行数
docker exec aats-postgres psql -U admin -d aats_live_derivatives -c "
  SELECT 'trades' AS t, COUNT(*) FROM bronze.market_trades WHERE ts >= NOW() - INTERVAL '1 hour'
  UNION ALL SELECT 'bbo', COUNT(*) FROM bronze.market_orderbook_bbo WHERE ts >= NOW() - INTERVAL '1 hour'
  UNION ALL SELECT 'silver_ob', COUNT(*) FROM silver.market_orderbook_metrics_15m;"
```

**T+6h 巡检** (15 min):
- 跑 Gate 1, 2, 5 验证命令
- Grafana dashboard 看 p95 趋势是否平稳
- 看 Loki 近 6h 里有没有 `etl_failed` / `subscription_error` / `60004`

**T+24h 巡检** (30 min):
- 跑 Gate 1-6 完整
- 看 48h 内 OKX 有没有 maintenance 通告
- 记录 Phase 1A 第一天 running stat 供 Phase 2A baseline

**T+48h 验收** (1h):
- 跑所有 10 个 Gate
- 如果全过, 在 `docs/review/p1d_phase1a_completion_2026_04_20.md` 标记 "48h gate 全通过"
- 用户发起 Phase 1B kickoff

---

## 5. 告警接警 (48h 窗口用)

**Grafana UI**:
- `http://localhost:3000/alerting/list` 看所有 11 条 alert rule 状态
- P1-D 相关 6 条: sev2-micro-ws-stale / sev3-micro-ws-reconnect / sev3-micro-silver-etl-slow / sev2-cost-fee-drift / sev2-cost-margin-tight / sev3-blocked-close-only-race

**对应响应**:

| 告警 | 响应时间 | 操作 |
|------|---------|------|
| SEV2 micro-ws-stale | 15 min | 见 §3.1 |
| SEV3 micro-ws-reconnect | 日内 | 看 OKX status,不紧急 |
| SEV3 micro-silver-etl-slow | 日内 | 见 §3.3 |
| SEV2 cost-fee-drift | 15 min | 查 cost_audit_live_reconciliation §7.2,评估 cost_margin 是否被触发 |
| SEV2 cost-margin-tight | 15 min | 看具体订单,是否需要临时调 `max_acceptable_cost_bps` |
| SEV3 blocked-close-only-race | 日内 | 看 §2.4 分析原因,连续 5 次加急 |

---

## 6. 48h 窗口结束后

### 6.1 如果 10 gate 全通过

1. `docs/review/p1d_phase1a_completion_2026_04_20.md` 标 "48h 稳定性验证: ✅ 全通过"
2. 把本 runbook 在 operator_checklist.md 里引用 (日常巡检参考 Gate 2/4)
3. Phase 1A 宣告完成,用户可 kickoff Phase 1B (recommendation 生成 + review UI)

### 6.2 如果有 1-2 gate 失败但已缓解

- 写 root cause analysis 文档到 `docs/review/p1d_phase1a_48h_rca_<date>.md`
- 如是 `workflow_scheduler` 的 custom frequency 问题 → 把 patch 合进主线
- 如是 OKX 暂时性问题 → 没有 action item,只记录

### 6.3 如果 > 2 gate 失败或严重故障

- 执行 pre-deploy checklist §6.3 Level 1 rollback (停 microstructure-collector)
- 召集 root cause session,按影响拆 P1B / P1C
- Phase 1A 按 root cause 迭代 (可能需要 Stage 5 修复)

---

## 7. 签署

- **作者**: P1-D Phase 1A Stage 4 agent · 2026-04-20
- **Scope**: Phase 1A 首次 deploy 后 T+0 ~ T+48h 的 10 gate 验证
- **不涵盖**: OKX API quota / monitoring 告警 contactpoint / Phase 2A feature
- **接棒**: 48h 后 `operator_checklist.md` 接日常巡检
