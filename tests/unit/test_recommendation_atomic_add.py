from __future__ import annotations

from copy import deepcopy
from unittest.mock import patch

import pytest

from aats.data_platform.decision_system.recommendation_registry import (
    add_recommendation,
    reject_recommendation,
    supersede_recommendation,
)
from aats.data_platform.governance._exceptions import (
    DBConflictError,
    DBConstraintViolation,
)


def _recommendation(rec_id: str, *, reason: str) -> dict[str, object]:
    return {
        "recommendation_id": rec_id,
        "created_at": "2026-08-27T12:00:00+00:00",
        "family": "independent",
        "symbol": "BTC-USDT-SWAP",
        "timeframe": "15m",
        "recommendation_type": "parameter_upgrade",
        "target_parameter_set_id": "ps_candidate",
        "source_round_id": "20260827_120000_deadbeef",
        "confidence": "high",
        "reason": reason,
        "evidence_bundle_ref": "20260827_120000_deadbeef",
        "status": "draft",
    }


def test_atomic_add_failure_leaves_old_draft_and_registry_unchanged() -> None:
    old = _recommendation("rec_old", reason="old")
    new = _recommendation("rec_new", reason="new")
    registry = {
        "version": 4,
        "recommendations": [deepcopy(old)],
        "_governance_storage_mode": "managed_db",
    }

    with (
        patch(
            "aats.data_platform.decision_system.recommendation_registry."
            "_db_add_recommendation_atomic",
            side_effect=DBConflictError("recommendation_immutable_identity_conflict"),
        ),
        pytest.raises(DBConflictError),
    ):
        add_recommendation(registry, new)

    assert registry == {
        "version": 4,
        "recommendations": [old],
        "_governance_storage_mode": "managed_db",
    }


def test_atomic_add_replaces_local_snapshot_with_complete_db_truth() -> None:
    old = _recommendation("rec_old", reason="old")
    concurrent = _recommendation("rec_concurrent", reason="concurrent")
    concurrent["status"] = "superseded"
    new = _recommendation("rec_new", reason="new")
    canonical = {
        "recommendations": [
            {**old, "status": "superseded"},
            concurrent,
            new,
        ]
    }
    registry = {
        "version": 4,
        "recommendations": [deepcopy(old)],
        "_governance_storage_mode": "managed_db",
    }

    with patch(
        "aats.data_platform.decision_system.recommendation_registry."
        "_db_add_recommendation_atomic",
        return_value=canonical,
    ):
        add_recommendation(registry, new)

    assert registry["version"] == 4
    assert registry["recommendations"] == canonical["recommendations"]


@pytest.mark.parametrize("transition", ["reject", "supersede"])
def test_terminal_transition_constraint_failure_rolls_back_memory(
    transition: str,
) -> None:
    recommendation = _recommendation("rec_transition", reason="transition")
    original = deepcopy(recommendation)
    registry = {"recommendations": [recommendation]}

    with (
        patch(
            "aats.data_platform.decision_system.recommendation_registry."
            "_db_update_rec_status",
            side_effect=DBConstraintViolation("synthetic constraint failure"),
        ),
        pytest.raises(DBConstraintViolation),
    ):
        if transition == "reject":
            reject_recommendation(registry, "rec_transition")
        else:
            supersede_recommendation(registry, "rec_transition")

    assert recommendation == original
