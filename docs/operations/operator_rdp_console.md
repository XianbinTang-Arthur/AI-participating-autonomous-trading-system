# Operator RDP Console 使用指南

> 本文档描述 operator 如何通过 API 和脚本查看 RDP 关键结论，
> 无需进入 `artifacts/` 目录翻文件。

---

## 1. 观察面总览

Operator 可直接查看以下 5 类 RDP 数据：

| 数据类型 | API 端点 | 说明 |
|----------|----------|------|
| Active Parameter Sets | `GET /rdp/parameters/active` | 当前生效的策略参数 |
| Latest Recommendations | `GET /rdp/recommendations/latest` | 最近的治理建议 |
| Recommendations History | `GET /rdp/recommendations/history` | 完整建议历史含审批记录 |
| Attribution Summary | `GET /rdp/attribution/latest` | 最近归因分析结论 |
| Execution Realism Summary | `GET /rdp/execution/latest` | 最近执行真实性评估 |
| Family/TF Decisions | `GET /rdp/decisions/latest` | 当前 family/timeframe 决策状态 |
| Promotion Readiness | `GET /rdp/readiness` | 参数升级就绪度评估 |
| Decision Round | `GET /rdp/decision-round/latest` | 最近完整决策轮次 |
| Apply History | `GET /rdp/parameters/apply-history` | 参数应用/回滚操作历史 |
| Health | `GET /rdp/health` | RDP 子系统健康状态 |

所有 GET 端点要求 read access 权限。

---

## 2. Active Parameter Sets

### API

```
GET /rdp/parameters/active
```

### 展示字段

| 字段 | 说明 |
|------|------|
| family | 策略家族（independent / directional） |
| timeframe | 时间框架（15m / 1h） |
| parameter_set_id | active parameter set id |
| values | 当前生效的参数值 |
| source_round_id | 来源 decision round |
| status | 状态（active） |
| applied_at | 最近 apply 时间 |
| applied_by | apply 操作人 |
| approval_recommendation_id | 关联的 recommendation |

### 响应示例

```json
{
  "total_active_sets": 1,
  "known_combos": ["independent_15m", "independent_1h", "directional_15m", "directional_1h"],
  "active_combos": ["independent_15m"],
  "missing_combos": ["independent_1h", "directional_15m", "directional_1h"],
  "parameter_sets": [
    {
      "combo_key": "independent_15m",
      "family": "independent",
      "timeframe": "15m",
      "parameter_set_id": "ps_20260404_072612_a5cc10",
      "status": "active",
      "applied_at": "2026-04-04T16:00:00+00:00",
      "applied_by": "rdp_apply (operator)",
      "values": {
        "signal_edge_scale_bps": 5.0,
        "min_confirm_ticks": 2,
        "min_safe_net_edge_bps": 1.5
      }
    }
  ]
}
```

---

## 3. Latest Recommendations

### API

```
GET /rdp/recommendations/latest?limit=20&status=draft
```

### 展示字段

| 字段 | 说明 |
|------|------|
| recommendation_id | 唯一标识 |
| recommendation_type | parameter_upgrade / keep_active / lower_priority / pause / require_review |
| family / timeframe | 目标 family/timeframe |
| target_parameter_set_id | 关联参数集 |
| confidence | low / medium / high |
| status | draft / approved / rejected / superseded |
| created_at | 创建时间 |
| approved_by / approved_at | 审批人和审批时间 |
| rejected_by / rejected_at | 拒绝人和拒绝时间 |
| approval_notes | 审批备注 |

### 过滤

- `?status=draft` — 只看待审批的建议
- `?status=approved` — 只看已批准的建议
- `?limit=50` — 最多返回 50 条

---

## 4. Latest Attribution Summary

### API

```
GET /rdp/attribution/latest
```

### 展示字段

| 字段 | 说明 |
|------|------|
| round_id | attribution round id |
| status | round 状态 |
| summary | 整体归因摘要 |
| combos[].combo_key | family_timeframe 维度 |
| combos[].summary | 该维度的失败类别、占比 |

### 关键关注点

- top failure modes（主要失败模式）
- family/timeframe 维度的关键失败类别
- 与上一轮对比是否有结构性变化

---

## 5. Latest Execution Realism Summary

### API

```
GET /rdp/execution/latest
```

### 展示字段

| 字段 | 说明 |
|------|------|
| round_id | execution round id |
| full_fill_ratio | 完全成交率 |
| partial_fill_ratio | 部分成交率 |
| mean_total_execution_cost_bps | 平均总执行成本（bps） |
| positive_adjusted_edge_ratio | 正向调整 edge 占比 |

---

## 6. Family/Timeframe Decisions

### API

```
GET /rdp/decisions/latest
```

### 展示字段

| 字段 | 说明 |
|------|------|
| family / timeframe | 目标 |
| current_status | keep_active / lower_priority / pause / require_review |
| active_parameter_set_id | 当前参数集 |
| last_recommendation_id | 最近 recommendation |
| last_updated_at | 最近更新时间 |

### 状态含义

| 状态 | 含义 |
|------|------|
| keep_active | 继续运行 |
| lower_priority | 降低预算/优先级 |
| pause | 暂停运行 |
| require_review | 需要人工审查 |

---

## 7. Apply / Rollback 操作历史

### API

```
GET /rdp/parameters/apply-history
```

### 展示字段

| 字段 | 说明 |
|------|------|
| operation_id | 操作唯一标识 |
| operation_type | apply / rollback / clear |
| family / timeframe | 目标 combo |
| from_parameter_set_id | 操作前的参数集 |
| to_parameter_set_id | 操作后的参数集 |
| recommendation_id | 关联 recommendation |
| actor | 操作人 |
| created_at | 操作时间 |
| notes | 操作备注 |

---

## 8. 写入操作端点

以下端点需要 write access 权限：

| 端点 | 说明 |
|------|------|
| `POST /rdp/recommendations/{id}/approve` | 审批 recommendation |
| `POST /rdp/recommendations/{id}/reject` | 拒绝 recommendation |
| `POST /rdp/recommendations/{id}/supersede` | 替代 recommendation |
| `POST /rdp/parameters/apply` | 应用参数 |
| `POST /rdp/parameters/rollback` | 回滚参数 |

### 审批请求体

```json
{
  "actor": "operator_name",
  "notes": "审批通过，confidence 和 evidence 充分"
}
```

### Apply 请求体

```json
{
  "recommendation_id": "rec_xxx",
  "actor": "operator_name",
  "notes": "按 SOP 应用"
}
```

### Rollback 请求体

```json
{
  "family": "independent",
  "timeframe": "15m",
  "to_parameter_set_id": null,
  "actor": "operator_name",
  "notes": "live 行为异常，回滚"
}
```

---

## 9. CLI 脚本

除 API 外，operator 也可通过脚本操作：

```bash
# 查看待审批建议
python scripts/rdp_approve_recommendation.py \
    --recommendation-id rec_xxx --action approve --actor operator

# 应用已批准建议
python scripts/rdp_apply_approved_recommendation.py \
    --recommendation-id rec_xxx --actor operator

# 回滚参数
python scripts/rdp_rollback_active_parameter_set.py \
    --family independent --timeframe 15m --actor operator

# 查看 active parameters
python scripts/apply_active_parameter_set.py --action show-active

# 查看审批历史
python scripts/approve_recommendation_and_apply.py --action history
```
