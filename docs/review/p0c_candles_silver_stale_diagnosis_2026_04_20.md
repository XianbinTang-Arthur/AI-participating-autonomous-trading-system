# P0-c: `silver.market_{spot,swap}_candles_15m` 停更 19h+ 根因诊断

**诊断时间**: 2026-04-20 03:50 UTC (= 11:50 Shanghai)
**诊断范围**: 仅定位根因, 不做任何代码/配置/数据变更
**上游需求**: 路线 A research phase 0 需要 OHLC 作为 microstructure signal 的对照基线

---

## §1 Symptom (精确数字 + 时间线)

### 1.1 Silver candles 停更观察

| 表 | max(ts) (UTC) | last created_at (UTC) | 行数 | stale 时长 |
|---|---|---|---|---|
| `silver.market_spot_candles_15m` | 2026-04-19 08:00:00 | 2026-04-19 08:00:13 | 6386 | 19h 50min |
| `silver.market_swap_candles_15m` | 2026-04-19 08:00:00 | 2026-04-19 08:00:14 | 6386 | 19h 50min |

### 1.2 Bronze / Staging 同样停更 (链路上游也断了)

| 表 | max(ts) (UTC) | last created_at (UTC) | 行数 |
|---|---|---|---|
| `staging.market_spot_candles_15m` | 2026-04-19 08:00:00 | 2026-04-19 08:00:13 | 6386 |
| `staging.market_swap_candles_15m` | 2026-04-19 08:00:00 | 2026-04-19 08:00:14 | 6386 |
| `bronze.market_spot_candles_15m` | 2026-04-19 08:00:00 | 2026-04-19 08:00:13 | 6386 |
| `bronze.market_swap_candles_15m` | 2026-04-19 08:00:00 | 2026-04-19 08:00:14 | 6386 |

→ **Silver 不是孤立停的, 整个 Bronze/Silver/Staging candles 链路一起停在 08:00 UTC 同一 bar**.

### 1.3 Checkpoint 状态 (健康, 无异常)

| dataset_domain | symbol | timeframe | last_successful_ts (UTC) | next_expected_ts (UTC) | status |
|---|---|---|---|---|---|
| candles | BTC-USDT | 15m | 2026-04-19 08:00:00 | 2026-04-19 08:15:00 | active |
| candles | BTC-USDT-SWAP | 15m | 2026-04-19 08:00:00 | 2026-04-19 08:15:00 | active |
| candles | ETH-USDT | 15m | 2026-04-19 08:00:00 | 2026-04-19 08:15:00 | active |
| candles | ETH-USDT-SWAP | 15m | 2026-04-19 08:00:00 | 2026-04-19 08:15:00 | active |

→ **Checkpoint 没有卡在 gap / stale, 下一次 ingest 会从 08:15 UTC 正确接续**. 不是数据损坏.

### 1.4 `ingest_runs` 时间线 — candles domain

过去 24 小时 `dataset_domain='candles'` 的 run 全部成功, 但分布呈 **明显的日批簇**:

| 时间窗 (UTC) | trigger | run_type | 状态 | 驱动 workflow |
|---|---|---|---|---|
| 2026-04-19 03:43 | scheduler | rolling ×8 + gold ×4 | succeeded | (疑似手动) research_cycle |
| 2026-04-19 **04:00** | scheduler | rolling ×8 + gold ×4 | succeeded | **data_maintenance (每日 04:00 UTC 槽位)** |
| 2026-04-19 06:28 | scheduler | rolling ×8 + gold ×4 | succeeded | (疑似手动) research_cycle |
| 2026-04-19 **08:00** | scheduler | rolling ×8 + gold ×4 | succeeded | **research_cycle (Sunday 08:00 UTC 槽位, 内嵌 data_maintenance)** |
| **2026-04-19 08:00 → now (2026-04-20 03:50)** | — | **zero runs** | — | — |

### 1.5 同期对照: 其他 workflow 正常

同一 `governance.rdp_task_queue` 在 19h 40min 的"空窗"内全程活跃:

