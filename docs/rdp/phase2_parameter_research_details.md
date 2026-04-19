# Phase 2 参数研究平台 — 详细参考

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


> 本文档从 README 抽出，包含 Phase 2 的完整技术细节。
> 概览请参阅 [README § 21.5](../../README.md)。

## 1. 架构总览

```text
Gold replay bars
  -- Replay Core（逐 bar 重放引擎）
  -- Strategy Adapter（independent / directional 家族适配器）
  -- Edge Contract（统一 4 层 edge 分解语义）
  -- Cost Model（可配置的保守成本模型）
  -- Diagnostics Engine（结构化诊断指标，含 edge 分解统计）
  -- Experiment Registry（实验元数据、产物路径追踪）
  -- Parameter Scan Engine（参数网格批量扫描，支持 partial_success 状态）
  -- Report Builder（Markdown / JSON / CSV 报告，含 edge 来源分析）
```

## 2. 统一 Edge Contract

所有 family adapter 必须按以下 4 层分解输出 edge（bps 单位）：

```text
expected_net_edge_bps = signal_edge_proxy_bps + funding_adjustment_bps - cost_bps
```

| 层 | 字段 | 说明 |
|----|------|------|
| Signal | `signal_edge_proxy_bps` | 来自策略信号（score / momentum / trend / alpha）的机会代理 |
| Funding | `funding_adjustment_bps` | funding rate 的附加调整（附加项，不是全部） |
| Cost | `cost_bps` | 交易成本（taker fee + slippage，来自 `ReplayCostConfig`） |
| Net | `expected_net_edge_bps` | 最终净 edge = signal + funding - cost |

两个 family 的 signal 内部估算方式可以不同，但输出语义统一，横向对比有效。

## 3. 成本模型

成本配置集中在 `ReplayCostConfig`，不硬编码在 adapter 里：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `taker_fee_bps` | 5.0 | OKX swap taker 0.05%，保守估计 |
| `slippage_bps` | 2.0 | 保守滑点估计 |
| **total_cost_bps** | **7.0** | 单边成本合计 |

可通过 `--param taker_fee_bps=3 --param slippage_bps=1.5` 直接覆盖。

## 4. 可覆盖参数

所有参数均可通过 CLI `--param key=value` 覆盖：

| 参数 | 类别 | 说明 | 默认值 |
|------|------|------|--------|
| `min_confirm_ticks` | 策略门槛 | 信号确认强度 | 2 |
| `score_stability_threshold` | 策略门槛 | 强信号是否被过度拦截 | 2.0 |
| `min_safe_net_edge_bps` | 策略门槛 | 边缘机会放行下限 | 0.0 |
| `signal_edge_scale_bps` | 信号校准 | score -> bps 缩放系数 | 10.0 |
| `directional_trend_weight` | 信号校准 | directional 趋势/return 混合权重 | 0.7 |
| `directional_return_clamp_bps` | 信号校准 | directional bar return 限幅 | 20.0 |
| `taker_fee_bps` | 成本模型 | taker 手续费（bps） | 5.0 |
| `slippage_bps` | 成本模型 | 滑点（bps） | 2.0 |

## 5. 运行方式

```powershell
# 单次 replay 实验
python scripts/rdp_run_replay.py \
    --family independent \
    --symbol BTC-USDT-SWAP \
    --timeframe 1m \
    --start 2026-03-31 --end 2026-04-02 \
    --dataset-version v1.0 \
    --param min_confirm_ticks=3 \
    --param min_safe_net_edge_bps=5

# 覆盖成本模型
python scripts/rdp_run_replay.py \
    --family independent \
    --symbol BTC-USDT-SWAP --timeframe 1m \
    --start 2026-03-31 --end 2026-04-02 \
    --param taker_fee_bps=3 --param slippage_bps=1.5

# 覆盖 signal edge 校准参数
python scripts/rdp_run_replay.py \
    --family directional \
    --symbol BTC-USDT-SWAP --timeframe 1m \
    --start 2026-03-31 --end 2026-04-02 \
    --param signal_edge_scale_bps=15 \
    --param directional_trend_weight=0.8

# 参数网格扫描（默认 3x3x3 = 27 组合）
python scripts/rdp_run_parameter_scan.py \
    --family independent \
    --symbol BTC-USDT-SWAP \
    --timeframe 1m \
    --start 2026-03-31 --end 2026-04-02

# 自定义参数网格
python scripts/rdp_run_parameter_scan.py \
    --family independent \
    --symbol BTC-USDT-SWAP \
    --timeframe 1m \
    --start 2026-03-31 --end 2026-04-02 \
    --grid '{"min_confirm_ticks":[2,3],"min_safe_net_edge_bps":[0,5,10]}'
```

