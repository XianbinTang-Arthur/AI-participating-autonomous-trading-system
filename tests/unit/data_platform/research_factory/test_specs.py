from datetime import datetime, timedelta, timezone

import pytest

from aats.data_platform.research_factory.specs import (
    DatasetSpec,
    ExperimentSpec,
    FeatureSpec,
    LabelSpec,
    MetricsSnapshot,
    ProcessorSpec,
    ResearchWorkflowSpec,
    SegmentSpec,
    WorkflowStageSpec,
)


UTC = timezone.utc


def dt(day: int) -> datetime:
    return datetime(2026, 1, day, tzinfo=UTC)


def segment(name: str, start_day: int, end_day: int) -> SegmentSpec:
    return SegmentSpec(
        name=name,
        start=dt(start_day),
        end=dt(end_day),
        purpose=f"{name} segment",
    )


def dataset_spec() -> DatasetSpec:
    return DatasetSpec(
        dataset_id="btc_15m_v1",
        symbol="BTC-USDT-SWAP",
        timeframe="15m",
        dataset_version="v1.0",
        window_start=dt(1),
        window_end=dt(10),
        segments=[
            segment("train", 1, 5),
            segment("valid", 5, 7),
            segment("test", 7, 10),
        ],
        source_refs={"gold": "gold.replay_bars"},
    )


def label_spec() -> LabelSpec:
    return LabelSpec(
        name="future_net_return_h4",
        horizon_bars=4,
        return_kind="simple_return",
        net_of_fee=True,
        net_of_slippage=True,
        include_funding=True,
        fee_bps=5.0,
        slippage_bps=2.0,
    )


def feature_spec() -> FeatureSpec:
    return FeatureSpec(
        name="close_return_1",
        expression="Return(close, 1)",
        processors=[ProcessorSpec(name="zscore", params={"window": 20})],
    )


def complete_metrics_snapshot() -> MetricsSnapshot:
    return MetricsSnapshot(
        ic=0.1,
        rank_ic=0.2,
        icir=0.3,
        rank_icir=0.4,
        annualized_return=0.05,
        net_annualized_return=0.03,
        information_ratio=0.7,
        sharpe=0.8,
        max_drawdown=0.1,
        turnover=0.2,
        fee_bps_mean=5.0,
        slippage_bps_mean=2.0,
        funding_bps_mean=0.5,
        fillable_ratio=0.9,
        partial_fill_ratio=0.05,
        cost_adjusted_edge_bps_mean=1.2,
    )


def test_dataset_rejects_timezone_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        SegmentSpec(
            name="train",
            start=datetime(2026, 1, 1),
            end=dt(2),
            purpose="bad segment",
        )


def test_dataset_rejects_invalid_window_order() -> None:
    with pytest.raises(ValueError, match="window_end must be after window_start"):
        DatasetSpec(
            dataset_id="bad_window",
            symbol="BTC-USDT-SWAP",
            timeframe="15m",
            dataset_version="v1.0",
            window_start=dt(2),
            window_end=dt(2),
            segments=[segment("train", 1, 2)],
        )


def test_dataset_rejects_segment_outside_window() -> None:
    with pytest.raises(ValueError, match="within dataset window"):
        DatasetSpec(
            dataset_id="out_of_window",
            symbol="BTC-USDT-SWAP",
            timeframe="15m",
            dataset_version="v1.0",
            window_start=dt(2),
            window_end=dt(5),
            segments=[segment("train", 1, 3)],
        )


def test_dataset_rejects_train_valid_test_overlap() -> None:
    with pytest.raises(ValueError, match="must not overlap"):
        DatasetSpec(
            dataset_id="overlap",
            symbol="BTC-USDT-SWAP",
            timeframe="15m",
            dataset_version="v1.0",
            window_start=dt(1),
            window_end=dt(6),
            segments=[
                segment("train", 1, 4),
                segment("valid", 3, 5),
            ],
        )


def test_dataset_rejects_test_segment_before_train() -> None:
    with pytest.raises(ValueError, match="test segment must not be earlier"):
        DatasetSpec(
            dataset_id="test_before_train",
            symbol="BTC-USDT-SWAP",
            timeframe="15m",
            dataset_version="v1.0",
            window_start=dt(1),
            window_end=dt(10),
            segments=[
                segment("test", 1, 3),
                segment("train", 5, 8),
            ],
        )


def test_dataset_allows_explicit_replay_overlap() -> None:
    spec = DatasetSpec(
        dataset_id="replay_overlap",
        symbol="BTC-USDT-SWAP",
        timeframe="15m",
        dataset_version="v1.0",
        window_start=dt(1),
        window_end=dt(5),
        segments=[
            segment("train", 1, 3),
            segment("replay", 2, 4),
        ],
    )

    assert [segment.name for segment in spec.segments] == ["train", "replay"]


def test_experiment_rejects_artifact_root_outside_research_tree() -> None:
    with pytest.raises(ValueError, match="under artifacts/research"):
        ExperimentSpec(
            experiment_id="exp_1",
            dataset=dataset_spec(),
            features=[feature_spec()],
            label=label_spec(),
            model_ref="baseline",
            metrics=["ic"],
            artifact_root="artifacts/private",
        )


