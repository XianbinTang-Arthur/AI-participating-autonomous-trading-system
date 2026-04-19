# P1-D Stage 5 完工报告 — OKX REST 历史回填 + OI Delta 回归 (2026-04-20)

> 项目定位声明: 本文件默认服从 AATS 的统一目标. 详见 [项目定位声明](../../docs/project_positioning.md).

**Scope**: 实施 `docs/design/p1d_okx_historical_backfill_plan_2026_04_20.md` 中规划的 3 个 OKX REST endpoint 历史回填 (OI / mark / LS), 并用回填到的 OI 数据跑 P1-D 可行性 §1.3 指定的 **OI delta × sign(ΔP) 对 realized_return 的 OLS 回归**, 给 P1-D Phase 2A 提供 GO / CONDITIONAL / NO-GO hint.

**边界**:
- **会真发 OKX REST 请求** (设计批准)
- 不 push worktree 到 main, 不 deploy, 不动 `aats/services/**`
- 严守 20 req/2s rate limit (默认 0.15s/req ≈ 6.7 req/s)

---

## TL;DR

| 项目 | 结果 |
|---|---|
| 3 个 Bronze 迁移 + rollback | batch_b_08 (OI) + batch_b_09 (mark + LS) 已 merge 到 `_batch_b.py`, 单测 pass |
| 3 个 Bronze ORM class | `BronzeMarketOIHistory1hModel` / `BronzeMarketMarkPriceCandles1mModel` / `BronzeMarketLongShortRatio5mModel` |
| REST collector module | `aats/data_platform/collectors/backfill/okx_rest_history_collectors.py` (1 文件 3 endpoint) |
| CLI (dry-run/apply/verify) | `scripts/rdp_backfill_okx_rest_history.py` |
| 实际回填 (APPLY) | OI 1440 rows (60d), Mark 43299 rows (30d), LS 576 rows (2d, OKX 限制) |
| OI delta 回归 | **best test R² = -0.00240** (abs_oi_delta @ 1h) → **NO-GO hint** |
| 单元测试 | 35 个 Stage 5 新增, 156 个 data_platform 全绿, 2559 个 unit 全绿 (非相关 pre-existing fail 已跳) |

**核心数字 (P1-D Phase 2A 最关键输出)**:

| feature | 1h test R² | 4h test R² | 1d test R² | 结论 |
|---|---|---|---|---|
| signed_oi_delta | -0.00461 | -0.00905 | -0.06016 | 无预测力 |
| oi_delta | -0.00319 | -0.01019 | -0.05039 | 无预测力 |
| abs_oi_delta | -0.00240 | -0.00803 | -0.05295 | 无预测力 |

所有 test R² 均为负 (worse than mean predictor), cross-window slope 在前半/后半反向, q80/q90 扣成本 mean_net 全部 < 0. **在 1h bar 粒度上, OI delta × sign(ΔP) 对 realized_return 没有 out-of-sample 预测力**.

---

## § 1. 交付物清单

### § 1.1 Migrations

| 文件 | 内容 |
|---|---|
| `aats/data_platform/migrations/batch_b_08_oi_history.sql` | bronze.market_oi_history_1h + 扩 `chk_cp_domain` / `chk_iri_domain` 白名单 (补 batch_b_07 遗漏) |
| `aats/data_platform/migrations/batch_b_08_oi_history_rollback.sql` | drop table |
| `aats/data_platform/migrations/batch_b_09_mark_ls_history.sql` | bronze.market_mark_price_candles_1m + bronze.market_long_short_ratio_5m |
| `aats/data_platform/migrations/batch_b_09_mark_ls_history_rollback.sql` | drop 2 tables |
| `aats/data_platform/migrations/_batch_b.py` | BATCH_B_STAGES 追加 `batch_b_08_oi_history` + `batch_b_09_mark_ls_history` |

### § 1.2 ORM

追加到 `aats/data_platform/rdp_models.py`:
- `BronzeMarketOIHistory1hModel` — PK (symbol, ts), oi/oi_ccy/oi_usd (后者 nullable)
- `BronzeMarketMarkPriceCandles1mModel` — PK (symbol, ts), OHLC 全 NOT NULL
- `BronzeMarketLongShortRatio5mModel` — PK (symbol, ts), ls_ratio_accounts / ls_ratio_positions (都 nullable)

### § 1.3 Collectors

