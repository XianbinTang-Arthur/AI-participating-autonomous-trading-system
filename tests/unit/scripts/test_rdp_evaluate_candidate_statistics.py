from __future__ import annotations

import pytest

from scripts.rdp_evaluate_candidate_statistics import evaluate_payload


def _payload() -> dict:
    returns = [0.003 + ((index % 5) - 2) * 0.0001 for index in range(120)]
    return {
        "candidate_id": "candidate-a",
        "dataset_fingerprint": "rfds_" + "a" * 64,
        "net_returns": returns,
        "candidate_p_values": {"candidate-a": 0.001, "candidate-b": 0.4},
        "trial_count": 2,
        "periods_per_year": 8760,
        "walk_forward": {
            "initial_train_size": 50,
            "test_size": 15,
            "purge_size": 2,
            "embargo_size": 1,
        },
        "statistics": {"block_size": 5, "replications": 200, "seed": 7},
    }


def test_statistics_payload_produces_v2_development_evidence() -> None:
    result = evaluate_payload(_payload())
    assert result["selection_protocol_version"] == "train_valid_selection_test_holdout_v2"
    assert result["benchmark_segment"] == "valid"
    assert result["walk_forward"]["format_version"] == 1
    assert result["statistics"]["format_version"] == 1


def test_candidate_must_be_part_of_multiple_testing_family() -> None:
    payload = _payload()
    payload["candidate_p_values"].pop("candidate-a")
    with pytest.raises(ValueError, match="candidate_id_missing"):
        evaluate_payload(payload)