注：replay / scan CLI 默认不跑 migration，需加 `--ensure-schema` 显式执行。

## 6. 产物结构

每次实验生成三个文件：

```text
artifacts/research/experiments/<experiment_id>/
  replay_decisions.csv    -- 逐 bar 决策明细（含 edge 4 层分解）
  diagnostics.json        -- 诊断指标快照（含 edge 分解统计）
  report.md               -- Markdown 研究报告（含 Edge Breakdown 表格）
```

参数扫描额外生成：

```text
artifacts/research/experiments/<scan_run_id>/
  comparison_summary.json -- 多组实验对比数据（含 edge 分解）
  comparison_report.md    -- 对比报告
  failed_combos.json      -- 失败组合明细（label + params + error）
  <experiment_id>/        -- 每组参数各自的产物
```

## 7. 诊断指标

| 指标 | 说明 |
|------|------|
| `opening_count` | 触发开仓次数 |
| `blocked_count` | 通过评分阈值但被门槛拦截的次数 |
| `selectable_ratio` | 评分达到入场阈值的 bar 占比 |
| `execution_compatible_ratio` | 同时满足评分+稳定性+边际的 bar 占比 |
| `top_blocking_reasons` | 拦截原因 Top N 排名 |
| `mean_signal_edge_proxy_bps` | 平均信号代理 edge（bps） |
| `mean_funding_adjustment_bps` | 平均 funding 调整（bps） |
| `mean_cost_bps` | 平均成本（bps） |
| `mean_expected_edge_bps` | 平均预期净边际（bps） |
| `positive_edge_ratio` | 净 edge 为正的 bar 占比 |
| `state_distribution` | 状态分布（flat/probing/holding/...） |
| `action_distribution` | 动作分布（open/hold/close/blocked） |

## 8. 策略适配器

| 适配器 | Signal Edge 来源 | 说明 |
|--------|------------------|------|
| `IndependentReplayAdapter` | `dominant_score * signal_edge_scale_bps` | 从 OHLCV 派生 alpha/momentum/trend/micro/confidence 因子，funding 作为附加项 |
| `DirectionalReplayAdapter` | `trend_w * score * scale + (1-trend_w) * clamped_return` | SMA crossover 趋势强度 + bar return 混合，funding 作为附加项 |

两个适配器均实现 `BaseReplayAdapter` 接口，均遵循统一 Edge Contract 输出 4 层分解。新增家族只需继承基类并实现 `evaluate_bar()` 方法。

## 9. Scan Run 状态

| 状态 | 含义 |
|------|------|
| `pending` | 已创建，未开始 |
| `running` | 正在执行 |
| `succeeded` | 全部组合成功 |
| `partial_success` | 部分成功、部分失败 |
| `failed` | 全部失败 |

## 10. 简化说明

Phase 2 replay 使用简化评分模型（不含 AI assessment、orderbook depth、真实 execution state），与生产系统评分存在偏差。不包含撮合仿真和 PnL accounting（属于后续 Phase）。成本模型使用可配置的保守估计（默认 7 bps），signal edge 通过可校准缩放系数映射。重点关注参数变化对决策结构的**相对影响**。

## 11. 校准批处理 (Calibration Batch)

`rdp_run_calibration_batch.py` 是一个轻量级批量校准工具，用于少量、人工设计的校准实验组合。与 `rdp_run_parameter_scan.py` 的参数网格笛卡尔积不同，校准批处理由 JSON 文件显式定义每组实验的参数和标签，适合以下场景：

| 场景 | 典型扫参 |
|------|----------|
| Signal scale 校准 | `signal_edge_scale_bps = 8, 10, 12, 15, 20` |
| 成本敏感性测试 | `(taker_fee_bps, slippage_bps)` = `(3,1)`, `(5,2)`, `(7,3)` |
| Threshold 敏感性 | `min_confirm_ticks = 2, 3, 4, 5` |