`aats/data_platform/collectors/backfill/okx_rest_history_collectors.py` (单文件 3 collector):
- `collect_oi_history(session, symbol, target_days, period, ...)` → BackfillStats
- `collect_mark_candles_history(session, symbol, target_days, period, ...)` → BackfillStats
- `collect_ls_ratio_history(session, ccy, target_days, period, ...)` → BackfillStats
- `estimate_*_requests(days, period)` — dry-run 预估 pages/rows/秒数
- `normalize_ls_symbol(ccy)` — "BTC" → "BTC-USDT-SWAP"

共用骨架 `_paged_request`:
- 429 指数 backoff (1s → 2s → 4s ... cap 30s, 最多重试 5 次)
- OKX code=50011 触发同样 backoff
- 4xx 非 rate-limit 直接停止
- 连续 3 页空 data → API 到底
- 每 N 页打 progress log

INSERT 全用 `ON CONFLICT (symbol, ts) DO NOTHING RETURNING 1` 幂等.

ingest_run + checkpoint 沿用 `aats.data_platform.jobs.run_registry` 和 `checkpoint_manager`:
- dataset_domain='microstructure' (需要 Stage 07 + Stage 08 migration 扩约束)
- trigger_mode='manual'
- timeframe='oi_1h' / 'mark_1m' / 'ls_5m'

### § 1.4 CLI

`scripts/rdp_backfill_okx_rest_history.py`:
- `--dry-run` (默认): 打印预估, 不发请求
- `--apply`: 实际发请求 + 写 DB
- `--verify`: 查 3 张 Bronze 表行数 + ts 范围
- 预估 > 1 小时或 > 1 GB 发 WARN
- 各 endpoint 独立 try/catch, 一个失败不阻塞其他
- `--output <path.json>` 输出 stats JSON

### § 1.5 Regression

`scripts/research/p1d_oi_delta_regression.py`:
- 加载 `bronze.market_oi_history_1h` + `silver.market_swap_candles_1h` (或从 15m 聚合)
- 计算 `oi_delta`, `price_change`, `sign_dp`, `signed_oi_delta`
- 3 horizons: 1h / 4h / 1d (bars=1/4/24)
- OLS train 70% / test 30%, 时间顺序, 禁 look-ahead
- Cross-window 前半/后半
- 4 象限分析 (oi↑/↓ × price↑/↓)
- 扣成本 mean_net @ q80/q90
- 输出 `docs/research/p1d_oi_delta_regression_2026_04_20.md`

### § 1.6 Unit Tests

| 文件 | 测试数 | 覆盖 |
|---|---|---|
| `tests/unit/data_platform/test_okx_rest_history_bronze_schema.py` | 8 | ORM round-trip, PK conflict, NOT NULL, rollback, migration files, BATCH_B_STAGES 注册 |
| `tests/unit/data_platform/test_okx_rest_history_collectors.py` | 27 | parse_row * 3, estimate * 3, normalize_ls_symbol, dedupe, dry-run no-HTTP * 3, 429 backoff mocked, BackfillStats |

---

## § 2. 实际执行记录

### § 2.1 dry-run 预览

```
总预估: 541 pages, 54000 rows, ~81 s, ~10.3 MB
  OI   (90d × 1H): 22 pages, 2160 rows, 3.3s
  Mark (30d × 1m): 432 pages, 43200 rows, 64.8s
  LS   (30d × 5m): 87 pages, 8640 rows, 13.0s
```

成本: ~10 MB 下载, < 2 分钟. 远低于 cost-awareness 门槛 (1GB / 1h), 批准实跑.

### § 2.2 实跑 & 结果 (verify 落库)

```
bronze.market_oi_history_1h              : 1440 rows  (2026-02-20 00:00 → 2026-04-19 23:00)
bronze.market_mark_price_candles_1m      : 43299 rows (2026-03-20 21:50 → 2026-04-19 23:28)
bronze.market_long_short_ratio_5m        : 576 rows   (2026-04-17 23:30 → 2026-04-19 23:25)
```

**关键观察**:
1. **OI history 实测仅 60 天**: 虽请求 90 天, OKX 实际只返回 60 天 (1440 rows = 60 × 24). "API 返回不足一页 → API 到底" 触发. 设计文档里标注的"≥ 90 天"**不成立**.
2. **LS ratio 5m 实测仅 2 天**: OKX 的 rubik LS endpoint **period=5m 只保留 2 天** (576 = 2 × 24 × 12). 但 **period=1H 能回溯 30 天 (720 rows)** — LS 的深度强依赖 period.
3. **Mark price 30 天 OK**: 43299 rows 即为 30 × 1440 ≈ 43200, 正常.