- `microstructure_silver_15m` 每 15 min 入队 + 跑完, 连续 80 个槽位全 done exit=0
- `release_cycle` / `observation_cycle` / `reliability_cycle` 每小时入队 + done
- **`data_maintenance` 0 次入队, `research_cycle` 0 次入队** (因为它们的下一个槽位 = 2026-04-20 04:00 UTC / 下周日)

→ Scheduler / task daemon / Postgres / Docker 全部健康. **仅 candles pipeline 的调度器槽位本身不产出**.

---

## §2 ETL 路径识别

### 2.1 调度链

```
cron(-ish) scheduler (aats/data_platform/operations/workflow_scheduler.py)
    └─ 按 configs/rdp_workflows/*.json 的 schedule 字段决定 slot
    └─ slot 到期 → 写 governance.rdp_task_queue (status='pending')

rdp-daemon (scripts/rdp_task_daemon.py, 容器 aats-rdp-daemon)
    └─ 每 10s 轮询 rdp_task_queue, claim → 运行 → 标 done/failed
```

### 2.2 `data_maintenance` workflow 配置 (configs/rdp_workflows/data_maintenance.json)

```json
{
  "workflow": "data_maintenance",
  "schedule": {
    "enabled": true,
    "frequency": "daily",
    "hour_utc": 4,
    "minute_utc": 0
  },
  "tasks": [
    { "name": "daily_ingest",
      "command": "python scripts/rdp_run_daily_ingest.py",
      "timeout_seconds": 900 },
    { "name": "artifact_index_rebuild", ... }
  ]
}
```

**触发频率**: 每天 1 次, 04:00 UTC.

### 2.3 ETL 代码入口

```
scripts/rdp_run_daily_ingest.py
    └─ aats.data_platform.collectors.rolling.candles_api_collector.collect_candles_incremental
            └─ OKX REST GET /api/v5/market/history-candles  (基于 checkpoint 增量)
            └─ INSERT INTO staging.market_{spot|swap}_candles_{tf} ...
    └─ aats.data_platform.merge.merge_pipeline.run_candle_merge_pipeline
            └─ staging → bronze (merge_candles_to_bronze)
            └─ bronze → silver (merge_candles_to_silver)
                    └─ INSERT INTO silver.market_{spot|swap}_candles_{tf}
                       ON CONFLICT (symbol, ts) DO UPDATE ...
```

**默认 symbol/timeframe** (`aats/data_platform/config.py`):

- `rolling_candles_symbols = [BTC-USDT, ETH-USDT, BTC-USDT-SWAP, ETH-USDT-SWAP]`
- `rolling_candles_timeframes = ["15m", "1h"]`

### 2.4 Bronze 源

**唯一** Bronze candles 写入方是 `collect_candles_incremental` (OKX REST). 没有 websocket / streaming 写 candles Bronze.

(注: 任务描述里说"Bronze 数据采集端过去 24h 连续有数据"指的是 `bronze.market_trades` / `bronze.market_orderbook_bbo` / `bronze.market_orderbook_books5` — 这些是 microstructure 源, 由独立的 market 进程 websocket 采集, 与 candles Bronze 完全不是同一条管道.)

---

## §3 Root Cause (证据链)

### 3.1 一句话结论

**`silver.market_*_candles_15m` 每天只在 04:00 UTC 一次 `data_maintenance` 槽位 (+ 每周日 08:00 UTC 的 research_cycle 槽位) 被更新. 从 2026-04-19 08:00 UTC research_cycle 跑完到现在, 没有任何调度槽位到期, 所以 candles pipeline 自然停在了 08:00 UTC 那个 bar — 这是"按设计运行", 不是故障.**

### 3.2 证据链

**证据 1** — `data_maintenance` 配置为 daily 04:00 UTC (文件: `configs/rdp_workflows/data_maintenance.json`), **不是 hourly**.

**证据 2** — `meta.ingest_runs` 表 `dataset_domain='candles'` 最近 48h 的 `trigger_mode='scheduler'` 成功 run **全部落在** 04:00 / 08:00 UTC 以及两个 ad-hoc 手动触发点 (03:43, 06:28). 没有任何小时内 cadence 的 candles ingest run.

