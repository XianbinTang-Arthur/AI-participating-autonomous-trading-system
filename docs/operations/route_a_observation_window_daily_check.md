# 路线 A phase 0 — 7 天观察窗 Daily Check Runbook

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。

> **本文件定义 2026-04-20 ~ 2026-04-27 观察窗期间的每日健康检查协议, 为路线 A phase 0 research 启动前置条件.**

---

## 1. 背景

### 1.1 什么是路线 A phase 0 观察窗

**路线 A** (`docs/governance/alpha_evidence_gate.md` §8.1) = microstructure directional alpha 研究。

**Phase 0** = 第一轮 evidence 研究, 前提是 `silver.market_*_15m` 两条 pipeline (microstructure + candles) **连续 7 天稳定产出, 无 gap**.

### 1.2 起算点与终点

| 项 | 时间 (UTC) | 说明 |
|---|---|---|
| 观察窗起点 | **2026-04-20 14:15:00** | P0-a/b/c 全落地 + deploy + candles_rolling_15m 修 VALID_WORKFLOWS 白名单后, 两 pipeline 首次同 cadence 对齐的 tick |
| 观察窗终点 | **2026-04-27 14:15:00** | 起点 + 7 天 = 672 bars |

**若期间任一日 check 失败 (FAIL 级别), 观察窗重置, 起算点延后到问题解决当日**。

### 1.3 为什么需要 7 天

- microstructure signal (OFI / TFI / queue imbalance) 对 regime 敏感, 短样本容易 overfit 单一市场状态
- 7 天 × 24h × 4 bars/h = 672 个 15min bar, 最小样本量, 跨 Asia / Europe / US session 各 2 轮
- 覆盖至少 1 个完整 8h funding cycle 周期 × 3

### 1.4 观察窗期间不允许做的

严格纪律, 任一违反即视为观察窗重置:

1. **不改** `frozen_parameters.md` 冻结参数
2. **不改** `configs/active_parameter_sets/` 策略配置
3. **不改** `ai_operating_mode` (必须保持 `baseline_only`)
4. **不跑** 路线 A research code (只积累数据)
5. **不改** Silver ETL 逻辑 (除非明显 bug)
6. **不动** Prometheus / Grafana / alerting 核心配置 (修 scrape issue 例外, 独立 task)

---

## 2. Daily check 协议

### 2.0 DB matrix (operator 必读)

脚本查**两个 DB**, 各司其职:

| DB | 脚本用于 | 主要查询 |
|---|---|---|
| `aats_research` | Silver / Bronze 数据层 | `silver.market_*_15m` max(ts), gap detection, Bronze 健康 |
| `aats_live_derivatives` | Live runtime 事件层 | `public.event_store` 的 `strategy.decision_outcome` 查 `ai_operating_mode` |

对应的 bash helper:
- `psql_q ...` → `aats_research` (Silver / Bronze 数据)
- `psql_live ...` → `aats_live_derivatives` (runtime event store / decision outcomes)

若某检查改 DB, 两个 helper 不要混用; 错 DB 会因 schema 不存在静默返回空值, 脚本可能误判 PASS。

### 2.0.1 infra/查询失败语义 (2026-04-23 强化)

`psql_q` / `psql_live` 以及 check 1 的 `docker ps` 遇到 **wsl / docker / psql 任一环节非零退出** 时:

- 把 psql / docker stderr 摘要打到屏幕 (operator 能直接看到故障原因)
- 在 stdout 输出哨兵值 `__PSQL_ERR__` (psql helpers) / 单独走 rc 分支 (docker ps)
- 调用点通过 `is_psql_err`、rc 校验以及 "COUNT(*) 必为非负整数" 的形态校验识别故障, 直接 `fail`

这条纪律意味着:

