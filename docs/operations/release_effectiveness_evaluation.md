# Release Effectiveness Evaluation

> 文档状态：现行专题参考
> 最后核对：2026-08-27（起始 HEAD `9c4112c6`，含当前控制面收口候选；以本文档所在 HEAD 为准）
> 核对范围：effectiveness 计算、managed DB 真值、JSON 审计镜像与自动风险收敛代码；不证明任何现场 release 已评估或已回滚

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

默认命令会持久化 effectiveness，但不会执行风险收敛；`--dry-run` 只评估且不保存，
`--enforce` 才会在 `rollback_triggered` 时显式调用内部 enforcer，并且动作范围严格限制为
`--release-id` 指定的单个 release，不会顺带处理其他 pending release。只需查看现有状态时
优先使用受控 UI/API，或明确使用 `--dry-run`。

```bash
# 评估指定 release
python scripts/rdp_evaluate_release_effectiveness.py --release-id rel_20260404_...

# JSON 输出
python scripts/rdp_evaluate_release_effectiveness.py --release-id rel_20260404_... --json

# 无写入评估
python scripts/rdp_evaluate_release_effectiveness.py --release-id rel_20260404_... --dry-run --json

# 显式执行受控风险收敛（有资本/治理副作用）
python scripts/rdp_evaluate_release_effectiveness.py --release-id rel_20260404_... --enforce
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

## 真值、镜像与自动风险收敛

Managed 环境以 governance PostgreSQL 为 canonical；数据库读取失败时禁止回退到陈旧
JSON。`artifacts/metrics/release_effectiveness_registry.json` 是离线模式真值或 managed
模式审计镜像。DB 提交成功而镜像刷新失败时，业务写入仍然成功并记录 degraded；不得因
镜像失败重复提交。

`observation_cycle` 的持久化运行会以全量 pending 范围调用
`enforce_pending_rollbacks()`；本 CLI 只有显式 `--enforce` 时才调用，并传入精确单 release
过滤。只有精确
post-apply release/evidence、clean pending attempt、combo lock 与数据库资本证明契约全部
通过时，内部路径才会执行回滚、取消旧意图或 soft pause。旧格式、缺 provenance、畸形或
不确定结果进入 `reconciliation_required`，不得自动重放。Operator 已完成的人工回滚也只能
在 release、rollback operation/target、apply history、当前 active 和 action time 全部精确一致时
收口为 `enforced + proof_kind=rollback`；不能误标为 active-change cancellation。终态证明保存在应用层
insert-once 的 `governance.release_effectiveness_action_proofs`；该表当前没有数据库级
禁止 UPDATE/DELETE 的 trigger，因此运维权限仍必须受控，不能把它描述为数据库不可变账本。

## 输出示例

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
