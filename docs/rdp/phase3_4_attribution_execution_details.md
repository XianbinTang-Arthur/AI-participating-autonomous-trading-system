# Phase 3-4 归因与执行可行性 — 详细参考

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


> 本文档从 README 抽出，包含 Phase 3/4 的完整技术细节。
> 概览请参阅 [README § 21.6](../../README.md)。

## Phase 3: Live Attribution / Replay 对照归因

Phase 3 建立标准化 replay vs live 对照归因流程，回答"为什么 live 没下单"。

### 核心模块（`aats/data_platform/attribution/`）

| 模块 | 职责 |
|------|------|
| `taxonomy.py` | 统一归因分类 + reason code（10 个 category, 30+ reason code） |
| `alignment.py` | Replay/live 事件按 bar 时间窗口对齐 + live DB 查询 |
| `layer_classifier.py` | 瀑布式分层归因（停在第一层失败处） |
| `aggregation.py` | 按 category × reason 聚合 + top failure modes + layer analysis |
| `report_builder.py` | Markdown 报告生成（单次 + 批量结论） |

### 归因瀑布（严格顺序）

```text
1. Strategy  → replay 想开, live strategy 也想开?
2. Permission → automatic_enabled = true?
3. Allocator → allocation 存在且 approved?
4. Budget   → budget_multiplier > 0, 未被 clamp?
5. Risk     → reconciliation 未阻止? (not only_reduce / halt)
6. Execution → bundle 状态正常?
7. Order    → order 创建且未 rejected?
8. Fill     → fill 出现?
```

### 运行命令

**One-shot Attribution**（`rdp_run_live_attribution.py`）：

```bash
# 对单个 family/timeframe 做 replay/live 归因
python scripts/rdp_run_live_attribution.py \
    --family independent --symbol BTC-USDT-SWAP --timeframe 15m \
    --start 2026-03-31 --end 2026-04-02 \
    --live-db-url "postgresql+psycopg://localhost:5432/aats_derivatives"

# 仅做 replay 分析（无 live DB）
python scripts/rdp_run_live_attribution.py \
    --family independent --symbol BTC-USDT-SWAP --timeframe 15m \
    --start 2026-03-31 --end 2026-04-02 --replay-only
```

**Phase 3 Round**（`rdp_run_phase3_round.py`）：

```bash
# 批量跑 4 个 family×tf 组合
python scripts/rdp_run_phase3_round.py \
    --start 2026-03-31 --end 2026-04-02
```

### 产物结构

```text
artifacts/research/attribution_rounds/<round_id>/
  round_manifest.json
  family_timeframe_attribution_summary.csv
  phase3_live_attribution_conclusion.md
  per_combo/
    independent_15m_<ts>/
      replay_live_alignment.csv
      attribution_summary.json
      top_failure_modes.json
      live_attribution_report.md
    independent_1h_<ts>/...
    directional_15m_<ts>/...
    directional_1h_<ts>/...
```

### Live 数据源（只读，不修改）

| 表 | 归因层 |
|----|--------|
| `strategy_sleeve_intents` | Strategy / Permission |
| `portfolio_allocation_decisions` | Allocator |
| `allocator_budget_snapshots` | Budget |
| `reconciliation_state_snapshots` | Risk |
| `strategy_execution_bundles` | Execution |
| `execution_orders` | Order |
| `execution_fills` | Fill |

---

## Phase 4: Execution Realism / 成交可行性研究

Phase 4 进入市场微观结构层，回答"即使策略想开单，这笔单在真实市场条件下是否可成交、成本多少"。

### 核心模块（`aats/data_platform/execution_realism/`）

| 模块 | 职责 |
|------|------|
| `market_alignment.py` | 候选订单与 Gold bar 市场快照对齐（OHLCV + volume 匹配） |
| `fill_feasibility.py` | 基于 volume ratio 的可成交性评估（fully/partially/not fillable） |
| `slippage_estimator.py` | Bar-proxy 滑点模型（half-spread + sqrt volume impact） |
| `execution_cost_model.py` | 执行成本汇总（slippage + fee，与 Phase 2 默认假设比较） |
| `aggregation.py` | 跨 family/timeframe 比较聚合 + 交叉发现 |
| `report_builder.py` | Markdown 报告生成（单次 + Phase 4 结论） |

### V1 分析链

```text
Replay Decision (action=open/close)
  -> Gold Bar Matching (同一 bar timestamp)
  -> Fill Feasibility (volume_ratio < 1%? → fully_fillable)
  -> Slippage Estimate (half_spread + sqrt_impact)
  -> Total Cost = slippage + taker_fee
  -> Cost-Adjusted Edge = net_edge + assumed_cost - realistic_cost
```

### V1 滑点模型（Bar-Based Proxy, 透明可解释）

- Half-spread: `max(0.5 bps, bar_range_bps × 0.02)`
- Volume impact: `bar_range_bps × sqrt(volume_ratio)` (square root law)
- 参数可通过真实 orderbook/trades 数据校准

### 运行命令

**One-shot Execution Realism**（`rdp_run_execution_realism.py`）：

```bash
# 对单个 family/timeframe 做 execution realism 分析
python scripts/rdp_run_execution_realism.py \
    --family independent --symbol BTC-USDT-SWAP --timeframe 15m \
    --start 2026-03-31 --end 2026-04-02

# 指定 taker fee
python scripts/rdp_run_execution_realism.py \
    --family independent --symbol BTC-USDT-SWAP --timeframe 15m \
    --start 2026-03-31 --end 2026-04-02 --taker-fee-bps 3.0
```

**Phase 4 Round**（`rdp_run_phase4_round.py`）：

```bash
# 批量跑 4 个 family×tf 组合
python scripts/rdp_run_phase4_round.py \
    --start 2026-03-31 --end 2026-04-02
```

### 产物结构

```text
artifacts/research/execution_rounds/<round_id>/
  round_manifest.json
  execution_realism_comparison.csv
  phase4_execution_realism_conclusion.md
  per_combo/
    independent_15m_<ts>/
      execution_alignment.csv
      fill_feasibility_summary.csv
      slippage_summary.csv
      execution_cost_summary.json
      live_execution_realism_report.md
    independent_1h_<ts>/...
    directional_15m_<ts>/...
    directional_1h_<ts>/...
```

### V1 数据源与限制

| 数据 | V1 使用 | 后续升级 |
|------|---------|---------|
| 价格 | Gold bar close | Orderbook mid-price |
| 流动性 | Bar volume (contracts) | Orderbook depth levels |
| Spread | Bar range × 0.02 proxy | Real best bid/ask spread |
| Impact | sqrt(volume_ratio) model | Trades-based calibration |
| 仓位 | 1 contract (0.01 BTC) | Portfolio-level sizing |