- 任一数据源 (aats-postgres / wsl / docker) 不可用 → check 直接 FAIL, 不会被折叠成 "无数据 WARN" 或默认 0 PASS。
- check 5 / 6 的 `COUNT(*)` 查询返回空串 / 非数字 也一律视为 infra 异常 FAIL (psql 对 COUNT 查询必返回一行)。
- check 4 的 task queue 查询与 check 7 的 runtime mode 查询若 infra 挂掉, 不再退化成 "24h 全部 done PASS" 或 "无 decision_outcome WARN", 而是显式 FAIL。

即: daily check 的"数据源本身不可用" 一律按 FAIL 处理, 观察窗 reset, 先恢复 infra 再说。

### 2.1 运行命令

```bash
bash scripts/ops/route_a_daily_check.sh
```

### 2.2 推荐时机

- **每日 22:00 Shanghai** (= 14:00 UTC, 上一个 15min tick 刚跑完)
- 5 分钟以内完成
- 结果 **append** 到 `artifacts/route_a_observation_window/<YYYY-MM-DD>.log`

### 2.3 7 项 check (含 threshold justification)

| # | Check | Pass 条件 | 失败级别 |
|:-:|---|---|---|
| 1 | 16 个 `aats-*` 容器 healthy | 全部 `Status=healthy` | FAIL (任一不 healthy) |
| 2 | Silver 依赖链三表最新 bar < 30min | `market_trade_flow_15m` / `market_orderbook_metrics_15m` / `market_swap_candles_15m` 的 `max(ts)` 距 now 不超 30min | WARN 30-60min, FAIL > 60min |
| 3 | 三表 cadence 对齐 | 三表 `max(ts)` 极差 ≤ 15min (1 bar) | WARN > 15min |
| 4 | 24h task queue | rolling workflow 非 done 数 ≤ 2 | WARN 1-2, FAIL > 2 |
| 5 | 观察窗内 Silver gap count (三表各自) | 0 gap | WARN = 1, FAIL > 1 |
| 6 | Microstructure 24h empty-bar / no-data | `trade_flow_15m` 命中 `trades_no_data` 的行数 = 0 **且** `orderbook_metrics_15m` 同时命中 `orderbook_bbo_no_data`+`orderbook_books5_no_data` 的行数 = 0 | WARN 1-4, FAIL > 4 (每表独立判定) |
| 7 | Runtime mode 守门 | `ai_operating_mode=baseline_only` | FAIL 任何其他值 |

#### Check 2/3/5 为何看 `market_trade_flow_15m` (2026-04-23 升级)

`scripts/rdp_build_microstructure_silver.py::_detect_trade_flow_watermark` 以
`silver.market_trade_flow_15m` 的 `MAX(ts)` 作为 microstructure silver runner 的
watermark, watermark 不推进则整个 silver backfill 链停摆 (orderbook_metrics /
volume_profile / oi_funding / vol_weighted_tfi 都跟着落后)。因此
**trade_flow 新鲜度 = 整条 microstructure silver pipeline 的活性指示器**, 必须
和 orderbook_metrics / candles 一起纳入 check 2/3/5。

反过来, orderbook_metrics 新鲜不代表 trade_flow 新鲜 (bronze trades 可能瞬断
而 books5 还在, commit c331e2b "committed-but-empty bars" 正是此场景), 老脚本
只看 orderbook_metrics 会漏掉 watermark 停滞。

#### Check 6 为何扫 `quality_flags` 而非 freshness (2026-04-23 新增)

check 2/3/5 只能回答 "silver row 是否按时落地"。但 silver runner 遇 bronze 全空时
仍会 commit NULL/0 指标 + `quality_flags=['trades_no_data' / 'orderbook_*_no_data']`,
watermark 照推 (`COMMITTED_BUT_EMPTY`) —— 结果 check 2/3/5 全绿但 microstructure
input 实际已饿死。check 6 直接扫 `silver.market_trade_flow_15m.quality_flags`
和 `silver.market_orderbook_metrics_15m.quality_flags`, 是唯一能区分
"pipeline 还在转 / 输入是否真有内容" 的观测点。

#### Threshold 设计理由 (2026-04-20 code review C-M1 补)