**证据 3** — `meta.ingest_checkpoints` 四条 candles 15m checkpoint 全部 `status='active'`, `last_successful_ts=08:00 UTC 2026-04-19`, `next_expected_ts=08:15 UTC 2026-04-19`. 没有卡死, 没有 gap_detected.

**证据 4** — `governance.rdp_task_queue` 最近 24h:
- `data_maintenance` 最后一次入队 = 2026-04-19 04:00:04 UTC, status=done, exit=0, 10 秒完成, log_tail 显示所有 4 个 symbol × 2 个 timeframe 的 Bronze/Silver/Gold 全部写成功
- 之后 **0 次** `data_maintenance` 入队 (因为 daily 槽位 2026-04-20 04:00 UTC 还没到)
- `research_cycle` 最后一次 = 2026-04-19 08:00:09 UTC (Sunday 08:00 UTC 槽位), 内嵌调用 data_maintenance → 写入 08:00 UTC bar
- 其后 `research_cycle` 下一个槽位 = 2026-04-26 08:00 UTC (再 6 天才到)

**证据 5** — rdp-daemon 容器在 2026-04-20 03:18:39 UTC 重启 (container StartedAt), 但重启**前后**的 scheduler 行为无异常: 重启即入队 hourly workflows (release/observation/reliability) 和 microstructure_silver_15m, 都正常 run done. 之所以没入队 data_maintenance, 是因为 scheduler 的 `last_processed_slot` 记录显示 2026-04-19 04:00 UTC 槽位已处理, 2026-04-20 04:00 UTC 槽位还没到 (距诊断时间还有约 10 分钟).

**证据 6** — rdp-daemon 日志 `docker logs --since 24h aats-rdp-daemon | grep -i candle` 无任何 candles 相关 ERROR / FAILED 记录.

**证据 7** — 实际日批执行日志 (data_maintenance task log_tail, 2026-04-19 12:00:09+08 UTC) 显示所有 candles Bronze/Silver merge 写入成功, 最后一行:
```
[Gold] OK ETH-USDT-SWAP 1h (0.2s)
[3/4] Gold done: 4 ok, 0 failed
[4/4] Gap detection done: 8 checked, 0 total gaps
```

### 3.3 排除的可能性

| 可能性 | 证据 | 结论 |
|---|---|---|
| 数据源 OKX REST 故障 | checkpoint status=active, 没有 failed run, 下次 ingest 会正常接续 | **排除** |
| ETL 代码挂了 | 最近一次 data_maintenance 完整执行日志显示全部 4×2 symbol 都成功 | **排除** |
| DDL 变更冲突 | 无相关错误日志, checkpoint 和表结构一致 | **排除** |
| workflow 调度器宕机 | scheduler 在 19h 窗口内连续入队 ~80 个 microstructure_silver_15m 任务 + 20+ 个 hourly workflow, 完全活跃 | **排除** |
| rdp-daemon 僵死 | heartbeat 连续, 19h 内全部 task done, 无 orphan running | **排除** |
| 槽位被误标 "已处理" | scheduler state 正确记录 2026-04-19 04:00 UTC 槽位已处理. 2026-04-20 04:00 UTC 槽位在本诊断写稿时尚未到 (< 10 min) | **不是 bug, 是设计** |
| **candles pipeline 设计为 daily cadence** | 配置文件 + 运维文档 `docs/operations/rdp_scheduling_strategy.md` §"数据采集迁移到日批" 明确声明: 2026-04-07 起 candles 采集从 60s daemon 切换为每日 04:00 UTC 一次批量拉取 | **是根因** |

### 3.4 关键背景 (来自 `docs/operations/rdp_scheduling_strategy.md`)

> 2026-04-07 重要变更: 数据采集已从常驻 daemon 模式切换为日批模式。原 `rdp_realtime_daemon.py` 的 60s tick 已废弃, 新增 `rdp_run_daily_ingest.py` 由 `data_maintenance` workflow 每天 04:00 UTC 调用一次.

设计论证 (同一文档):

