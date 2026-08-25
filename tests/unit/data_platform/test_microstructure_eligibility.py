from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aats.data_platform.quality.microstructure_eligibility import (
    MicrostructureEligibilityPolicy,
    MicrostructureWindowObservation,
    evaluate_microstructure_window,
)
from scripts.rdp_validate_microstructure_window import _load_collector_freshness


_START = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
_RUN_ID = "00000000-0000-0000-0000-000000000001"


def _observation(**overrides: object) -> MicrostructureWindowObservation:
    values: dict[str, object] = {
        "symbol": "btc-usdt-swap",
        "window_start": _START,
        "window_end": _START + timedelta(minutes=15),
        "bbo_samples_n": 900,
        "books5_samples_n": 900,
        "trade_count": 100,
        "oi_samples_n": 10,
        "funding_rate_present": True,
        "mark_price_present": True,
        "liquidation_event_count": 0,
        "microstructure_collector_fresh": True,
        "liquidations_collector_fresh": True,
        "dataset_versions": {
            "orderbook": "silver-v1",
            "trades": "silver-v1",
            "oi_funding": "silver-v1",
            "liquidations": "silver-v1",
        },
        "ingest_run_ids": {
            "orderbook": _RUN_ID,
            "trades": _RUN_ID,
            "oi_funding": _RUN_ID,
            "liquidations": _RUN_ID,
        },
        "quality_flags": {
            "orderbook": (),
            "trades": (),
            "oi_funding": (),
            "liquidations": ("liquidation_no_data",),
        },
    }
    values.update(overrides)
    return MicrostructureWindowObservation(**values)  # type: ignore[arg-type]


def test_zero_liquidation_events_are_eligible_when_sparse_collector_is_fresh() -> None:
    report = evaluate_microstructure_window(
        _observation(),
        evaluated_at=_START + timedelta(minutes=20),
    )

    assert report.eligible_for_research is True
    assert report.reason_codes == ()
    assert len(report.evidence_fingerprint) == 64
    assert report.to_dict()["observation"]["symbol"] == "BTC-USDT-SWAP"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"bbo_samples_n": 719}, "bbo_samples_below_minimum"),
        ({"books5_samples_n": 719}, "books5_samples_below_minimum"),
        ({"trade_count": 0}, "trade_count_below_minimum"),
        ({"oi_samples_n": 0}, "oi_samples_below_minimum"),
        ({"funding_rate_present": False}, "funding_rate_missing"),
        ({"mark_price_present": False}, "mark_price_missing"),
        (
            {"microstructure_collector_fresh": False},
            "microstructure_collector_not_fresh",
        ),
        (
            {"liquidations_collector_fresh": False},
            "liquidations_collector_not_fresh",
        ),
    ],
)
def test_required_channel_gaps_fail_closed(
    overrides: dict[str, object],
    reason: str,
) -> None:
    report = evaluate_microstructure_window(_observation(**overrides))

    assert report.eligible_for_research is False
    assert reason in report.reason_codes


def test_fatal_quality_flags_and_lineage_mismatch_fail_closed() -> None:
    report = evaluate_microstructure_window(
        _observation(
            dataset_versions={
                "orderbook": "silver-v1",
                "trades": "silver-v2",
                "oi_funding": "silver-v1",
                "liquidations": "silver-v1",
            },
            quality_flags={
                "orderbook": ("stale_source",),
                "trades": (),
                "oi_funding": (),
                "liquidations": ("liquidation_no_data",),
            },
        )
    )

    assert report.eligible_for_research is False
    assert "dataset_version_mismatch" in report.reason_codes
    assert "fatal_quality_flag:orderbook:stale_source" in report.reason_codes
    assert "fatal_quality_flag:liquidations:liquidation_no_data" not in report.reason_codes


def test_fingerprint_is_stable_and_excludes_evaluation_clock() -> None:
    observation = _observation()
    first = evaluate_microstructure_window(observation, evaluated_at=_START)
    second = evaluate_microstructure_window(
        observation,
        evaluated_at=_START + timedelta(days=1),
    )

    assert first.evidence_fingerprint == second.evidence_fingerprint
    changed_policy = replace(
        MicrostructureEligibilityPolicy(),
        min_trade_count=101,
    )
    third = evaluate_microstructure_window(observation, policy=changed_policy)
    assert third.evidence_fingerprint != first.evidence_fingerprint


def test_naive_window_timestamp_is_rejected() -> None:
    with pytest.raises(ValueError, match="window_start_must_be_timezone_aware"):
        _observation(window_start=datetime(2026, 8, 25, 12, 0))


def test_collector_packet_freshness_is_recomputed_not_trusted(
    tmp_path: Path,
) -> None:
    packet = tmp_path / "collectors.json"
    packet.write_text(
        json.dumps(
            {
                "collector_freshness": [
                    {
                        "name": "aats-microstructure-collector",
                        "fresh": True,
                        "heartbeat_at": (_START - timedelta(seconds=61)).isoformat(),
                    },
                    {
                        "name": "aats-liquidations-daemon",
                        "fresh": True,
                        "heartbeat_at": (_START - timedelta(seconds=10)).isoformat(),
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    result = _load_collector_freshness(packet, evaluated_at=_START)
    assert result["aats-microstructure-collector"] is False
    assert result["aats-liquidations-daemon"] is True
