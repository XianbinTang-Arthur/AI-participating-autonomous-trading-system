# Recommendation 审批工作流

> 本文档定义 recommendation 从"结果文件"到"受控可审批对象"的完整生命周期。

---

## 1. Recommendation 生命周期

```
  draft ──→ approved ──→ (apply)
    │
    ├──→ rejected
    │
    └──→ superseded   ← 被新 recommendation 替代
```

### 状态定义

| 状态 | 含义 | 触发条件 |
|------|------|----------|
| `draft` | 初始状态，等待审批 | decision round 自动生成 |
| `approved` | 已批准，可被 apply | operator 显式审批 |
| `rejected` | 已拒绝 | operator 显式拒绝 |
| `superseded` | 已被替代 | 新 recommendation 替代旧 recommendation |

---

## 2. 审批元信息

每条 recommendation 审批后会包含以下元信息：

### approved 状态

| 字段 | 说明 |
|------|------|
| `approved_by` | 审批人 |
| `approved_at` | 审批时间（UTC ISO 8601） |
| `approval_notes` | 审批备注 |

### rejected 状态

| 字段 | 说明 |
|------|------|
| `rejected_by` | 拒绝人 |
| `rejected_at` | 拒绝时间 |
| `approval_notes` | 拒绝理由 |

### superseded 状态

| 字段 | 说明 |
|------|------|
| `superseded_by` | 操作人 |
| `superseded_at` | 替代时间 |
| `superseded_by_recommendation_id` | 替代此建议的新 recommendation id |
| `approval_notes` | 备注 |

---

## 3. 审批操作

### 3.1 通过脚本审批

```bash
# 审批
python scripts/rdp_approve_recommendation.py \
    --recommendation-id rec_20260404_153614_abc123 \
    --action approve \
    --actor operator_wang \
    --notes "confidence sufficient, evidence complete"

# 拒绝
python scripts/rdp_approve_recommendation.py \
    --recommendation-id rec_20260404_153614_abc123 \
    --action reject \
    --actor operator_wang \
    --notes "evidence incomplete, wait for next round"

# 替代
python scripts/rdp_approve_recommendation.py \
    --recommendation-id rec_old_xxx \
    --action supersede \
    --superseded-by-id rec_new_yyy \
    --notes "replaced by newer analysis"

# 预览（不实际修改）
python scripts/rdp_approve_recommendation.py \
    --recommendation-id rec_xxx \
    --action approve \
    --dry-run
```

### 3.2 通过 API 审批

```bash
# 审批
curl -X POST /rdp/recommendations/rec_xxx/approve \
    -d '{"actor": "operator_wang", "notes": "approved"}'

# 拒绝
curl -X POST /rdp/recommendations/rec_xxx/reject \
    -d '{"actor": "operator_wang", "notes": "insufficient evidence"}'

# 替代
curl -X POST /rdp/recommendations/rec_xxx/supersede \
    -d '{"actor": "system", "superseded_by_id": "rec_new", "notes": "replaced"}'
```

---

## 4. 审批前置检查

在审批前，operator 应检查：

### 必查项

- [ ] recommendation confidence >= medium
- [ ] evidence completeness 足够（evidence_bundle 有数据）
- [ ] quality monitor 健康（`GET /rdp/health` 显示 healthy）
- [ ] attribution / execution realism 无明显冲突

### 建议检查

- [ ] target_parameter_set_id 在 governance registry 中存在
- [ ] target parameter set 状态为 frozen 或 candidate
- [ ] 同一 family/timeframe 没有已批准但未 apply 的 recommendation

---

## 5. 审计日志

所有审批操作自动记录到：

```
artifacts/governance/approval_logs/recommendation_approval_history.jsonl
```

每条记录格式：

```json
{
  "timestamp": "2026-04-04T16:30:00+00:00",
  "action": "approve",
  "recommendation_id": "rec_xxx",
  "actor": "operator_wang",
  "recommendation_type": "parameter_upgrade",
  "family": "independent",
  "timeframe": "15m",
  "notes": "approved after review"
}
```

查看历史：

```bash
python scripts/approve_recommendation_and_apply.py --action history
```

---

## 6. 状态流转规则

| 当前状态 | 允许的目标状态 | 说明 |
|----------|---------------|------|
| draft | approved | 正常审批 |
| draft | rejected | 正常拒绝 |
| draft | superseded | 被新建议替代 |
| approved | superseded | 已批准但被新建议替代（未 apply 前） |
| rejected | — | 终态，不可变更 |
| superseded | — | 终态，不可变更 |

> **注意**: rejected 和 superseded 为终态。如需重新启用，应创建新的 recommendation。

---

## 7. 与 Apply 流程的衔接

recommendation 被 approved 后，不会自动生效。需要显式 apply：

```bash
# 方式 1：独立 apply
python scripts/rdp_apply_approved_recommendation.py \
    --recommendation-id rec_xxx --actor operator

# 方式 2：审批并同时 apply（旧脚本兼容）
python scripts/approve_recommendation_and_apply.py \
    --action approve-and-apply --rec-id rec_xxx
```

详见 `docs/operations/parameter_apply_and_rollback.md`。