| Check | 阈值 | 理由 |
|:-:|---|---|
| 2 | 30min WARN | cadence 是 15min bar. 1 bar 延迟 (= 15-30min age) 可能是 15min tick 执行窗口偏移; > 30min 说明至少错过 1 个 tick, 进 WARN 排查. |
| 2 | 60min FAIL | 连续错过 4 次 tick (= 60min/15min) 说明 pipeline 明显卡住, 观察窗失去"连续产出"前提, 需 reset. |
| 3 | 15min cadence diff | 三表设计上同 cadence (commit 15dd04e + scheduler 测试 test_candles_rolling_15m_slot_aligns_to_microstructure_cadence 锁定; trade_flow/orderbook_metrics 同 silver run 同 bar 写入). 极差 > 1 bar = 某条线落后 (脚本打印最落后表名), 影响 T-bar 对齐 + watermark 推进. |
| 4 | 2 次 WARN 阈值 | 24h = 96 个 15min tick, 2 / 96 ≈ 2.08% 容忍偶发网络抖动 / OKX REST 5xx. > 2 次说明系统性问题. |
| 4 | **未**区分 contiguous vs sparse | 简化 v0.1. 若观察窗期间发现"连续 2 次同 workflow failed" 更严重但本测试没抓, operator 需手工查 log_tail 判断. 留 v0.2 迭代点. |
| 5 | 1 gap WARN | 允许 1 次偶发数据源断 (OKX 维护 / 网络瞬断), UPSERT 幂等 + catchup 脚本能补. > 1 gap 需重置, 以免数据空洞污染 alpha 研究. |
| 6 | 1-4 bar WARN / >4 FAIL | 24h = 96 bar. silver runner 遇 bronze 全空会 commit "一行 NULL/0 + `*_no_data` quality_flags" 并推 watermark (`COMMITTED_BUT_EMPTY`, commit c331e2b), 于是 freshness/cadence/gap 三项全绿但输入其实饿死 —— 必须直接扫 `quality_flags` 才能抓到. ≤ 4 bar (≈ 1h) 容忍 collector 重启 / WS 断线自愈; > 4 bar (> 1h 连续饿死) 说明 bronze 上游系统性中断, 观察窗的 "连续 microstructure 输入" 前提挖空, 必须 reset. orderbook 要求 bbo + books5 双 flag 命中才计 (和 merger `_TABLE_NO_DATA_TRIGGERS['orderbook']` 对齐), 避免把 "单 source 缺, row 还有部分真实数据" 误报成饿死. |
| 7 | FAIL 任何其他值 | §2.4 要求观察窗期间 runtime mode 不可改. ai_assisted / ai_decision_maker 都触发 FAIL + 观察窗 reset 7 天. |

### 2.4 Exit code 语义

| Exit | 含义 | 观察窗处理 |
|:-:|---|---|
| 0 | 全 pass | 计数 +1 天 |
| 1 | 至少 1 个 WARN | 计数 +1 天, 但记录 WARN 原因 |
| 2 | 至少 1 个 FAIL | **观察窗重置**, 起算点延后到问题解决日 |

---

## 3. 失败应对

### 3.1 FAIL case 1: 容器不 healthy

可能原因:
- 重启循环 (OOM / crash)
- 健康检查 endpoint 超时
- 依赖服务 (Postgres / NATS / Redis) 挂了

**应对**:
- `wsl -d Ubuntu -- docker logs <container> --tail 50`
- 修复根因, 观察窗重置
- 绝不"重启看看" (掩盖真正问题)

### 3.2 FAIL case 2: Silver pipeline 断档

可能原因:
- rdp-daemon 挂了
- Bronze 源数据断 (collector 挂)
- Silver ETL bug (如 NUMERIC 溢出, 参考 P0-a)
- Scheduler timer drift

**应对**:
- 检查 rdp-daemon health + 日志
- 检查 Bronze 是否断
- 检查 `governance.rdp_task_queue` 看最近 task
- 用 `scripts/maintenance/microstructure_silver_catchup_20260420.py` 回填 gap (上次用过)
- 必要时开独立诊断 task