**用法：**

```bash
# JSON 文件驱动（推荐）
python scripts/rdp_run_calibration_batch.py \
    --batch-file configs/research_batches/independent_scale_calibration_15m.json

# 内置预设（不需要额外 JSON 文件）
python scripts/rdp_run_calibration_batch.py --preset independent_scale_15m

# 失败即停 + 自定义产物目录
python scripts/rdp_run_calibration_batch.py \
    --batch-file my_batch.json \
    --artifact-root artifacts/custom \
    --stop-on-error
```

**JSON 批次文件格式：**

```json
{
  "batch_name": "independent_scale_calibration_15m",
  "description": "Calibrate signal_edge_scale_bps",
  "family": "independent",
  "symbol": "BTC-USDT-SWAP",
  "timeframe": "15m",
  "dataset_version": "v1.0",
  "start": "2026-03-31",
  "end": "2026-04-02",
  "experiments": [
    {"label": "scale_10", "params": {"signal_edge_scale_bps": 10}},
    {"label": "scale_15", "params": {"signal_edge_scale_bps": 15}},
    {"label": "scale_20", "params": {"signal_edge_scale_bps": 20}}
  ]
}
```

公共字段（family, symbol, timeframe, start, end）放顶层，每个实验只写 `label` + `params`。

**产物结构：**

```text
artifacts/research/calibration_batches/<batch_run_id>/
  batch_spec.json           # 原始输入规格副本（便于复现）
  batch_summary.csv         # 人工快速比较（每行一组实验）
  batch_summary.json        # 机器可读 summary
  batch_report.md           # 批次级 Markdown 报告（含趋势分析）
  failed_experiments.json   # 失败实验列表
  experiment_refs.json      # label → experiment_id 映射
  experiments/
    <label>/
      replay_decisions.csv  # 单实验决策记录
      diagnostics.json      # 单实验诊断指标
      report.md             # 单实验报告
```

**内置预设：**

| 预设名 | 描述 |
|--------|------|
| `independent_scale_15m` | 扫 signal_edge_scale_bps = 8, 10, 12, 15, 20 |
| `independent_cost_15m` | 扫 (taker_fee_bps, slippage_bps) = (3,1), (5,2), (7,3) |
| `independent_confirm_ticks_15m` | 扫 min_confirm_ticks = 2, 3, 4, 5 |

**与 Parameter Scan 的区别：**

| 维度 | `rdp_run_parameter_scan.py` | `rdp_run_calibration_batch.py` |
|------|----------------------------|-------------------------------|
| 输入 | 参数网格 → 笛卡尔积 | JSON 显式定义每组实验 |
| 定位 | 大范围参数空间探索 | 少量人工设计的校准实验 |
| DB 记录 | 创建 scan_run 表记录 | 不创建新 DB 表，仅复用 experiment 表 |
| 产物 | comparison_summary.json | batch_summary.csv/json + batch_report.md |
| 报告 | 对比表 | 趋势分析 + 自动 findings |

## 12. Step 1 校准编排 (Step 1 Calibration Orchestrator)

`rdp_run_step1_calibration.py` 是 Step 1 的自动化编排脚本，将 3 个 calibration batch 串联为**可重复执行、可复现、可交付**的标准流程。

**固定范围（Step 1）**：`independent` / `BTC-USDT-SWAP` / `15m`

```bash
# 一键执行 Step 1 完整流程
python scripts/rdp_run_step1_calibration.py

# 首次运行时确保 schema 就绪
python scripts/rdp_run_step1_calibration.py --ensure-schema
```

**自动执行步骤：**

1. 顺序运行 3 个固定 batch（scale → cost → confirm_ticks）
2. 汇总 12 个实验结果为 round_summary
3. 规则化推荐引擎生成参数建议（透明、可解释，非黑盒）
4. 生成结论文档

**产物结构：**

```text
artifacts/research/calibration_rounds/<round_id>/
  round_manifest.json                       # 轮次元信息（时间、batch 状态）
  round_summary.csv                         # 3 个 batch 12 个实验的汇总表
  round_summary.json                        # 机器可读汇总
  parameter_recommendations.json            # 规则化参数推荐 + confidence + reason
  phase2_step1_calibration_conclusion.md    # 面向人的结论文档
  batches/                                  # 3 个 batch 的完整产物
```

