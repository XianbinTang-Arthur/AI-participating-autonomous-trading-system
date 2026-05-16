"""Typed Research Factory specifications with explicit validation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePath
from typing import Any

from aats.data_platform.research_factory.numeric import require_finite_number

ALLOWED_SEGMENT_NAMES = frozenset({"train", "valid", "test", "replay"})
ALLOWED_PROCESSOR_NAMES = frozenset(
    {
        "drop_missing",
        "forward_fill_limited",
        "winsorize",
        "zscore",
        "minmax",
        "leakage_guard",
    }
)
ALLOWED_RETURN_KINDS = frozenset({"simple_return", "log_return"})
ALLOWED_GOVERNANCE_MODES = frozenset({"candidate_only"})
ALLOWED_WORKFLOW_STAGES = frozenset(
    {
        "dataset",
        "feature",
        "experiment",
        "benchmark",
        "governance",
        "sandbox",
    }
)
FORBIDDEN_WORKFLOW_OUTPUT_TERMS = frozenset(
    {
        "active_parameter",
        "active_parameters",
        "apply",
        "live_order",
        "okx_write",
        "operator_write",
        "production_config",
    }
)
ALLOWED_WORKFLOW_ACTIONS = frozenset(
    {
        "candidate_gate",
        "candidate_review",
        "validate",
        "record",
        "static_scan",
        "draft_recommendation",
    }
)
FORBIDDEN_WORKFLOW_ACTION_TERMS = FORBIDDEN_WORKFLOW_OUTPUT_TERMS

METRIC_FIELDS = (
    "ic",
    "rank_ic",
    "icir",
    "rank_icir",
    "annualized_return",
    "net_annualized_return",
    "information_ratio",
    "sharpe",
    "max_drawdown",
    "turnover",
    "fee_bps_mean",
    "slippage_bps_mean",
    "funding_bps_mean",
    "fillable_ratio",
    "partial_fill_ratio",
    "cost_adjusted_edge_bps_mean",
)


def _require_non_empty(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _require_safe_id(value: str, field_name: str) -> str:
    value = _require_non_empty(value, field_name)
    if "/" in value or "\\" in value or value in {".", ".."} or ".." in value:
        raise ValueError(f"{field_name} must not contain path traversal or separators")
    return value


def _require_aware_datetime(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _intervals_overlap(left: "SegmentSpec", right: "SegmentSpec") -> bool:
    return left.start < right.end and right.start < left.end


def _require_artifact_root(value: str) -> str:
    value = _require_non_empty(value, "artifact_root")
    path = PurePath(value)
    parts = path.parts
    if ".." in parts:
        raise ValueError("artifact_root must not contain path traversal")
    has_research_artifact_root = any(
        parts[index] == "artifacts" and parts[index + 1] == "research"
        for index in range(len(parts) - 1)
    )
    if not has_research_artifact_root:
        raise ValueError("artifact_root must be under artifacts/research")
    return value


def _require_research_only_output(value: str) -> str:
    value = _require_non_empty(value, "workflow output")
    lowered = value.lower()
    for forbidden in FORBIDDEN_WORKFLOW_OUTPUT_TERMS:
        if forbidden in lowered:
            raise ValueError(f"workflow output {value!r} is not research-only")
    return value


def _require_research_only_action(value: str) -> str:
    value = _require_non_empty(value, "workflow_stage.action").lower()
    for forbidden in FORBIDDEN_WORKFLOW_ACTION_TERMS:
        if forbidden in value:
            raise ValueError(f"workflow action {value!r} is not research-only")
    if value not in ALLOWED_WORKFLOW_ACTIONS:
        allowed = ", ".join(sorted(ALLOWED_WORKFLOW_ACTIONS))
        raise ValueError(f"workflow action {value!r} must be one of: {allowed}")
    return value


@dataclass(frozen=True, slots=True)
class SegmentSpec:
    name: str
    start: datetime
    end: datetime
    purpose: str

    def __post_init__(self) -> None:
        if self.name not in ALLOWED_SEGMENT_NAMES:
            allowed = ", ".join(sorted(ALLOWED_SEGMENT_NAMES))
            raise ValueError(f"segment name {self.name!r} must be one of: {allowed}")
        _require_aware_datetime(self.start, "segment.start")
        _require_aware_datetime(self.end, "segment.end")
        if self.end <= self.start:
            raise ValueError("segment end must be after start")
        _require_non_empty(self.purpose, "segment.purpose")


@dataclass(frozen=True, slots=True)
class ProcessorSpec:
    name: str
    params: Mapping[str, Any] = field(default_factory=dict)
    version: str = "v1"

    def __post_init__(self) -> None:
        if self.name not in ALLOWED_PROCESSOR_NAMES:
            allowed = ", ".join(sorted(ALLOWED_PROCESSOR_NAMES))
            raise ValueError(f"processor {self.name!r} must be one of: {allowed}")
        if not isinstance(self.params, Mapping):
            raise ValueError("processor params must be a mapping")
        if any(callable(value) for value in self.params.values()):
            raise ValueError("processor params must not contain callables")
        _require_non_empty(self.version, "processor.version")
        object.__setattr__(self, "params", dict(self.params))


@dataclass(frozen=True, slots=True)
class FeatureSpec:
    name: str
    expression: str
    processors: Sequence[ProcessorSpec] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_safe_id(self.name, "feature.name")
        _require_non_empty(self.expression, "feature.expression")
        if not all(isinstance(processor, ProcessorSpec) for processor in self.processors):
            raise ValueError("feature processors must be ProcessorSpec instances")
        object.__setattr__(self, "processors", tuple(self.processors))
        object.__setattr__(self, "metadata", dict(self.metadata))


@dataclass(frozen=True, slots=True)
class LabelSpec:
    name: str
    horizon_bars: int
    return_kind: str
    net_of_fee: bool
    net_of_slippage: bool
    include_funding: bool
    fee_bps: float
    slippage_bps: float

    def __post_init__(self) -> None:
        _require_safe_id(self.name, "label.name")
        if self.horizon_bars <= 0:
            raise ValueError("label horizon_bars must be positive")
        if self.return_kind not in ALLOWED_RETURN_KINDS:
            allowed = ", ".join(sorted(ALLOWED_RETURN_KINDS))
            raise ValueError(f"label return_kind must be one of: {allowed}")
        fee_bps = require_finite_number(self.fee_bps, "label fee_bps")
        slippage_bps = require_finite_number(self.slippage_bps, "label slippage_bps")
        if fee_bps < 0:
            raise ValueError("label fee_bps must be non-negative")
        if slippage_bps < 0:
            raise ValueError("label slippage_bps must be non-negative")
        object.__setattr__(self, "fee_bps", fee_bps)
        object.__setattr__(self, "slippage_bps", slippage_bps)


@dataclass(frozen=True, slots=True)
class DatasetSpec:
    dataset_id: str
    symbol: str
    timeframe: str
    dataset_version: str
    window_start: datetime
    window_end: datetime
    segments: Sequence[SegmentSpec]
    source_refs: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_safe_id(self.dataset_id, "dataset_id")
        _require_non_empty(self.symbol, "symbol")
        _require_non_empty(self.timeframe, "timeframe")
        _require_non_empty(self.dataset_version, "dataset_version")
        _require_aware_datetime(self.window_start, "window_start")
        _require_aware_datetime(self.window_end, "window_end")
        if self.window_end <= self.window_start:
            raise ValueError("dataset window_end must be after window_start")
        if not self.segments:
            raise ValueError("dataset segments must not be empty")
        if not all(isinstance(segment, SegmentSpec) for segment in self.segments):
            raise ValueError("dataset segments must be SegmentSpec instances")
        segments = tuple(self.segments)
        for segment in segments:
            if segment.start < self.window_start or segment.end > self.window_end:
                raise ValueError("dataset segment must stay within dataset window")
        self._validate_segment_order(segments)
        object.__setattr__(self, "segments", segments)
        object.__setattr__(self, "source_refs", dict(self.source_refs))

    @staticmethod
    def _validate_segment_order(segments: tuple[SegmentSpec, ...]) -> None:
        comparable_segments = [segment for segment in segments if segment.name != "replay"]
        for index, left in enumerate(comparable_segments):
            for right in comparable_segments[index + 1 :]:
                if _intervals_overlap(left, right):
                    raise ValueError("train/valid/test segments must not overlap")

        train_ends = [segment.end for segment in comparable_segments if segment.name == "train"]
        test_starts = [segment.start for segment in comparable_segments if segment.name == "test"]
        if train_ends and test_starts and min(test_starts) < max(train_ends):
            raise ValueError("test segment must not be earlier than train segment")


@dataclass(frozen=True, slots=True)
class MetricsSnapshot:
    ic: float | None = None
    rank_ic: float | None = None
    icir: float | None = None
    rank_icir: float | None = None
    annualized_return: float | None = None
    net_annualized_return: float | None = None
    information_ratio: float | None = None
    sharpe: float | None = None
    max_drawdown: float | None = None
    turnover: float | None = None
    fee_bps_mean: float | None = None
    slippage_bps_mean: float | None = None
    funding_bps_mean: float | None = None
    fillable_ratio: float | None = None
    partial_fill_ratio: float | None = None
    cost_adjusted_edge_bps_mean: float | None = None
    missing_reasons: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.missing_reasons, Mapping):
            raise ValueError("missing_reasons must be a mapping")
        missing_reasons = dict(self.missing_reasons)
        for field_name in METRIC_FIELDS:
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    require_finite_number(value, f"metric {field_name}"),
                )
                continue
            if not missing_reasons.get(field_name):
                raise ValueError(f"metric {field_name!r} is missing without a reason")
        object.__setattr__(self, "missing_reasons", missing_reasons)


@dataclass(frozen=True, slots=True)
class ExperimentSpec:
    experiment_id: str
    dataset: DatasetSpec
    features: Sequence[FeatureSpec]
    label: LabelSpec
    model_ref: str
    metrics: Sequence[str]
    artifact_root: str
    governance_mode: str = "candidate_only"

    def __post_init__(self) -> None:
        _require_safe_id(self.experiment_id, "experiment_id")
        if not isinstance(self.dataset, DatasetSpec):
            raise ValueError("experiment dataset must be a DatasetSpec")
        if not self.features:
            raise ValueError("experiment features must not be empty")
        if not all(isinstance(feature, FeatureSpec) for feature in self.features):
            raise ValueError("experiment features must be FeatureSpec instances")
        if not isinstance(self.label, LabelSpec):
            raise ValueError("experiment label must be a LabelSpec")
        _require_non_empty(self.model_ref, "model_ref")
        if not self.metrics:
            raise ValueError("experiment metrics must not be empty")
        if not all(isinstance(metric, str) and metric.strip() for metric in self.metrics):
            raise ValueError("experiment metrics must be non-empty strings")
        _require_artifact_root(self.artifact_root)
        if self.governance_mode not in ALLOWED_GOVERNANCE_MODES:
            allowed = ", ".join(sorted(ALLOWED_GOVERNANCE_MODES))
            raise ValueError(f"governance_mode must be one of: {allowed}")
        object.__setattr__(self, "features", tuple(self.features))
        object.__setattr__(self, "metrics", tuple(self.metrics))


@dataclass(frozen=True, slots=True)
class WorkflowStageSpec:
    name: str
    purpose: str
    outputs: Sequence[str] = field(default_factory=tuple)
    action: str | None = None

    def __post_init__(self) -> None:
        if self.name not in ALLOWED_WORKFLOW_STAGES:
            allowed = ", ".join(sorted(ALLOWED_WORKFLOW_STAGES))
            raise ValueError(f"workflow stage {self.name!r} must be one of: {allowed}")
        _require_non_empty(self.purpose, "workflow_stage.purpose")
        outputs = tuple(_require_research_only_output(output) for output in self.outputs)
        action = self.action
        if action is not None:
            action = _require_research_only_action(action)
        object.__setattr__(self, "outputs", outputs)
        object.__setattr__(self, "action", action)


@dataclass(frozen=True, slots=True)
class ResearchWorkflowSpec:
    workflow_id: str
    stages: Sequence[WorkflowStageSpec]
    outputs: Sequence[str] = field(default_factory=tuple)
    description: str | None = None

    def __post_init__(self) -> None:
        _require_safe_id(self.workflow_id, "workflow_id")
        if not self.stages:
            raise ValueError("workflow stages must not be empty")
        if not all(isinstance(stage, WorkflowStageSpec) for stage in self.stages):
            raise ValueError("workflow stages must be WorkflowStageSpec instances")
        stages = tuple(self.stages)
        if not any(stage.name == "dataset" for stage in stages):
            raise ValueError("workflow must include a dataset stage")
        self._validate_stage_order(stages)
        outputs = tuple(_require_research_only_output(output) for output in self.outputs)
        description = self.description
        if description is not None:
            description = _require_non_empty(description, "workflow.description")
        object.__setattr__(self, "stages", stages)
        object.__setattr__(self, "outputs", outputs)
        object.__setattr__(self, "description", description)

    @staticmethod
    def _validate_stage_order(stages: tuple[WorkflowStageSpec, ...]) -> None:
        governance_apply_seen = False
        for stage in stages:
            if stage.name == "governance" and stage.action == "apply":
                governance_apply_seen = True
            if stage.name == "sandbox" and governance_apply_seen:
                raise ValueError("sandbox stage must not run after governance apply")
