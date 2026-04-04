# 参数治理 (Parameter Governance)

## 1. 概述

研究平台产生的参数结论分散在:
- `parameter_recommendations.json` (Step 1 单实验推荐)
- `parameter_candidates.json` (Step 2 跨参数扫描推荐)
- Round 结论文档中的建议

Phase 5 将这些分散结论收口到统一的 **Parameter Registry**。

---

## 2. Parameter Set 生命周期

```
draft -> candidate -> frozen
                   \-> deprecated
```

| 状态 | 含义 |
|------|------|
| `draft` | 初始导入，未经验证 |
| `candidate` | 经过初步验证的候选参数 |
| `frozen` | 已确认为当前有效参数，不再修改 |
| `deprecated` | 已过期或被替代 |

---

## 3. Registry 文件结构

路径: `artifacts/governance/current_parameter_registry.json`

```json
{
  "generated_at": "2026-04-04T10:00:00+00:00",
  "parameter_sets": [
    {
      "parameter_set_id": "ps_20260403_143052_a1b2c3",
      "family": "independent",
      "symbol": "BTC-USDT-SWAP",
      "timeframe": "15m",
      "source_round_id": null,
      "source_phase": "phase2_step2",
      "dataset_version": "v1.0",
      "values": {
        "signal_edge_scale_bps": 12.0,
        "min_confirm_ticks": 2,
        "min_safe_net_edge_bps": 0.0,
        "score_stability_threshold": 2.0
      },
      "confidence": "medium",
      "status": "frozen",
      "created_at": "2026-04-03T14:30:52+00:00",
      "frozen_at": "2026-04-03T15:00:00+00:00",
      "deprecated_at": null,
      "notes": "从 parameter_candidates.json 导入"
    }
  ]
}
```

### 3.1 Parameter Set 字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `parameter_set_id` | string | 唯一标识 `ps_YYYYMMDD_HHMMSS_hex6` |
| `family` | string | 策略族: independent / directional |
| `symbol` | string | 交易对 |
| `timeframe` | string | 时间框架: 15m / 1H |
| `source_round_id` | string? | 来源 round ID |
| `source_phase` | string? | 来源 phase |
| `dataset_version` | string | 数据版本 |
| `values` | object | 参数键值对 |
| `confidence` | string? | high / medium / low |
| `status` | string | draft / candidate / frozen / deprecated |
| `created_at` | string | 创建时间 |
| `frozen_at` | string? | 冻结时间 |
| `deprecated_at` | string? | 废弃时间 |
| `notes` | string? | 备注 |

---

## 4. 操作指南

### 4.1 查看当前参数

```bash
# 查看全部
python scripts/rdp_freeze_parameter_set.py --action show --verbose

# 只看 frozen
python scripts/rdp_freeze_parameter_set.py --action show --status frozen

# 按 family 筛选
python scripts/rdp_freeze_parameter_set.py --action show --family independent
```

### 4.2 从实验结果导入

```bash
# 从 parameter_candidates.json 导入（自动解析 family_tf）
python scripts/rdp_freeze_parameter_set.py --action import \
    --source path/to/parameter_candidates.json

# 从 parameter_recommendations.json 导入（需指定 family/tf）
python scripts/rdp_freeze_parameter_set.py --action import \
    --source path/to/parameter_recommendations.json \
    --family independent --timeframe 15m

# 导入时指定初始状态为 candidate
python scripts/rdp_freeze_parameter_set.py --action import \
    --source path/to/parameter_candidates.json \
    --initial-status candidate
```

### 4.3 冻结参数

冻结意味着该参数集被确认为当前有效版本:

```bash
python scripts/rdp_freeze_parameter_set.py --action freeze \
    --parameter-set-id ps_20260403_143052_a1b2c3 \
    --notes "Phase 3 归因验证通过"
```

### 4.4 废弃参数

当参数被新版本替代时:

```bash
python scripts/rdp_freeze_parameter_set.py --action deprecate \
    --parameter-set-id ps_20260403_143052_a1b2c3 \
    --notes "被 ps_20260404 替代"
```

---

## 5. 如何确认当前有效参数

1. 查看 registry 中 `status: "frozen"` 的 parameter set
2. 按 `frozen_at` 时间降序，最近冻结的为当前有效版本
3. 如果没有 frozen 的，查看 `status: "candidate"` 的
4. 如果都没有，需要重新运行 Step 2 研究

---

## 6. 参数传递链路

```
Step 2 研究
  -> parameter_candidates.json
    -> 导入 Registry (draft)
      -> 验证提升为 candidate
        -> Phase 3/4 验证通过 -> frozen
          -> Phase 3/4 round 通过 --params-json 使用
```

Phase 3/4 round runner 通过 `--params-json` 注入参数:
```bash
python scripts/rdp_run_phase3_round.py \
    --start 2026-03-31 --end 2026-04-02 \
    --params-json path/to/parameter_candidates.json
```
