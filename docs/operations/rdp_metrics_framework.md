# RDP 统一指标框架

## 概述

RDP Metrics Framework 定义了一套统一指标体系，用于衡量 RDP integration 的整体效果。
指标分为 5 层，覆盖从研究到运营的完整链路。

## 指标分层

### 1. 研究层 (Research)

| 指标 | 方向 | 单位 | 说明 |
|------|------|------|------|
| `recommendation_count` | ↑ | count | 总 recommendation 数量 |
| `approved_recommendation_count` | ↑ | count | 已审批通过的 recommendation 数量 |
| `promoted_parameter_set_count` | ↑ | count | 已提升为 frozen/candidate 的参数集数量 |
| `evidence_completeness_ratio` | ↑ | ratio | 证据完整性 (phases_with_data / total_phases) |
| `stale_recommendation_ratio` | ↓ | ratio | 长期未处理 (draft) 的 recommendation 比率 |

### 2. 归因层 (Attribution)

| 指标 | 方向 | 单位 | 说明 |
|------|------|------|------|
| `replay_live_alignment_coverage` | ↑ | ratio | 回放与实盘对齐覆盖率 |
| `top_failure_mode_concentration` | ↓ | ratio | Top failure mode 集中度 |
| `strategy_blocked_ratio` | ↓ | ratio | 策略被阻断的比率 |
| `risk_rejected_ratio` | ↓ | ratio | 风控拒绝的比率 |
| `execution_blocked_ratio` | ↓ | ratio | 执行被阻断的比率 |

### 3. 执行可行性层 (Execution)

| 指标 | 方向 | 单位 | 说明 |
|------|------|------|------|
| `full_fill_ratio` | ↑ | ratio | 完全成交比率 |
| `partial_fill_ratio` | ↓ | ratio | 部分成交比率 |
| `mean_total_execution_cost_bps` | ↓ | bps | 平均总执行成本 |
| `positive_adjusted_edge_ratio` | ↑ | ratio | 成本调整后正收益比率 |

### 4. 运营层 (Operations)

| 指标 | 方向 | 单位 | 说明 |
|------|------|------|------|
| `apply_success_count` | ↑ | count | 参数 apply 成功次数 |
| `rollback_count` | ↓ | count | 参数 rollback 次数 |
| `rollback_recommendation_count` | ↓ | count | 触发 rollback recommendation 次数 |
| `release_observation_completion_ratio` | ↑ | ratio | release observation 完成率 |
| `release_without_gate_ratio` | ↓ | ratio | 缺少 gate 记录的 release 比率；生产目标必须为 0 |

### 5. 可靠性层 (Reliability)

| 指标 | 方向 | 单位 | 说明 |
|------|------|------|------|
| `workflow_success_ratio` | ↑ | ratio | Workflow 成功率 |
| `retry_success_ratio` | ↑ | ratio | 补跑成功率 |
| `alert_open_count` | ↓ | count | 当前 open 告警数量 |
| `alert_resolution_time_hours` | ↓ | hours | 告警平均解决时间 |
| `stale_round_count` | ↓ | count | 长期未完成的 round 数量 |

> ↑ = higher_is_better, ↓ = lower_is_better

> 生产安全说明：`release_without_gate_ratio > 0` 在 live 环境应视为流程违规，不只是普通运营指标退化。

## 使用方式

### 生成 Snapshot

```bash
# 全局 snapshot
python scripts/rdp_build_metrics_snapshot.py

# 按维度筛选
python scripts/rdp_build_metrics_snapshot.py --family independent --timeframe 15m

# 查看指标目录
python scripts/rdp_build_metrics_snapshot.py --catalog
```

### Snapshot 输出

```json
{
  "snapshot_id": "snap_20260404_120000",
  "generated_at": "2026-04-04T12:00:00Z",
  "filter": {"family": null, "timeframe": null},
  "metrics_by_layer": {
    "research": {"recommendation_count": 15, ...},
    "attribution": {...},
    "execution": {...},
    "operations": {...},
    "reliability": {...}
  },
  "flat_metrics": {"recommendation_count": 15, ...},
  "summary": {"total_metrics": 24, "non_zero_metrics": 12}
}
```

### 历史追踪

每次 snapshot 自动追加到 `artifacts/metrics/metrics_history.json`，保留最近 200 条。

### Snapshot 对比

```python
from aats.data_platform.metrics.metric_registry import compare_snapshots

diff = compare_snapshots(current_snapshot, baseline_snapshot)
# 每个指标返回: current, baseline, delta, trend (improved/regressed/unchanged)
```

## 数据来源

> 设置 `AATS_ACTIVE_PARAMETER_DB_URL` 后，研究/运营层指标优先从 DB 读取。

| 层 | DB 表（主存储） | JSON 文件（fallback） |
|----|--------|--------|
| Research | `governance.recommendations`, `governance.parameter_sets` | recommendation_registry.json, current_parameter_registry.json, decision_rounds/ |
| Attribution | — | evidence_summary.json (phase3) |
| Execution | — | evidence_summary.json (phase4) |
| Operations | `governance.parameter_apply_history`, `governance.active_parameter_sets` | parameter_apply_history.json, parameter_release_history.json |
| Reliability | — | workflow_runs/, workflow_failures.json, current_alerts.json |

## 扩展指标

在 `definitions.py` 中添加新的 `MetricDefinition`，然后在 `metric_calculator.py` 对应层的计算函数中添加计算逻辑。