> **为什么日批就够了**
>
> | RDP 消费方 | 实际 cadence | 是否需要 intra-minute 数据 |
> |---|---|---|
> | data_maintenance | daily 04:00 UTC | 否 |
> | governance_cycle | daily 07:00 UTC | 否 |
> | research_cycle | weekly Sunday | 否 |
> | decision_cycle | weekly | 否 |
> | 实盘交易引擎 | 实时 | 是 — 但走 OKX websocket, 不读 RDP 数据 |
>
> 结论: 没有任何 RDP 消费方需要 60s tick 频率, daemon 是为不存在的 use case 服务的.

**关键发现**: 这段论证在 2026-04-07 是对的, 但**路线 A research phase 0 的出现引入了一个新的 RDP 消费方 — microstructure signal 需要对照基线 OHLC**, 这不符合"所有消费方都是 daily/weekly"的原假设. 即 **p0c 的根因本质上是过期的 scheduling 假设**.

---

## §4 修复方案 (3 个选项)

### 选项 A — **把 candles rolling 拆成独立 15min workflow** (最对齐 microstructure)

**做法**: 新增 `configs/rdp_workflows/candles_rolling_15m.json`, schedule = custom every 15 min (照搬 `microstructure_silver_15m.json` 的模板), task 执行 `python scripts/rdp_run_daily_ingest.py --timeframes 15m`. `data_maintenance` 保留但职责缩减为每日 1h timeframe + gold 重建 + gap 检测 + index rebuild.

**利**:
- 直接让 `silver.market_*_candles_15m` 与 microstructure 5 张表同 cadence, 路线 A phase 0 对照基线零延迟
- 复用现有 `collect_candles_incremental` + checkpoint 机制, 代码零改动
- 失败策略 (`allow_failure=true`) 和 microstructure_silver_15m 一致, 单次失败不阻塞 daily 主链路
- 延续 2026-04-07 日批设计对 1m/1h tf 的资源节省 (每 15min 只拉 4 symbol × 1 tf = 4 次 REST 调用)

**弊**:
- 增加 OKX REST 调用频率: 4 symbols × 2 tf (15m + 1h 重建?) × 96 slot/day = ~768 calls/day; 相比日批的 ~8 calls/day 增加约 100×, 但远低于废弃的 daemon 7800 calls/day
- workflow 定义 +1 个, 运维监控面板要加一行

**工程量**: **S (小)**. ~2 小时:
- 新 json 配置 1 份 (30 行)
- `workflow_dispatcher` / scheduler 无需改动 (custom/interval_minutes 已支持, 见 `workflow_scheduler.py` line 190-197)
- `rdp_run_daily_ingest.py` 无需改动 (已支持 `--timeframes 15m` 参数)
- 单测: 加一个 scheduler 测试覆盖新 slot key
- 文档: 更新 `rdp_scheduling_strategy.md` 说明路线 A phase 0 消费方的存在

### 选项 B — **把 `data_maintenance` 的 schedule 从 daily 改成 hourly 或 15min**

**做法**: 修改 `configs/rdp_workflows/data_maintenance.json`, schedule 改为 `{ "frequency": "custom", "interval_minutes": 15 }` (或 hourly). 其他 task (gold + artifact_index) 继续跑.

**利**:
- 单文件 1 行改动, 工程量最小
- candles / funding / gold / gap / index 全部 15min 刷新, 一键实现全栈 intra-minute 观测
- 无新 workflow 概念, 运维习惯不变

**弊**:
- **资源浪费**: artifact_index_rebuild 每 15min 跑 96 次/天 是多余的 (索引变动低频). Gold replay bars 重建虽然每次 ~200ms, 但 96×4 symbol×2 tf = 768 次重建/天, Postgres I/O 和复杂度上升
- Gap 检测 lookback 24h 每 15min 检测一次, 会反复扫同一窗口; 单次 detection 里面的 `create_gap_repair_runs` 可能重复入队 gap_repair
- `allow_failure=false` 的 task 数量增加会把原本 "daily 偶发失败不影响研究" 的 SLA 压到 "15min 偶发失败触发告警风暴"
- 运维文档 `rdp_scheduling_strategy.md` 的"日批优于 daemon"论证需要大改 (语义倒退)