### 3.3 FAIL case 3: Microstructure 24h empty-bar 超阈值 (check 6 FAIL)

可能原因:
- bronze collector 容器挂 / 重启循环 (trades / books5 / bbo 任一断)
- OKX WS 连接断 (collector 侧 auto-reconnect 失败)
- OKX 维护窗口 > 1h
- Bronze 写 Postgres 失败 (磁盘满 / 权限)

**应对**:
- 立即看 market / collector 容器 healthy + 日志
- `SELECT ts, quality_flags FROM silver.market_trade_flow_15m ORDER BY ts DESC LIMIT 20` 看饿死区间
- 对照 `bronze.market_trades` / `bronze.market_orderbook_*` 同期是否真的空, 定位是 bronze 缺 or silver 误标
- 修根因后, 观察窗 reset, 起算点延后到 bronze 恢复当日 (和 FAIL case 2 处理一致)
- 不要用 backfill 把 empty bar 覆盖掉 — silver ETL 对 bronze 全空期幂等, 补不回真实 tick

### 3.4 FAIL case 4: Runtime mode 变了

**严重程度最高**. `ai_operating_mode` 从 `baseline_only` 切走意味着:
- 要么有人手动改了 `.env.*.live` (违纪)
- 要么 authority_map 被碰过 (frozen 违纪)
- 要么 event_store 数据损坏

**应对**:
- 立即告警, **不要**继续观察
- 查 `git log .env.derivatives.live configs/active_parameter_sets/` 和 decision_engine commit
- 找到切换者 + 还原
- 观察窗重置, 起算点延后 7 天

### 3.5 WARN case: 偶发 task 失败

rolling workflow 设计上 `allow_failure=true`, 单次失败由下一个 15min tick 自愈。24h 内 ≤ 2 次是正常容忍。

但若出现**连续 2 次以上同一 workflow 失败**, 即使没破 FAIL 阈值, operator 也要主动看 `log_tail` 找原因, 不能放任。

---

## 4. 通过观察窗后 (2026-04-27 14:15 UTC 之后)

若 **7 个连续 daily check 全 exit 0** (允许偶发 WARN), 满足以下额外条件即可正式启动路线 A phase 0:

1. 无 governance doc 新增修改 (`docs/governance/frozen_parameters.md` / `runtime_trading_mode_semantics.md` / `alpha_evidence_gate.md`)
2. 无 frozen 参数解冻 (除非走完 alpha evidence gate)
3. 用户 (= final sign-off) 明示批准启动

**启动形态**: 按 `docs/research/_templates/route_a_phase0_evidence_template.md` (本周内产出) 填第一个 `(feature × horizon)` 组合的 evidence 提案, 走 `alpha_evidence_gate` §3 的 4 条硬指标。

---

## 5. 归档

每日 check 结果 append 到:
```
artifacts/route_a_observation_window/
├── 2026-04-20.log
├── 2026-04-21.log
├── ...
└── 2026-04-27.log
```

观察窗结束后, 整批日志 commit 到 `docs/research/route_a_phase0/observation_window_logs/` 作为 evidence 提案的一部分 (Silver 稳定性的**第一手证据**)。

---

## 6. 自动化 (可选, 非必须)

若未来要自动化 (不用人工跑):
- Windows 任务计划: 每日 22:00 Shanghai 触发 `scripts/ops/route_a_daily_check.sh`
- 结果 email / 钉钉 通知 operator
- 异常 (exit 2) 即刻告警

**v0.1 (本文件) 不要求自动化**, 人工每日 5 min 跑一次为准, 保持 operator 对数据的感性认知。

---

## 7. 签署

- 起草: Claude Opus 4.7 · 2026-04-20
- 触发: 用户 2026-04-20 战略 directive "下周才允许开始 A 路线" 的观察窗前置条件
- 状态: v0.1, 2026-04-27 观察窗结束后归档, 若经验表明需调整 check 项则 v0.2 迭代