**推荐引擎规则：**

| 参数 | 规则 |
|------|------|
| `signal_edge_scale_bps` | 最低 positive-edge scale → 向上找结构改善平衡点（开仓增长 >10% 或 positive_edge_ratio 改善 >15pp） |
| `taker_fee_bps` / `slippage_bps` | 检查默认 (5,2) 的 edge 方向、成本敏感度、edge 脆弱性 |
| `min_confirm_ticks` | 最保守 ticks（opening_count 不显著下降 >40%） |
| `min_safe_net_edge_bps` | Step 1 标记为 pending，需专项 batch |

## 13. Step 2 正式研究闭环 (Step 2 Research Orchestrator)

`rdp_run_step2_research.py` 将 Step 1 的单范围校准推进到覆盖 **independent + directional、15m + 1h** 的完整研究闭环。

**固定范围（Step 2）**：`BTC-USDT-SWAP` / `{independent, directional}` / `{15m, 1h}`

```bash
# 一键执行 Step 2 完整流程（4 phase）
python scripts/rdp_run_step2_research.py

# 首次运行确保 schema
python scripts/rdp_run_step2_research.py --ensure-schema

# 只跑 calibration（跳过 scan）
python scripts/rdp_run_step2_research.py --skip-scan

# 只跑 scan（跳过 calibration）
python scripts/rdp_run_step2_research.py --skip-calibration
```

**四阶段执行流程：**

| Phase | 内容 | 子任务数 |
|-------|------|----------|
| A: Calibration | independent/1h → directional/15m → directional/1h | 3 round × 3~5 batch = 13 batch |
| B: Formal Scan | independent/15m, 1h + directional/15m, 1h | 4 scan (27+27+18+18 = 90 combo) |
| C: Aggregation | 汇总 + 推荐 + parameter_candidates | — |
| D: Conclusion | 比较报告 + 结论文档 | — |

**Directional 特有参数：**

Directional family 除共享参数外，额外校准两个特有参数：

| 参数 | 含义 | 校准范围 |
|------|------|----------|
| `directional_trend_weight` | 趋势信号 vs bar return 的混合权重 (0~1) | 0.3, 0.5, 0.7, 0.85, 1.0 |
| `directional_return_clamp_bps` | bar return 贡献的上限 (bps) | 10, 15, 20, 30, 50 |

**产物结构：**

```text
artifacts/research/step2_rounds/<round_id>/
  round_manifest.json                       # 轮次元信息
  family_timeframe_summary.csv              # 按 family×timeframe 汇总的实验表
  family_timeframe_summary.json             # 机器可读汇总
  scan_comparison_summary.csv               # 4 组 scan 的统一比较表
  scan_comparison_summary.json              # 机器可读 scan 汇总
  parameter_candidates.json                 # 默认参数候选 (per family/tf)
  phase2_step2_research_conclusion.md       # 结论文档（含 4 维比较）
  batches/                                  # 13 个 calibration batch 的完整产物
```

**结论文档包含的比较维度：**

1. Independent vs Directional on 15m
2. Independent vs Directional on 1h
3. 15m vs 1h within Independent
4. 15m vs 1h within Directional

**推荐引擎扩展（Directional 特有）：**

| 参数 | 规则 |
|------|------|
| `directional_trend_weight` | positive edge 下选最优 opening/pos_ratio；接近时倾向更高 weight（更保守） |
| `directional_return_clamp_bps` | positive edge 下选 pos_ratio 最高；检查跨 clamp 的 edge 波动稳定性 |

**Scan Matrix 配置**（`configs/research_rounds/step2_formal_scan_matrix.json`）定义每个 family/timeframe 的参数网格：

| 组合 | 网格维度 | 组合数 |
|------|----------|--------|
| independent/15m | confirm_ticks × scale × net_edge | 27 |
| independent/1h | confirm_ticks × scale × net_edge | 27 |
| directional/15m | confirm_ticks × trend_weight × clamp | 18 |
| directional/1h | confirm_ticks × trend_weight × clamp | 18 |
