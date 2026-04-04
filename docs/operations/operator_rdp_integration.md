# Operator RDP Integration 指南

## 1. 概述

RDP 通过只读 API 将研究/归因/治理/决策系统的关键结论暴露给 Operator。
Operator 不需要进入 artifacts 目录翻文件即可看到 RDP 的核心产出。

## 2. API 端点清单

所有端点需要 read access 权限，均为 GET 方法。

| 端点 | 说明 |
|------|------|
| `GET /rdp/health` | RDP 子系统健康状态 |
| `GET /rdp/parameters/active` | 当前 active parameter sets |
| `GET /rdp/attribution/latest` | 最近 attribution 结论 |
| `GET /rdp/execution/latest` | 最近 execution realism 结论 |
| `GET /rdp/decisions/latest` | 当前 family/timeframe 决策状态 |
| `GET /rdp/recommendations/latest` | 最近 recommendations |
| `GET /rdp/decision-round/latest` | 最近 decision round 完整结论 |
| `GET /rdp/readiness` | Promotion readiness 评估 |

## 3. 端点详细说明

### 3.1 GET /rdp/health

返回 RDP 子系统整体健康状态。

**响应示例:**
```json
{
  "overall_health": "healthy",
  "quality_monitor_health": "healthy",
  "governance_initialized": true,
  "decision_system_initialized": true,
  "active_parameter_count": 2,
  "checks": [
    {
      "category": "governance",
      "name": "artifact_index",
      "exists": true,
      "generated_at": "2026-04-04T07:30:00Z"
    }
  ]
}
```

**overall_health 值:**
- `healthy` — 治理层 + 决策层均正常
- `degraded` — 部分正常
- `not_initialized` — 尚未初始化

### 3.2 GET /rdp/parameters/active

返回所有 active parameter sets 的详细信息。

**响应示例:**
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
      "applied_at": "2026-04-04T07:30:00Z",
      "values": {
        "signal_edge_scale_bps": 12.0,
        "min_confirm_ticks": 2
      }
    }
  ]
}
```

### 3.3 GET /rdp/attribution/latest

返回最近一次 attribution round 的结论。

**响应示例:**
```json
{
  "available": true,
  "round_id": "20260404_073446_57046653",
  "manifest": {
    "round_id": "20260404_073446_57046653",
    "status": "succeeded",
    "started_at": "2026-04-04T07:34:46Z",
    "finished_at": "2026-04-04T07:35:12Z"
  },
  "summary": { ... },
  "combos": [
    {
      "combo_key": "independent_15m",
      "summary": { ... }
    }
  ]
}
```

### 3.4 GET /rdp/execution/latest

返回最近一次 execution realism round 的结论。
结构与 attribution 类似。

### 3.5 GET /rdp/decisions/latest

返回当前 family/timeframe 运营决策状态。

**响应示例:**
```json
{
  "available": true,
  "version": 3,
  "decisions": [
    {
      "family": "independent",
      "timeframe": "15m",
      "current_status": "keep_active",
      "last_recommendation_id": "rec_20260404_153614_abc123",
      "last_updated_at": "2026-04-04T15:36:14Z"
    }
  ],
  "status_distribution": {
    "keep_active": 1,
    "require_review": 3
  }
}
```

**决策状态值:**
- `keep_active` — 继续运行
- `lower_priority` — 降低优先级
- `pause` — 暂停
- `require_review` — 需要人工审查

### 3.6 GET /rdp/recommendations/latest

**查询参数:**
- `limit` (int, 1-100, default 20) — 返回条数
- `status` (string, optional) — 按状态过滤 (draft/approved/rejected/superseded)

**响应示例:**
```json
{
  "available": true,
  "total_count": 8,
  "recommendations": [
    {
      "recommendation_id": "rec_20260404_153614_abc123",
      "recommendation_type": "parameter_upgrade",
      "family": "independent",
      "timeframe": "15m",
      "confidence": "medium",
      "reason": "...",
      "status": "draft"
    }
  ],
  "status_distribution": {
    "draft": 6,
    "approved": 2
  }
}
```

### 3.7 GET /rdp/decision-round/latest

返回最近一次 decision round 的完整结论。

### 3.8 GET /rdp/readiness

返回 promotion readiness 评估结果。

## 4. UI 建议

### 4.1 Active Parameter Sets 卡片

展示:
- 每个 family/timeframe 的当前 active 参数
- parameter_set_id 和 frozen/candidate 状态
- source round 追溯

### 4.2 Latest Attribution 卡片

展示:
- top failure modes
- 最近 round 时间和状态
- combo 级别的通过/失败比例

### 4.3 Latest Execution Realism 卡片

展示:
- full_fill_ratio
- total_execution_cost_mean
- positive_adjusted_edge_ratio

### 4.4 Family/Timeframe Decisions 表格

展示:
- 所有 combo 的当前决策状态
- 颜色编码: keep_active=绿, lower_priority=黄, pause=红, require_review=橙
- 最近 recommendation ID 可点击查看详情
- readiness 状态

## 5. 数据来源

所有数据来自以下文件系统路径（API 层负责读取和格式化）:

| 数据 | 文件路径 |
|------|---------|
| Parameter Registry | `artifacts/governance/current_parameter_registry.json` |
| Active Parameters | `configs/active_parameter_sets/*.json` |
| Artifact Index | `artifacts/governance/artifact_index.json` |
| Quality Monitor | `artifacts/governance/quality_monitor_summary.json` |
| Recommendations | `artifacts/decision_system/recommendation_registry.json` |
| Active Decisions | `artifacts/decision_system/active_decision_registry.json` |
| Evidence Bundles | `artifacts/decision_system/evidence_bundle_index.json` |
| Decision Rounds | `artifacts/decision_rounds/<round_id>/` |
| Attribution Rounds | `artifacts/research/attribution_rounds/<round_id>/` |
| Execution Rounds | `artifacts/research/execution_rounds/<round_id>/` |