**工程量**: **XS (极小)**. 30 分钟. 但 soak test 后可能会反弹成 M.

### 选项 C — **让 microstructure workflow 按需自动触发 candles 补拉**

**做法**: 在 `scripts/rdp_build_microstructure_silver.py` 的 prerequisite 检查里加一段: 若当前 15m bar 对应的 `silver.market_{swap|spot}_candles_15m` 行不存在, 就 in-process 调用 `collect_candles_incremental(symbol, '15m', max_pages=1)` + `run_candle_merge_pipeline` 拉一下.

**利**:
- 严格的 "按需拉取" — 只在 microstructure 要用的时候才去补 candles, 彻底零浪费
- 把"consumer 驱动 producer"语义显式化, 未来再加 hourly / 1h 消费方时也走同样模式
- 无需新 workflow / 配置

**弊**:
- 把两条 pipeline 的 ownership 纠缠在一起: microstructure workflow 失败会携带 candles 故障, 排障边界变模糊
- `allow_failure=true` 的 microstructure 失败语义要重新定义: 是 candles 拉不到算失败, 还是只要 microstructure 计算出就 OK?
- Gap detection 和 Gold rebuild 仍留在 daily, 需要单独考虑 (或干脆放弃这些对路线 A 的即时性要求)
- **跨 workflow 边界写 checkpoint / ingest_runs 违反单一职责原则**, 且对 scheduler 的 "workflow X 对应 table Y" 心智模型破坏
- 对 OKX REST 会有 48×15min = 96 次 ×4 symbol / day, 数量级等同选项 A, 但没有 A 的清晰度

**工程量**: **M (中)**. ~1 天:
- `rdp_build_microstructure_silver.py` 入口逻辑 +50 行 (prerequisite 检查)
- 引入对 `candles_api_collector` 和 `merge_pipeline` 的依赖注入
- 单测: microstructure workflow 在 candles stale 时应触发补拉 + stale 检测 SLA + 双写幂等
- 回归测试: microstructure_silver_15m 原有 p95 < 10s 指标不能被破坏

---

## §5 Severity 定级

### 5.1 当前状态

| 维度 | 评级 | 理由 |
|---|---|---|
| 实盘交易影响 | **无** | 实盘交易引擎直连 OKX websocket, **不读** RDP 的 candles silver 层 (见 `docs/operations/rdp_scheduling_strategy.md` 原话) |
| 研究 pipeline 影响 | **无** | `research_cycle` weekly Sunday 自带数据刷新, 下次运行前会把 candles 拉到最新 |
| Phase 1 P1-D microstructure 影响 | **低** | microstructure 5 张 silver 表是独立 ETL, 不依赖 candles silver; 两者都是路线 A phase 0 的输入 |
| 数据损坏 | **无** | checkpoint / bronze / silver 全部自洽, 下次 ingest 正常接续 |
| 路线 A research phase 0 阻塞 | **是** | 需要 OHLC intra-day 新鲜度作为 microstructure signal 的对照基线 |

### 5.2 定级: **P1** (not P0)

**理由**:
- 不是 outage (系统按设计运行), 不是数据损坏, 实盘交易完全不受影响
- 但**阻塞即将开始的 "路线 A research phase 0"** — 这是确定的工作依赖, 不修不能推进
- 时效要求: 路线 A 启动前必须落地 (具体 deadline 看路线 A 的 SOW, 但不会今天就要)

### 5.3 是否阻塞路线 A research phase 0 — **是**

**判断依据**: 任务描述明确说"OHLC 作为 microstructure signal 的对照基线". 若 candles silver 停在 T-19h, 回归/对比分析的 ground truth 全部滞后 19h, alpha 稳定性和方向性分析会被 stale OHLC 污染. 除非路线 A phase 0 的 backtest 窗口只取 T-20h 以前的区间 (属于回测, 不需要实时新鲜度), 否则必须修.

---

## §6 不在本诊断范围

本诊断**不覆盖** (划清边界):

