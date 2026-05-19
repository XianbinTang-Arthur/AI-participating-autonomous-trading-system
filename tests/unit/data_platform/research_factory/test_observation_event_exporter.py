import json
from pathlib import Path

import pytest

from aats.data_platform.research_factory.observation_event_exporter import (
    load_source_events_jsonl,
    normalize_source_events,
    write_observation_events_jsonl,
)
from aats.data_platform.research_factory.observation_summary_generator import (
    build_observation_summary_from_events,
    load_observation_events_jsonl,
)


def research_root(tmp_path: Path) -> Path:
    return tmp_path / "artifacts" / "research" / "research_factory"


def source_path(tmp_path: Path) -> Path:
    return research_root(tmp_path) / "source_events" / "shadow_decisions.jsonl"


def output_path(tmp_path: Path) -> Path:
    return research_root(tmp_path) / "observation_events" / "events.jsonl"


def source_event(**overrides):
    payload = {
        "id": "shadow_evt_1",
        "created_at": "2026-05-18T00:00:00+00:00",
        "payload": {
            "bar_ts": "2026-05-18T00:00:00+00:00",
            "signal": True,
            "fillable": True,
            "partial_fill": False,
            "fee_bps": 5.0,
            "slippage_bps": 2.0,
            "funding_bps": 0.5,
            "cost_adjusted_edge_bps": 1.25,
            "drawdown": 0.04,
            "metric_drift": 0.1,
            "abort_triggered": False,
        },
    }
    for key, value in overrides.items():
        if key in payload["payload"]:
            payload["payload"][key] = value
        else:
            payload[key] = value
    return payload


def write_jsonl(path: Path, *payloads: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(payload, ensure_ascii=False, sort_keys=True) for payload in payloads)
        + "\n",
        encoding="utf-8",
    )
    return path


def test_shadow_source_events_normalize_to_canonical_observation_events(tmp_path: Path) -> None:
    source = write_jsonl(
        source_path(tmp_path),
        source_event(),
        source_event(
            id="shadow_evt_2",
            created_at="2026-05-18T01:00:00+00:00",
            bar_ts="2026-05-18T01:00:00+00:00",
            fillable=False,
            partial_fill=True,
            cost_adjusted_edge_bps=0.75,
            drawdown=0.07,
            metric_drift=0.2,
        ),
    )

    events = normalize_source_events(
        load_source_events_jsonl(source),
        recommendation_id="rec_export",
        candidate_id="cand_export",
        experiment_id="exp_export",
        mode="shadow",
        source_kind="shadow_decision",
    )
    output = write_observation_events_jsonl(events, output_path(tmp_path))

    loaded = load_observation_events_jsonl(output)
    summary = build_observation_summary_from_events(
        loaded,
        recommendation_id="rec_export",
        candidate_id="cand_export",
        experiment_id="exp_export",
        mode="shadow",
    )

    assert len(events) == 2
    assert loaded[0].source_event_id == "shadow_evt_1"
    assert summary.fillable_ratio == pytest.approx(0.5)
    assert summary.cost_adjusted_edge_bps_mean == pytest.approx(1.0)


def test_paper_intent_source_defaults_paper_intent_true(tmp_path: Path) -> None:
    source = write_jsonl(source_path(tmp_path), source_event(id="paper_evt_1"))

    events = normalize_source_events(
        load_source_events_jsonl(source),
        recommendation_id="rec_export",
        candidate_id="cand_export",
        experiment_id="exp_export",
        mode="paper",
        source_kind="paper_intent",
    )

    assert events[0].mode == "paper"
    assert events[0].paper_intent is True
    assert events[0].signal is True


def test_missing_execution_metric_rejects_source_event(tmp_path: Path) -> None:
    payload = source_event()
    del payload["payload"]["cost_adjusted_edge_bps"]
    source = write_jsonl(source_path(tmp_path), payload)

    with pytest.raises(ValueError, match="cost_adjusted_edge_bps"):
        normalize_source_events(
            load_source_events_jsonl(source),
            recommendation_id="rec_export",
            candidate_id="cand_export",
            experiment_id="exp_export",
            mode="shadow",
            source_kind="shadow_decision",
        )


def test_canonical_observation_event_identity_mismatch_rejected(tmp_path: Path) -> None:
    source = write_jsonl(
        source_path(tmp_path),
        {
            "ts": "2026-05-18T00:00:00+00:00",
            "recommendation_id": "rec_other",
            "candidate_id": "cand_export",
            "experiment_id": "exp_export",
            "mode": "shadow",
            "signal": True,
            "paper_intent": False,
            "fillable": True,
            "partial_fill": False,
            "fee_bps": 5.0,
            "slippage_bps": 2.0,
            "funding_bps": 0.5,
            "cost_adjusted_edge_bps": 1.25,
            "drawdown": 0.04,
            "metric_drift": 0.1,
            "abort_triggered": False,
        },
    )

    with pytest.raises(ValueError, match="recommendation_id"):
        normalize_source_events(
            load_source_events_jsonl(source),
            recommendation_id="rec_export",
            candidate_id="cand_export",
            experiment_id="exp_export",
            mode="shadow",
            source_kind="observation_event",
        )


def test_output_path_must_stay_under_research_artifacts(tmp_path: Path) -> None:
    source = write_jsonl(source_path(tmp_path), source_event())
    events = normalize_source_events(
        load_source_events_jsonl(source),
        recommendation_id="rec_export",
        candidate_id="cand_export",
        experiment_id="exp_export",
        mode="shadow",
        source_kind="shadow_decision",
    )

    with pytest.raises(ValueError, match="under artifacts/research"):
        write_observation_events_jsonl(events, tmp_path / "events.jsonl")


def test_output_requires_overwrite_opt_in(tmp_path: Path) -> None:
    source = write_jsonl(source_path(tmp_path), source_event())
    events = normalize_source_events(
        load_source_events_jsonl(source),
        recommendation_id="rec_export",
        candidate_id="cand_export",
        experiment_id="exp_export",
        mode="shadow",
        source_kind="shadow_decision",
    )
    output = output_path(tmp_path)
    write_observation_events_jsonl(events, output)

    with pytest.raises(ValueError, match="already exists"):
        write_observation_events_jsonl(events, output)

    write_observation_events_jsonl(events, output, overwrite=True)
