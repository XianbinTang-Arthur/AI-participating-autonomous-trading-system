# Release Effectiveness Evaluation

## 概述

每次 parameter release 都应该得到一个 effectiveness 后评估，回答：
- 这次 apply 有没有带来改善？
- 应该保留还是回退？

## 评价维度

### 1. 行为层 (Behavior)

检查 observation 中 attribution 和 decision status 是否有 regression。

| Score | 条件 |
|-------|------|
| positive | 无 regression |
| negative | attribution 或 decision regression |
| unknown | 缺少 observation 数据 |

### 2. 执行层 (Execution)

检查 execution realism 是否恶化。

| Score | 条件 |
|-------|------|
| positive | 执行稳定或改善 |
| negative | execution regression |
| unknown | 缺少执行数据 |

### 3. 运营层 (Operations)

检查是否触发 rollback recommendation，observation 是否完成。

| Score | 条件 |
|-------|------|
| positive | observation 完成，无 rollback |
| negative | 触发 rollback recommendation |
| unknown | 仍在 observing |

### 4. 治理层 (Governance)

检查 gate 状态和未处理的 critical alerts。

| Score | 条件 |
|-------|------|
| positive | governance 健康 |
| negative | gate blocked 或有 critical alerts |
| mixed | gate 有 warnings |

## 综合结论

| 结论 | 条件 |
|------|------|
| `effective` | 无 negative，至少 2 个 positive |
| `mixed` | 有 positive 也有 negative |
| `ineffective` | 2+ negative |
| `rollback_triggered` | operations 维度有 rollback |
| `insufficient_evidence` | 3+ unknown |

## 使用方式

```bash
# 评估指定 release
python scripts/rdp_evaluate_release_effectiveness.py --release-id rel_20260404_...

# JSON 输出
python scripts/rdp_evaluate_release_effectiveness.py --release-id rel_20260404_... --json
```

### 退出码

| 码 | 含义 |
|----|------|
| 0 | effective |
| 1 | ineffective / rollback_triggered |
| 2 | mixed |
| 3 | insufficient_evidence |

## 与 Baseline Comparison 的关系

Effectiveness 评估会自动引用 baseline comparison 的结论（如果已生成）。
建议流程：先运行 baseline comparison，再运行 effectiveness evaluation。

```bash
python scripts/rdp_compare_release_to_baseline.py --release-id rel_...
python scripts/rdp_evaluate_release_effectiveness.py --release-id rel_...
```

## 输出

评估结果保存到 `artifacts/metrics/release_effectiveness_registry.json`，同一 release 的重复评估会替换旧结果。

```json
{
  "evaluation_id": "eff_20260404_120000",
  "release_id": "rel_20260404_...",
  "family": "independent",
  "timeframe": "15m",
  "conclusion": "mixed",
  "dimensions": [
    {"dimension": "behavior", "score": "positive", "detail": "..."},
    {"dimension": "execution", "score": "unknown", "detail": "..."},
    {"dimension": "operations", "score": "positive", "detail": "..."},
    {"dimension": "governance", "score": "mixed", "detail": "..."}
  ]
}
```