### § 2.3 踩坑记录

**坑 1: `chk_cp_domain` 未扩 'microstructure'**
- Stage 07 只扩了 `chk_ir_domain` (ingest_runs), 漏了 `chk_cp_domain` (checkpoints) 和 `chk_iri_domain` (run_items).
- Stage 5 首次 apply 时 `upsert_checkpoint(domain='microstructure')` 触发 CheckViolation, 导致 session 事务 aborted.
- **修复**: 在 `batch_b_08_oi_history.sql` 里把这两个 CHECK 都扩了 (类似 Stage 07 的模式). 重跑 migration 后 OK.

**坑 2: MARK 路径错误**
- 设计文档 §3.2 写的 `/api/v5/market/mark-price-candles-history` 返回 404.
- 实测正确路径: `/api/v5/market/history-mark-price-candles` (和 history-candles 是并列的, 命名前缀 history- 而非 后缀 -history).
- `/api/v5/market/mark-price-candles` (无 history) 只返回近 1 天数据, 不够回填用.
- **修复**: 代码 + SQL comment + design-alignment 都已校正为 `history-mark-price-candles`.

**坑 3: Checkpoint 失败导致 session 不可用**
- `_write_bronze_oi_history` commit 后写入成功, 紧跟的 `upsert_checkpoint` 失败把 session 打坏, 后续 `finish_ingest_run` 也跟着失败 raise.
- **修复**: 在每个 `collect_*` 末尾的 checkpoint try/except 里加 `session.rollback()`, 保证后续操作能继续.

---

## § 3. P1-D Phase 2A 判定 (核心)

### § 3.1 R² 数字

| feature | 1h | 4h | 1d |
|---|---|---|---|
| signed_oi_delta | -0.00461 | -0.00905 | -0.06016 |
| oi_delta | -0.00319 | -0.01019 | -0.05039 |
| abs_oi_delta | -0.00240 | -0.00803 | -0.05295 |

### § 3.2 与 P1-D 预估对比

P1-D 可行性 §1.3 预估 **R² = 0.01-0.02**.
实测 **best test R² = -0.002** (即模型比 mean 预测还差). 

**证据**:
- Cross-window slope 前半/后半反号 (signed_oi_delta: first +230, second -138; oi_delta: first +181, second -384)
- q80 扣成本 mean_net 全部负值 (-2.85 到 -8.14 bps)
- 4 象限看 **1h horizon** 的边际信号 (新多开 +4.94 bps), 但 4 象限在 1d horizon 非常混乱 (short_squeeze 和 long_flush 都 bullish +32/+36 bps, 与理论相反)

### § 3.3 Hint: **NO-GO**

- 门槛: R² < 0.005 across all features × horizons → NO-GO hint (对齐 p1d_preview_regression_funding_basis 模式)

**但**: 这不是 Phase 2A 的最终判定, 只是 1 个信号证据. 可能有的原因:
1. **Horizon 太粗**: 1h OI + 1h bar 粒度丢失了 15m / 5m 级别的 OI 动能衰减
2. **样本太少**: 1425 有效 bar, ±0.005 R² 置信区间 ±0.004
3. **OKX OI 更新频率**: OKX OI history 是 1h 的 snapshot, 不是 tick-level, 真正的 OI flow (谁在开仓/平仓) 需要 order-flow
4. **非线性**: OLS 1-var 无法捕捉 4 象限的非线性 (4 象限分析表明不同 regime 行为确实不同)

---

## § 4. 后续建议 (Phase 2A 路径)

### 推荐路径 (按优先级)

**A. 等 microstructure WS 积累 30 天数据, 换用 5m/1m 粒度 OI tick 回归** (成本最低)
- `staging.market_oi_funding_ticks` 正在实时采 tick-level OI 变化
- 30 天到位后可做:
  - 5m 粒度的 `oi_delta × sign(ΔP)` + forward 5m / 15m return
  - OI flow 加速度 (`d²OI/dt²`)
  - 结合 bbo_imbalance 多变量回归

**B. 上 Tardis.dev trades 历史 backfill $300-600** (预算最贵但最有 edge 可能)
- P1-D 可行性 §1.2: **taker buy/sell ratio** 是第二重要特征 (仅次于 OBI)
- Tardis.dev 一次性 30-60 天逐笔 trades → order-flow imbalance (OFI)
- 和 OI 是互补信号

