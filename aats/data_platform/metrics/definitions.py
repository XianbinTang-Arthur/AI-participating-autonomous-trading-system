"""统一指标定义.

工作包 A: 定义 RDP integration 的所有 success metrics，分为 5 层。
每个指标有名称、分层、描述、计算方式、方向 (higher_is_better / lower_is_better)。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MetricDefinition:
    """单个指标定义."""
    name: str
    layer: str           # research / attribution / execution / operations / reliability
    description: str
    direction: str       # higher_is_better / lower_is_better
    unit: str = ""       # count, ratio, bps, hours, etc.
    default_value: float | None = None


# ── 研究层指标 ────────────────────────────────────────────────

RESEARCH_METRICS: list[MetricDefinition] = [
    MetricDefinition(
        name="recommendation_count",
        layer="research",
        description="总 recommendation 数量",
        direction="higher_is_better",
        unit="count",
    ),
    MetricDefinition(
        name="approved_recommendation_count",
        layer="research",
        description="已审批通过的 recommendation 数量",
        direction="higher_is_better",
        unit="count",
    ),
    MetricDefinition(
        name="promoted_parameter_set_count",
        layer="research",
        description="已提升为 frozen/candidate 的参数集数量",
        direction="higher_is_better",
        unit="count",
    ),
    MetricDefinition(
        name="evidence_completeness_ratio",
        layer="research",
        description="证据完整性比率 (phases_with_data / total_phases)",
        direction="higher_is_better",
        unit="ratio",
    ),
    MetricDefinition(
        name="stale_recommendation_ratio",
        layer="research",
        description="长期未处理 (draft) 的 recommendation 比率",
        direction="lower_is_better",
        unit="ratio",
    ),
]


# ── 归因层指标 ────────────────────────────────────────────────

ATTRIBUTION_METRICS: list[MetricDefinition] = [
    MetricDefinition(
        name="replay_live_alignment_coverage",
        layer="attribution",
        description="回放与实盘对齐覆盖率",
        direction="higher_is_better",
        unit="ratio",
    ),
    MetricDefinition(
        name="top_failure_mode_concentration",
        layer="attribution",
        description="Top failure mode 集中度 (越低越分散)",
        direction="lower_is_better",
        unit="ratio",
    ),
    MetricDefinition(
        name="strategy_blocked_ratio",
        layer="attribution",
        description="策略被阻断的比率",
        direction="lower_is_better",
        unit="ratio",
    ),
    MetricDefinition(
        name="risk_rejected_ratio",
        layer="attribution",
        description="风控拒绝���比率",
        direction="lower_is_better",
        unit="ratio",
    ),
    MetricDefinition(
        name="execution_blocked_ratio",
        layer="attribution",
        description="执行被阻断的比率",
        direction="lower_is_better",
        unit="ratio",
    ),
]


# ── 执行可行性层指标 ──────────────────────────────────────────

EXECUTION_METRICS: list[MetricDefinition] = [
    MetricDefinition(
        name="full_fill_ratio",
        layer="execution",
        description="完全成交比率",
        direction="higher_is_better",
        unit="ratio",
    ),
    MetricDefinition(
        name="partial_fill_ratio",
        layer="execution",
        description="部分成交比率",
        direction="lower_is_better",
        unit="ratio",
    ),
    MetricDefinition(
        name="mean_total_execution_cost_bps",
        layer="execution",
        description="平均总执行成本 (bps)",
        direction="lower_is_better",
        unit="bps",
    ),
    MetricDefinition(
        name="positive_adjusted_edge_ratio",
        layer="execution",
        description="成本调整后仍有正收益的比率",
        direction="higher_is_better",
        unit="ratio",
    ),
]


# ── 运营层指标 ────────────────────────────────────────────────

OPERATIONS_METRICS: list[MetricDefinition] = [
    MetricDefinition(
        name="apply_success_count",
        layer="operations",
        description="参数 apply 成功次数",
        direction="higher_is_better",
        unit="count",
    ),
    MetricDefinition(
        name="rollback_count",
        layer="operations",
        description="参数 rollback 次数",
        direction="lower_is_better",
        unit="count",
    ),
    MetricDefinition(
        name="rollback_recommendation_count",
        layer="operations",
        description="触发 rollback recommendation 的次数",
        direction="lower_is_better",
        unit="count",
    ),
    MetricDefinition(
        name="release_observation_completion_ratio",
        layer="operations",
        description="release observation 完成率",
        direction="higher_is_better",
        unit="ratio",
    ),
    MetricDefinition(
        name="release_without_gate_ratio",
        layer="operations",
        description="跳过 gate 的 release 比率",
        direction="lower_is_better",
        unit="ratio",
    ),
]


# ── 可靠性层指标 ──────────────────────────────────────────────

RELIABILITY_METRICS: list[MetricDefinition] = [
    MetricDefinition(
        name="workflow_success_ratio",
        layer="reliability",
        description="Workflow 成功率",
        direction="higher_is_better",
        unit="ratio",
    ),
    MetricDefinition(
        name="retry_success_ratio",
        layer="reliability",
        description="补跑成功率",
        direction="higher_is_better",
        unit="ratio",
    ),
    MetricDefinition(
        name="alert_open_count",
        layer="reliability",
        description="当前 open 告警数量",
        direction="lower_is_better",
        unit="count",
    ),
    MetricDefinition(
        name="alert_resolution_time_hours",
        layer="reliability",
        description="告警平均解决时间（小时）",
        direction="lower_is_better",
        unit="hours",
    ),
    MetricDefinition(
        name="stale_round_count",
        layer="reliability",
        description="长期未完成的 round 数量",
        direction="lower_is_better",
        unit="count",
    ),
]


# ── 汇总 ─────────────────────────────────────────────────────

ALL_METRICS: list[MetricDefinition] = (
    RESEARCH_METRICS
    + ATTRIBUTION_METRICS
    + EXECUTION_METRICS
    + OPERATIONS_METRICS
    + RELIABILITY_METRICS
)

METRICS_BY_LAYER: dict[str, list[MetricDefinition]] = {
    "research": RESEARCH_METRICS,
    "attribution": ATTRIBUTION_METRICS,
    "execution": EXECUTION_METRICS,
    "operations": OPERATIONS_METRICS,
    "reliability": RELIABILITY_METRICS,
}

METRICS_BY_NAME: dict[str, MetricDefinition] = {
    m.name: m for m in ALL_METRICS
}


def get_metric_catalog() -> list[dict]:
    """返回指标目录（适合 JSON 输出）."""
    return [
        {
            "name": m.name,
            "layer": m.layer,
            "description": m.description,
            "direction": m.direction,
            "unit": m.unit,
        }
        for m in ALL_METRICS
    ]
