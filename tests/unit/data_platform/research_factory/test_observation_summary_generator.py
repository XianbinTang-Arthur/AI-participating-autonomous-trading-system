import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aats.data_platform.research_factory.observation_summary_generator import (
    OBSERVATION_SUMMARY_GENERATOR_SCHEMA_VERSION,
    build_observation_summary_from_events,
    load_observation_events_jsonl,
    write_observation_summary,
)

UTC = timezone.utc


def research_root(tmp_path: Path) -> Path:
    return tmp_path / "artifacts" / "research" / "research_factory"


def event_payload(**overrides):
    payload = {
        "schema_version": OBSERVATION_SUMMARY_GENERATOR_SCHEMA_VERSION,
        "ts": "2026-05-18T00:00:00+00:00",
        "bar_ts": "2026-05-18T00:00:00+00:00",
        "recommendation_id": "rec_obs_summary",
        "candidate_id": "cand_obs_summary",
        "experiment_id": "exp_obs_summary",
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
    }
    payload.update(overrides)
    return payload


def write_events(path: Path, *payloads: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(payload, ensure_ascii=False, sort_keys=True) for payload in payloads)
        + "\n",
        encoding="utf-8",
    )
    return path


def events_path(tmp_path: Path) -> Path:
    return research_root(tmp_path) / "observation_events" / "events.jsonl"


def test_shadow_event_aggregation_writes_summary(tmp_path: Path) -> None:
    event_file = write_events(
        events_path(tmp_path),
        event_payload(),
        event_payload(
            ts="2026-05-18T01:00:00+00:00",
            bar_ts="2026-05-18T01:00:00+00:00",
            signal=False,
            fillable=False,
            partial_fill=True,
            cost_adjusted_edge_bps=0.75,
            drawdown=0.07,
            metric_drift=0.2,
        ),
    )
    events = load_observation_events_jsonl(event_file)

    summary = build_observation_summary_from_events(
        events,
        recommendation_id="rec_obs_summary",
        candidate_id="cand_obs_summary",
        experiment_id="exp_obs_summary",
        mode="shadow",
        source_artifact_ref="research_factory/observation_events/events.jsonl",
        generated_at=datetime(2026, 5, 18, 2, tzinfo=UTC),
    )
    output = research_root(tmp_path) / "observation_inputs" / "summary.json"
    write_observation_summary(summary, output)

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["mode"] == "shadow"
    assert payload["observed_bars"] == 2
    assert payload["observed_events"] == 2
    assert payload["signal_count"] == 1
    assert payload["paper_intent_count"] == 0
    assert payload["fillable_ratio"] == pytest.approx(0.5)
    assert payload["partial_fill_ratio"] == pytest.approx(0.5)
    assert payload["cost_adjusted_edge_bps_mean"] == pytest.approx(1.0)
    assert payload["drawdown"] == pytest.approx(0.07)
    assert payload["metric_drift"] == pytest.approx(0.2)


def test_paper_event_aggregation_counts_paper_intents(tmp_path: Path) -> None:
    event_file = write_events(
        events_path(tmp_path),
        event_payload(mode="paper", paper_intent=True),
        event_payload(
            ts="2026-05-18T01:00:00+00:00",
            bar_ts="2026-05-18T01:00:00+00:00",
            mode="paper",
            paper_intent=True,
        ),
    )

    summary = build_observation_summary_from_events(
        load_observation_events_jsonl(event_file),
        recommendation_id="rec_obs_summary",
        candidate_id="cand_obs_summary",
        experiment_id="exp_obs_summary",
        mode="paper",
    )

    assert summary.mode == "paper"
    assert summary.paper_intent_count == 2
    assert summary.signal_count == 2


def test_identity_mismatch_rejects_events(tmp_path: Path) -> None:
    event_file = write_events(events_path(tmp_path), event_payload(candidate_id="cand_other"))

    with pytest.raises(ValueError, match="candidate_id"):
        build_observation_summary_from_events(
            load_observation_events_jsonl(event_file),
            recommendation_id="rec_obs_summary",
            candidate_id="cand_obs_summary",
            experiment_id="exp_obs_summary",
            mode="shadow",
        )


def test_empty_event_file_is_rejected(tmp_path: Path) -> None:
    event_file = events_path(tmp_path)
    event_file.parent.mkdir(parents=True)
    event_file.write_text("\n", encoding="utf-8")

    with pytest.raises(ValueError, match="at least one event"):
        load_observation_events_jsonl(event_file)


def test_output_path_must_stay_under_research_artifacts(tmp_path: Path) -> None:
    event_file = write_events(events_path(tmp_path), event_payload())
    summary = build_observation_summary_from_events(
        load_observation_events_jsonl(event_file),
        recommendation_id="rec_obs_summary",
        candidate_id="cand_obs_summary",
        experiment_id="exp_obs_summary",
        mode="shadow",
    )

    with pytest.raises(ValueError, match="under artifacts/research"):
        write_observation_summary(summary, tmp_path / "outside.json")


def test_summary_write_requires_overwrite_opt_in(tmp_path: Path) -> None:
    event_file = write_events(events_path(tmp_path), event_payload())
    summary = build_observation_summary_from_events(
        load_observation_events_jsonl(event_file),
        recommendation_id="rec_obs_summary",
        candidate_id="cand_obs_summary",
        experiment_id="exp_obs_summary",
        mode="shadow",
    )
    output = research_root(tmp_path) / "observation_inputs" / "summary.json"
    write_observation_summary(summary, output)

    with pytest.raises(ValueError, match="already exists"):
        write_observation_summary(summary, output)

    write_observation_summary(summary, output, overwrite=True)
