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

### 2.1 运行命令

```bash
bash scripts/ops/route_a_daily_check.sh
```

### 2.2 推荐时机

- **每日 22:00 Shanghai** (= 14:00 UTC, 上一个 15min tick 刚跑完)
- 5 分钟以内完成
- 结果 **append** 到 `artifacts/route_a_observation_window/<YYYY-MM-DD>.log`

### 2.3 6 项 check (含 threshold justification)

| # | Check | Pass 条件 | 失败级别 |
|:-:|---|---|---|
| 1 | 16 个 `aats-*` 容器 healthy | 全部 `Status=healthy` | FAIL (任一不 healthy) |
| 2 | Silver 两 pipeline 最新 bar < 30min | `market_orderbook_metrics_15m` / `market_swap_candles_15m` 的 `max(ts)` 距 now 不超 30min | WARN 30-60min, FAIL > 60min |
| 3 | Micro / candles cadence 对齐 | 两表 `max(ts)` 差 ≤ 15min (1 bar) | WARN > 15min |
| 4 | 24h task queue | rolling workflow 非 done 数 ≤ 2 | WARN 1-2, FAIL > 2 |
| 5 | 观察窗内 Silver gap count | 0 gap | WARN = 1, FAIL > 1 |
| 6 | Runtime mode 守门 | `ai_operating_mode=baseline_only` | FAIL 任何其他值 |

#### Threshold 设计理由 (2026-04-20 code review C-M1 补)

| Check | 阈值 | 理由 |
|:-:|---|---|
| 2 | 30min WARN | cadence 是 15min bar. 1 bar 延迟 (= 15-30min age) 可能是 15min tick 执行窗口偏移; > 30min 说明至少错过 1 个 tick, 进 WARN 排查. |
| 2 | 60min FAIL | 连续错过 4 次 tick (= 60min/15min) 说明 pipeline 明显卡住, 观察窗失去"连续产出"前提, 需 reset. |
| 3 | 15min cadence diff | 两 pipeline 设计上同 cadence (commit 15dd04e + scheduler 测试 test_candles_rolling_15m_slot_aligns_to_microstructure_cadence 锁定). 差 > 1 bar = 某条线落后, 影响 T-bar 对齐. |
| 4 | 2 次 WARN 阈值 | 24h = 96 个 15min tick, 2 / 96 ≈ 2.08% 容忍偶发网络抖动 / OKX REST 5xx. > 2 次说明系统性问题. |
| 4 | **未**区分 contiguous vs sparse | 简化 v0.1. 若观察窗期间发现"连续 2 次同 workflow failed" 更严重但本测试没抓, operator 需手工查 log_tail 判断. 留 v0.2 迭代点. |
| 5 | 1 gap WARN | 允许 1 次偶发数据源断 (OKX 维护 / 网络瞬断), UPSERT 幂等 + catchup 脚本能补. > 1 gap 需重置, 以免数据空洞污染 alpha 研究. |
| 6 | FAIL 任何其他值 | §2.4 要求观察窗期间 runtime mode 不可改. ai_assisted / ai_decision_maker 都触发 FAIL + 观察窗 reset 7 天. |

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

### 3.3 FAIL case 3: Runtime mode 变了

**严重程度最高**. `ai_operating_mode` 从 `baseline_only` 切走意味着:
- 要么有人手动改了 `.env.*.live` (违纪)
- 要么 authority_map 被碰过 (frozen 违纪)
- 要么 event_store 数据损坏

**应对**:
- 立即告警, **不要**继续观察
- 查 `git log .env.derivatives.live configs/active_parameter_sets/` 和 decision_engine commit
- 找到切换者 + 还原
- 观察窗重置, 起算点延后 7 天

### 3.4 WARN case: 偶发 task 失败

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
