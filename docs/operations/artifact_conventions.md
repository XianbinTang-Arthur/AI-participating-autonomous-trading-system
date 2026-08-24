# Artifact 规范 (Artifact Conventions)

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。

> 最后核对：2026-08-22。Artifact 是证据/审计产物，不等于 runtime 配置真源；尤其 active parameter JSON 不再被主交易 loader fallback 读取。


## 1. 目录结构

```
artifacts/
  research/
    experiments/{uuid}/           # Step 1/2 单实验
      diagnostics.json            # 核心诊断数据
      replay_decisions.csv        # Replay 决策记录
      report.md                   # Markdown 报告
      replay_params_used.json     # (可选) 实际使用的参数快照
      parameter_recommendations.json  # (Step 1 产出)
      parameter_candidates.json       # (Step 2 产出)
      {uuid}/                     # 参数扫描子实验
        diagnostics.json
        replay_decisions.csv
        report.md
      comparison_summary.json     # (参数扫描产出)
      comparison_report.md        # (参数扫描产出)

    calibration_batches/{batch_id}/  # Step 1 批量校准
      round_manifest.json
      ...

    calibration_rounds/{round_id}/   # Step 2 研究 round
      round_manifest.json
      ...

    attribution_rounds/{round_id}/   # Phase 3 归因 round
      round_manifest.json
      per_combo/
        {run_id}/
          attribution_summary.json
          replay_live_alignment.csv
          top_failure_modes.json
          report.md
      family_timeframe_attribution_summary.csv
      phase3_live_attribution_conclusion.md

    execution_rounds/{round_id}/     # Phase 4 执行评估 round
      round_manifest.json
      per_combo/
        {run_id}/
          execution_alignment.csv
          fill_feasibility.csv
          slippage_summary.csv
          execution_cost_summary.json
          report.md
      execution_realism_comparison.csv
      phase4_execution_realism_conclusion.md

  governance/
    artifact_index.json
    active_round_index.json
    current_parameter_registry.json      # → DB: governance.parameter_sets
    quality_monitor_summary.json

  decision_system/
    recommendation_registry.json          # → DB: governance.recommendations
    active_decision_registry.json         # → DB: governance.active_decisions
    evidence_bundle_index.json
    parameter_apply_history.json          # → DB: governance.parameter_apply_history

configs/active_parameter_sets/
    active_parameter_registry.json        # 历史兼容/审计副本；runtime 不读取
    <family>_<timeframe>.json             # 历史 per-combo 文件副本
```

> 标注 `→ DB:` 的治理 registry 通常同时有 governance schema 表；具体读写/降级语义以对应模块为准。runtime active parameters 是例外：`governance.active_parameter_sets` 为唯一真源，不从上述 JSON fallback。

---

## 2. Round ID 格式

```
{YYYYMMDD}_{HHMMSS}_{uuid8}
```

示例: `20260403_143052_a1b2c3d4`

- 时间为 UTC
- uuid 取前 8 个 hex 字符

---

## 3. Round Manifest 统一规范

每个 round 目录**必须**包含 `round_manifest.json`，字段规范:

```json
{
  "round_id": "20260403_143052_a1b2c3d4",
  "phase": "phase3",
  "status": "succeeded",
  "started_at": "2026-04-03T14:30:52.123456+00:00",
  "finished_at": "2026-04-03T14:35:12.654321+00:00",
  "scope": {
    "symbol": "BTC-USDT-SWAP",
    "families": ["independent", "directional"],
    "timeframes": ["15m", "1h"],
    "window": {"start": "2026-03-31", "end": "2026-04-02"}
  },
  "input_refs": {
    "dataset_version": "v1.0",
    "parameter_set_id": null
  },
  "output_refs": {
    "summary_path": "family_timeframe_attribution_summary.csv",
    "report_path": "phase3_live_attribution_conclusion.md"
  },
  "combos": [
    {
      "key": "independent_15m",
      "family": "independent",
      "timeframe": "15m",
      "status": "succeeded",
      "run_dir": "per_combo/..."
    }
  ],
  "code_version": null,
  "notes": null
}
```

### 3.1 必须字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `round_id` | string | 唯一标识 |
| `status` | string | 整体状态 |
| `started_at` | string | ISO 8601 UTC |
| `finished_at` | string | ISO 8601 UTC |
| `scope` | object | 范围描述 |

### 3.2 推荐字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `phase` | string | Phase 标识 |
| `input_refs` | object | 输入引用 |
| `output_refs` | object | 输出引用 |
| `code_version` | string | 代码版本 |
| `notes` | string | 备注 |

### 3.3 status 合法值

见 [Round 生命周期](round_lifecycle.md)。

---

## 4. 关键文件说明

### 4.1 diagnostics.json (Step 1/2)

| 字段 | 说明 |
|------|------|
| `total_bars` | 总 bar 数 |
| `opening_count` | 开仓次数 |
| `positive_edge_ratio` | 正 edge 比例 |
| `selectable_ratio` | 可选比例 |
| `execution_compatible_ratio` | 执行兼容比例 |
| `top_blocking_reasons` | 阻断原因 Top N |

### 4.2 attribution_summary.json (Phase 3)

每个 combo 的 replay vs live 归因汇总。

### 4.3 execution_cost_summary.json (Phase 4)

| 字段 | 说明 |
|------|------|
| `total_candidates` | 候选订单总数 |
| `full_fill_ratio` | 完全可成交比例 |
| `slippage.mean` | 平均滑点 (bps) |
| `total_execution_cost.mean` | 平均总执行成本 (bps) |
| `cost_adjusted_edge.mean` | 成本调整后 edge (bps) |
| `positive_adjusted_edge_ratio` | 正调整 edge 比例 |

---

## 5. 校验工具

使用 `rdp_validate_artifacts.py` 校验:

```bash
python scripts/rdp_validate_artifacts.py
python scripts/rdp_validate_artifacts.py --phase phase3 --fix
```

使用 `rdp_build_artifact_index.py` 构建索引:

```bash
python scripts/rdp_build_artifact_index.py
```