1. **microstructure_silver_15m 的 P0-b volume_profile_15m numeric overflow bug** — 在 `meta.ingest_runs` 里看到 2026-04-20 02:00 和 03:30 UTC 两次 `tables_failed=['volume_profile_15m']` numeric field overflow 错误, 但 10 分钟后被 gap_repair 补齐. 这是独立故障, 与 candles silver 停更无因果关系
2. **weekly research_cycle 的 2026-04-19 03:43 和 06:28 UTC 异常触发** — 明显不是 Sunday 08:00 UTC 的 cron, 属于手动/ad-hoc 触发, 但无人记录触发原因. 不影响本诊断结论
3. **修复方案 A/B/C 之间的 soak test / 生产流量对比** — 需要落地前单独做基准测试, 本报告只给工程量估算
4. **OKX REST 限流策略调整** — 选项 A/B/C 任一落地都会增加 REST 调用频率, 需不需要调整 `rolling_candles` 的 retry backoff 和 rate limiting, 本报告不涉及
5. **Gold 层 replay_bars 是否要同步升级到 15min cadence** — 取决于路线 A 是否直接消费 Gold bar, 暂不在诊断范围
6. **`rolling_candles_timeframes` 是否应扩展回 1m 或 5m** — 属于消费方需求分析, 本报告不替路线 A 做这个决定
7. **candles 与 funding 的 cadence 是否应保持一致** — funding 用 8h cadence 即可 (OKX 本身 funding 8h 一次), 不需要跟着 candles 升级

---

## 附录 A. 数据来源命令清单

```bash
# 数据库访问
wsl -d Ubuntu -- docker exec aats-postgres psql -U admin -d aats_research -c "..."

# 关键查询
-- 1.1 silver candles max(ts)
SELECT 'silver.market_spot_candles_15m' AS t, MAX(ts AT TIME ZONE 'UTC'), MAX(created_at AT TIME ZONE 'UTC'), COUNT(*)
FROM silver.market_spot_candles_15m
UNION ALL ...;

-- 1.3 checkpoint
SELECT * FROM meta.ingest_checkpoints WHERE dataset_domain='candles' AND timeframe='15m';

-- 1.4 ingest_runs 时间线
SELECT run_type, dataset_domain, status, symbol, timeframe, started_at AT TIME ZONE 'UTC', trigger_mode
FROM meta.ingest_runs WHERE dataset_domain='candles' AND started_at >= '2026-04-19 00:00:00+00'
ORDER BY started_at;

-- 1.5 task_queue 时间线
SELECT workflow, status, requested_at AT TIME ZONE 'UTC' FROM governance.rdp_task_queue
WHERE requested_at >= '2026-04-19 00:00:00+00' AND workflow IN ('data_maintenance','research_cycle')
ORDER BY requested_at;

# 日志访问
wsl -d Ubuntu -- docker logs --since 24h aats-rdp-daemon 2>&1 | grep -i candle

# 容器状态
wsl -d Ubuntu -- docker inspect aats-rdp-daemon --format '{{.State.StartedAt}} {{.State.Status}}'
```

## 附录 B. 相关文件引用

- `configs/rdp_workflows/data_maintenance.json` — schedule 配置
- `configs/rdp_workflows/microstructure_silver_15m.json` — 可供选项 A 复用的 15min template
- `scripts/rdp_run_daily_ingest.py` — ETL 实际入口
- `aats/data_platform/collectors/rolling/candles_api_collector.py` — OKX REST 拉取逻辑
- `aats/data_platform/merge/merge_pipeline.py::run_candle_merge_pipeline` — staging→bronze→silver 管道
- `aats/data_platform/merge/silver_merger.py::merge_candles_to_silver` — silver 层 UPSERT
- `aats/data_platform/operations/workflow_scheduler.py` — slot 计算, 见 `_latest_slot_for_schedule` L162-204
- `scripts/rdp_task_daemon.py` — 工作进程, `WORKFLOW_TIMEOUTS` 见 L52
- `docs/operations/rdp_scheduling_strategy.md` — 2026-04-07 日批设计依据