def test_experiment_rejects_non_candidate_governance_mode() -> None:
    with pytest.raises(ValueError, match="governance_mode"):
        ExperimentSpec(
            experiment_id="exp_1",
            dataset=dataset_spec(),
            features=[feature_spec()],
            label=label_spec(),
            model_ref="baseline",
            metrics=["ic"],
            artifact_root="artifacts/research",
            governance_mode="active_apply",
        )


def test_experiment_accepts_candidate_only_spec() -> None:
    spec = ExperimentSpec(
        experiment_id="exp_1",
        dataset=dataset_spec(),
        features=[feature_spec()],
        label=label_spec(),
        model_ref="baseline",
        metrics=["ic", "rank_ic"],
        artifact_root="artifacts/research/experiments",
    )

    assert spec.governance_mode == "candidate_only"
    assert spec.features[0].name == "close_return_1"


def test_processor_rejects_callable_params() -> None:
    with pytest.raises(ValueError, match="must not contain callables"):
        ProcessorSpec(name="zscore", params={"callback": lambda value: value})


def test_label_rejects_non_positive_horizon() -> None:
    with pytest.raises(ValueError, match="horizon_bars must be positive"):
        LabelSpec(
            name="future_net_return_h0",
            horizon_bars=0,
            return_kind="simple_return",
            net_of_fee=True,
            net_of_slippage=True,
            include_funding=True,
            fee_bps=5.0,
            slippage_bps=2.0,
        )


def test_metrics_snapshot_requires_missing_reason_for_null_metric() -> None:
    with pytest.raises(ValueError, match="missing without a reason"):
        MetricsSnapshot(
            ic=None,
            rank_ic=0.1,
            icir=0.1,
            rank_icir=0.1,
            annualized_return=0.1,
            net_annualized_return=0.1,
            information_ratio=0.1,
            sharpe=0.1,
            max_drawdown=0.1,
            turnover=0.1,
            fee_bps_mean=1.0,
            slippage_bps_mean=1.0,
            funding_bps_mean=1.0,
            fillable_ratio=0.9,
            partial_fill_ratio=0.1,
            cost_adjusted_edge_bps_mean=1.0,
        )


def test_metrics_snapshot_accepts_complete_metrics() -> None:
    assert complete_metrics_snapshot().cost_adjusted_edge_bps_mean == 1.2


def test_safe_ids_reject_path_separators() -> None:
    with pytest.raises(ValueError, match="path traversal"):
        DatasetSpec(
            dataset_id="../escape",
            symbol="BTC-USDT-SWAP",
            timeframe="15m",
            dataset_version="v1.0",
            window_start=dt(1),
            window_end=dt(3),
            segments=[segment("train", 1, 2)],
        )


def test_segment_rejects_empty_window() -> None:
    with pytest.raises(ValueError, match="end must be after start"):
        SegmentSpec(
            name="train",
            start=dt(1),
            end=dt(1) + timedelta(0),
            purpose="empty",
        )


def test_research_workflow_requires_dataset_stage() -> None:
    with pytest.raises(ValueError, match="must include a dataset stage"):
        ResearchWorkflowSpec(
            workflow_id="workflow_without_dataset",
            stages=[
                WorkflowStageSpec(
                    name="experiment",
                    purpose="run experiment",
                    outputs=["candidate_parameters.json"],
                )
            ],
        )


def test_research_workflow_rejects_unknown_stage() -> None:
    with pytest.raises(ValueError, match="workflow stage"):
        WorkflowStageSpec(
            name="live_execution",
            purpose="not allowed",
        )


def test_research_workflow_rejects_sandbox_after_governance_apply() -> None:
    with pytest.raises(ValueError, match="sandbox stage must not run after governance apply"):
        ResearchWorkflowSpec(
            workflow_id="sandbox_after_apply",
            stages=[
                WorkflowStageSpec(name="dataset", purpose="prepare dataset"),
                WorkflowStageSpec(name="governance", purpose="apply candidate", action="apply"),
                WorkflowStageSpec(name="sandbox", purpose="generate proposal"),
            ],
        )


def test_research_workflow_rejects_non_research_outputs() -> None:
    with pytest.raises(ValueError, match="not research-only"):
        ResearchWorkflowSpec(
            workflow_id="bad_outputs",
            stages=[
                WorkflowStageSpec(
                    name="dataset",
                    purpose="prepare dataset",
                    outputs=["active_parameter_set.json"],
                )
            ],
            outputs=["candidate_parameters.json"],
        )


def test_research_workflow_accepts_research_only_outputs() -> None:
    spec = ResearchWorkflowSpec(
        workflow_id="baseline_research_workflow",
        stages=[
            WorkflowStageSpec(
                name="dataset",
                purpose="prepare dataset",
                outputs=["datasets/btc_15m_v1.json"],
            ),
            WorkflowStageSpec(
                name="experiment",
                purpose="run benchmark",
                outputs=["experiments/exp_1/metrics_snapshot.json"],
            ),
            WorkflowStageSpec(
                name="governance",
                purpose="candidate-only review",
                action="candidate_review",
                outputs=["candidate_parameters.json"],
            ),
        ],
        outputs=["research_summary.json"],
        description="research-only baseline workflow",
    )

    assert [stage.name for stage in spec.stages] == ["dataset", "experiment", "governance"]
    assert spec.outputs == ("research_summary.json",)