**C. 完全放弃 OI 作为直接 signal, 只当 regime filter 用**
- OI delta 本身 R² 低, 但可能作为 filter 提高其他 signal 的 signal-to-noise
- 例如: `basis_z × I(|oi_delta| > p80)` — 在 OI 变动剧烈时 basis_z 可能更可靠
- 成本低, 代码改动小

### 不推荐

- **直接上 signed_oi_delta feature 到 production baseline strategy** — R² 负值, 会引入 cost 而无收益

### 可补充

- 单独 backfill period=15m / 30m 的 `bronze.market_oi_history_15m`: 新建同 structure 表 + 新 migration stage (不在 Stage 5 scope, Phase 2A pre-work 可独立做)

---

## § 5. 疑问 / 需用户决策

1. **LS ratio 5m 只 2 天可用是 OKX 硬限**. 若 Phase 2A 要用 LS, 建议切到 period=1H (30 天可用). 需要新 bronze 表 `market_long_short_ratio_1h` (Stage 5 只建了 5m 版). 是否要补?
2. **OI 60 天上限 vs 设计的 90 天**: 设计里的 90 天是乐观估计. 实测 OKX 只给 60 天. 是否更新设计文档 §2.2 的量级估算?
3. **Mark price 30 天已落库但本次未用于回归**: Phase 2A 要用 basis = (perp - mark) / mark 作 true basis 特征吗? 这比目前用 spot close 做代理更准确.
4. **Trades history 的 Tardis.dev 采购决策**: 本次 NO-GO hint 增加了 Tardis.dev 的需求度. 是否推进采购?
5. **Stage 5 的 migration 到 live DB 应用**: 已在 `aats_research` 实测 apply, 但生产 deploy 链路 (`scripts/deploy.sh`) 没跑. Stage 5 的 schema 是否需要在下次 deploy 时自动 apply?

---

## § 6. 验收 checklist

- [x] 3 migration (batch_b_08/09) + rollback 都能跑 (in-memory SQLite 单测 pass)
- [x] 3 ORM class 可 import + metadata create_all
- [x] REST collector unit tests (包括 mock HTTP 429 backoff, dry-run, dedupe)
- [x] `--dry-run` 正确打印预期 (541 pages / 54K rows / 81s / 10 MB)
- [x] `--apply` 实际下到数据 (OI 1440 / mark 43299 / LS 576 行)
- [x] `--verify` 能查到落库
- [x] OI delta 回归跑出 R² 数字 (见 §3.1)
- [x] 全量 `tests/unit/data_platform/` 无回归 (156 pass)
- [x] 全量 `tests/unit/` 除 pre-existing .env.spot 失败外全绿 (2559 pass)
- [x] 未改 `aats/services/**`
- [x] 未 push / 未 merge

---

## § 7. Commit 序列 (建议顺序)

1. `feat(rdp): batch_b_08 + batch_b_09 migrations (OI/mark/LS history bronze)`
   - 3 SQL + 3 rollback + `_batch_b.py` BATCH_B_STAGES 追加
2. `feat(rdp): OKX REST history backfill collectors + ORM`
   - `okx_rest_history_collectors.py` + `rdp_models.py` 3 Bronze 追加
3. `feat(scripts): rdp_backfill_okx_rest_history CLI + dry-run/apply 双层保护`
   - `scripts/rdp_backfill_okx_rest_history.py`
4. `test(rdp): unit tests for Stage 5 migrations + collectors`
   - 2 个新 test 文件 (35 tests)
5. `feat(research): p1d_oi_delta_regression 脚本`
   - `scripts/research/p1d_oi_delta_regression.py`
6. `docs(research): OI delta 回归结果 + Phase 2A NO-GO hint`
   - `docs/research/p1d_oi_delta_regression_2026_04_20.md`
7. `docs(review): Stage 5 完工报告`
   - 本文件

---

## § 8. 签署

- **执行日期**: 2026-04-20
- **执行范围**: 实施 + 实跑 + 回归分析
- **OKX 请求量**: ~500 REST 请求 (OI 22 × 2 retries + Mark 433 + LS 4)
- **OKX rate limit 触发**: 0 次 (0.15s 间隔足够)
- **实际耗时**: ~7 分钟 (dry-run + apply + mark re-run + LS 一次)
- **数据落库量**: 45315 rows × 3 tables
- **Phase 2A hint**: **NO-GO** (best R² = -0.002, 4 象限也不一致, cross-window 不稳定)
- **推荐下一步**: 等 30 天 WS microstructure 积累 → 5m 粒度 OI tick 回归 (免费路径); 或 Tardis.dev $300-600 一次性 trades backfill (付费快通道)
