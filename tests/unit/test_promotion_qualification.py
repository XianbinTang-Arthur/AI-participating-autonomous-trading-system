from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from aats.data_platform.decision_system import promotion_qualification as qualification
from aats.data_platform.decision_system.evidence_bundle import (
    COMBOS,
    PHASE2_PROMOTION_QUALIFICATION_POLICY,
)
from aats.data_platform.governance.decision_rounds_db import (
    db_load_decision_round_snapshot,
    db_load_decision_round_snapshots,
)
from aats.data_platform.governance.parameter_identity import (
    parameter_values_fingerprint,
)

ROUND_ID = "20260827_120000_deadbeef"
SOURCE_ROUND_ID = "20260827_103000_55667788"
STEP2_ROUND_ID = "20260827_100000_1234abcd"
STEP2_SNAPSHOT_SHA256 = "d" * 64
VALUES_FINGERPRINT = parameter_values_fingerprint({"entry_threshold": 1.25})
RESOLVED_VALUES_FINGERPRINT = parameter_values_fingerprint(
    {"entry_threshold": 1.25, "exit_threshold": 0.5}
)
STEP3_CANDIDATE_SHA256 = "c" * 64
PHASE3_ROUND_ID = "20260827_110000_aabbccdd"
PHASE4_ROUND_ID = "20260827_113000_11223344"
RECOMMENDATION_CREATED_AT = datetime.now(timezone.utc).isoformat()
READINESS_CHECKS = (
    "research_stability",
    "attribution_no_severe_issue",
    "execution_not_severe",
    "governance_healthy",
    "parameter_traceable",
    "has_promote_candidate",
    "has_keep_active_ft",
)


def _recommendation(**overrides):
    value = {
        "recommendation_id": "rec_001",
        "recommendation_type": "parameter_upgrade",
        "target_parameter_set_id": "ps_001",
        "source_round_id": SOURCE_ROUND_ID,
        "evidence_bundle_ref": ROUND_ID,
        "family": "independent",
        "timeframe": "1H",
        "symbol": "BTC-USDT-SWAP",
        "confidence": "high",
        "reason": "qualified",
        "created_at": RECOMMENDATION_CREATED_AT,
    }
    value.update(overrides)
    return value


def _candidate(**overrides):
    value = {
        "parameter_set_id": "ps_001",
        "parameter_values_fingerprint": VALUES_FINGERPRINT,
        "source_round_id": SOURCE_ROUND_ID,
        "family": "independent",
        "timeframe": "1hour",
        "symbol": "BTC-USDT-SWAP",
        "decision": "promote_candidate",
        "confidence": "high",
        "score_ratio": 0.9,
    }
    value.update(overrides)
    return value


def _evidence(*, reference_time=None, **phase2_overrides):
    reference_time = reference_time or datetime.now(timezone.utc)
    phase2 = {
        "promotion_qualification_policy": (
            PHASE2_PROMOTION_QUALIFICATION_POLICY
        ),
        "canonical_step2_round_id": STEP2_ROUND_ID,
        "canonical_step2_snapshot_sha256": STEP2_SNAPSHOT_SHA256,
        "combo_stats": {
            "independent_1h": {
                "available": True,
                "family": "independent",
                "timeframe": "1h",
                "combo_key": "independent_1h",
                "total_experiments": 2,
                "experiments_with_openings": 1,
                "max_opening_count": 3,
                "mean_positive_edge_ratio": 0.5,
            }
        },
    }
    phase2.update(phase2_overrides)
    return {
        "governance_index_used": {"active_round_index": True},
        "phase2_evidence": phase2,
        "phase3_evidence": {
            "source": "phase3",
            "evidence_source": "governance_index",
            "round_count": 1,
            "trusted_round_count": 1,
            "latest_round": {
                "round_id": PHASE3_ROUND_ID,
                "started_at": (reference_time - timedelta(minutes=10)).isoformat(),
                "status": "succeeded",
                "replay_only": False,
                "live_query_succeeded": True,
                "combos": {
                    combo["key"]: {
                        "status": "succeeded",
                        "live_query_succeeded": True,
                        "source_step3_round_id": SOURCE_ROUND_ID,
                        "parameter_values_fingerprint": VALUES_FINGERPRINT,
                        "resolved_parameter_values_fingerprint": (
                            RESOLVED_VALUES_FINGERPRINT
                        ),
                        "source_step3_candidate_sha256": STEP3_CANDIDATE_SHA256,
                        "alignment_stats": {
                            "aligned": 10,
                            "unattributable": 0,
                        },
                    }
                    for combo in COMBOS
                },
            },
        },
        "phase4_evidence": {
            "source": "phase4",
            "evidence_source": "governance_index",
            "round_count": 1,
            "trusted_round_count": 1,
            "latest_round": {
                "round_id": PHASE4_ROUND_ID,
                "started_at": (reference_time - timedelta(minutes=5)).isoformat(),
                "status": "succeeded",
                "combos": {
                    combo["key"]: {
                        "status": "succeeded",
                        "source_step3_round_id": SOURCE_ROUND_ID,
                        "parameter_values_fingerprint": VALUES_FINGERPRINT,
                        "resolved_parameter_values_fingerprint": (
                            RESOLVED_VALUES_FINGERPRINT
                        ),
                        "source_step3_candidate_sha256": STEP3_CANDIDATE_SHA256,
                        "cost_summary": {
                            "total_candidates": 10,
                            "cost_adjusted_edge_mean": 1.0,
                            "full_fill_ratio": 0.8,
                        },
                    }
                    for combo in COMBOS
                },
            },
        },
        "phase5_governance_evidence": {
            "quality_health": "healthy",
            "frozen_parameter_sets": [{"parameter_set_id": "ps_001"}],
            "candidate_parameter_sets": [],
        },
    }


def _ft_decisions():
    return [
        {
            "family": "independent",
            "timeframe": "1h",
            "combo_key": "independent_1h",
            "decision": "keep_active",
            "confidence": "high",
        }
    ]


def _control_plane_publication(*recommendations):
    mapped = []
    for producer_index, recommendation in enumerate(recommendations):
        entry = dict(recommendation)
        entry["timeframe"] = qualification.normalize_timeframe_value(
            entry["timeframe"]
        )
        entry["producer_index"] = producer_index
        mapped.append(entry)
    return {
        "schema_version": "aats.phase6.control_plane_publication.v1",
        "recommendations": mapped,
        "active_decisions": [],
        "evidence_bundle": {},
    }


def _manifest(*, candidates, **overrides):
    finished_at = overrides.get("finished_at", datetime.now(timezone.utc).isoformat())
    try:
        started_at = (
            datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
            - timedelta(minutes=5)
        ).isoformat()
    except (TypeError, ValueError):
        started_at = finished_at
    value = {
        "round_id": ROUND_ID,
        "phase": "phase6",
        "status": "succeeded",
        "started_at": started_at,
        "finished_at": finished_at,
        "readiness": "ready_for_next_live_test",
        "scope": {
            "symbol": "BTC-USDT-SWAP",
            "families": ["independent"],
            "timeframes": ["1h"],
        },
        "upgrade_candidates_count": len(candidates),
        "ft_decisions_count": 1,
        "output_refs": {
            "evidence_summary": "evidence_summary.json",
            "upgrade_candidates": "parameter_upgrade_candidates.json",
            "ft_decisions": "family_timeframe_decisions.json",
            "readiness_report": "promotion_readiness_report.json",
        },
        "control_plane_publication": _control_plane_publication(
            _recommendation()
        ),
    }
    value.update(overrides)
    return value


def _readiness(
    *,
    candidates,
    manifest,
    evidence=None,
    ft_decisions=None,
    **overrides,
):
    evidence = _evidence() if evidence is None else evidence
    ft_decisions = _ft_decisions() if ft_decisions is None else ft_decisions
    value = qualification.evaluate_promotion_readiness(
        evidence,
        candidates,
        ft_decisions,
    )
    value["generated_at"] = manifest["finished_at"]
    value.update(overrides)
    return value


def _snapshot(
    *,
    candidates=None,
    evidence=None,
    manifest=None,
    ft_decisions=None,
    readiness=None,
):
    candidates = [_candidate()] if candidates is None else candidates
    ft_decisions = _ft_decisions() if ft_decisions is None else ft_decisions
    manifest = manifest or _manifest(candidates=candidates)
    if evidence is None:
        evidence = _evidence(
            reference_time=datetime.fromisoformat(
                manifest["started_at"].replace("Z", "+00:00")
            )
        )
    manifest["ft_decisions_count"] = len(ft_decisions)
    if readiness is None:
        readiness = _readiness(
            candidates=candidates,
            manifest=manifest,
            evidence=evidence,
            ft_decisions=ft_decisions,
        )
    return {
        "round_id": ROUND_ID,
        "started_at": manifest.get("started_at"),
        "finished_at": manifest.get("finished_at"),
        "manifest": manifest,
        "evidence_bundle_summary": evidence,
        "parameter_upgrade_candidates": candidates,
        "family_timeframe_decisions": ft_decisions,
        "promotion_readiness_assessment": readiness,
    }


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )


def _write_round(
    project_root: Path,
    *,
    candidates=None,
    evidence=None,
    manifest=None,
    ft_decisions=None,
    readiness=None,
) -> Path:
    candidates = [_candidate()] if candidates is None else candidates
    ft_decisions = _ft_decisions() if ft_decisions is None else ft_decisions
    manifest = manifest or _manifest(candidates=candidates)
    if evidence is None:
        evidence = _evidence(
            reference_time=datetime.fromisoformat(
                manifest["started_at"].replace("Z", "+00:00")
            )
        )
    manifest["ft_decisions_count"] = len(ft_decisions)
    if readiness is None:
        readiness = _readiness(
            candidates=candidates,
            manifest=manifest,
            evidence=evidence,
            ft_decisions=ft_decisions,
        )
    round_dir = project_root / "artifacts" / "decision_rounds" / ROUND_ID
    _write_json(round_dir / "round_manifest.json", manifest)
    _write_json(
        round_dir / "evidence_summary.json",
        evidence,
    )
    _write_json(round_dir / "parameter_upgrade_candidates.json", candidates)
    _write_json(round_dir / "family_timeframe_decisions.json", ft_decisions)
    _write_json(round_dir / "promotion_readiness_report.json", readiness)
    return round_dir


@pytest.fixture(autouse=True)
def _file_mode(monkeypatch):
    monkeypatch.setattr(
        qualification,
        "has_explicit_governance_db_configuration",
        lambda _root: False,
    )
    monkeypatch.setattr(
        qualification,
        "try_governance_db",
        lambda: (None, False),
    )

    artifact = SimpleNamespace(
        candidate_sha256=STEP3_CANDIDATE_SHA256,
        metadata={
            "round_id": SOURCE_ROUND_ID,
            "status": "succeeded",
            "symbol": "BTC-USDT-SWAP",
            "dataset_version": "v1.0",
        },
        payload={
            "candidates": {
                "independent_1h": {"entry_threshold": 1.25},
            }
        },
    )

    def _load_formal_candidate(
        project_root,
        candidate_path,
        *,
        expected_round_id=None,
        expected_candidate_sha256=None,
    ):
        expected_path = (
            project_root
            / "artifacts/research/step3_rounds"
            / SOURCE_ROUND_ID
            / "parameter_candidates_merged.json"
        )
        if (
            candidate_path != expected_path
            or expected_round_id != SOURCE_ROUND_ID
            or expected_candidate_sha256 != STEP3_CANDIDATE_SHA256
        ):
            return None
        return artifact

    monkeypatch.setattr(
        qualification,
        "load_validated_formal_step3_candidate",
        _load_formal_candidate,
    )
    monkeypatch.setattr(
        qualification,
        "materialize_validated_step3_parameter_sets",
        lambda _artifact, *, initial_status: [
            {
                "parameter_set_id": "ps_001",
                "family": "independent",
                "symbol": "BTC-USDT-SWAP",
                "timeframe": "1h",
                "source_round_id": SOURCE_ROUND_ID,
                "source_phase": "step3_merged",
                "values": {"entry_threshold": 1.25},
                "status": initial_status,
            }
        ],
    )
    monkeypatch.setattr(
        qualification,
        "load_research_round_snapshot",
        lambda *, round_id, project_root, require_managed_db_truth: {
            "data_source": "db",
            "round_id": round_id,
            "phase": qualification.ROUND_PHASE_STEP3,
            "status": "succeeded",
            "artifacts": {
                "step2_round_id": STEP2_ROUND_ID,
                "step2_snapshot_sha256": STEP2_SNAPSHOT_SHA256,
            },
        },
    )


def test_non_apply_recommendation_does_not_load_any_round(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        qualification,
        "try_governance_db",
        Mock(side_effect=AssertionError("DB must not be consulted")),
    )

    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(
            recommendation_type="pause",
            target_parameter_set_id=None,
            evidence_bundle_ref=None,
        ),
    )

    assert verdict.required is False
    assert verdict.eligible is True
    assert verdict.reason_code == "not_required"
    assert verdict.qualified_round_id is None


def test_legacy_missing_type_with_target_is_apply_capable(tmp_path: Path) -> None:
    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(recommendation_type=None, evidence_bundle_ref=None),
    )

    assert verdict.required is True
    assert verdict.eligible is False
    assert verdict.reason_code == "evidence_bundle_ref_required"


@pytest.mark.parametrize("recommendation_type", ["pause", "bogus_type"])
def test_non_apply_or_unknown_type_with_target_cannot_bypass_qualification(
    tmp_path: Path,
    recommendation_type: str,
) -> None:
    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(recommendation_type=recommendation_type),
    )

    assert verdict.required is True
    assert verdict.eligible is False
    assert verdict.reason_code == "recommendation_invalid"


@pytest.mark.parametrize("recommendation_type", [123, "", " pause "])
def test_malformed_non_null_type_is_not_treated_as_legacy_missing(
    tmp_path: Path,
    recommendation_type,
) -> None:
    _write_round(tmp_path)

    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(recommendation_type=recommendation_type),
    )

    assert verdict.required is True
    assert verdict.eligible is False
    assert verdict.reason_code == "recommendation_invalid"


@pytest.mark.parametrize("target", [123, "", " ps_001 "])
def test_malformed_non_null_target_is_not_treated_as_absent(
    tmp_path: Path,
    target,
) -> None:
    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(
            recommendation_type="pause",
            target_parameter_set_id=target,
        ),
    )

    assert verdict.required is True
    assert verdict.eligible is False
    assert verdict.reason_code == "recommendation_invalid"


def test_exact_file_round_qualifies_and_normalizes_timeframe(tmp_path: Path) -> None:
    _write_round(tmp_path)

    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(),
    )

    assert verdict.required is True
    assert verdict.eligible is True
    assert verdict.reason_code == "qualified"
    assert verdict.qualified_round_id == ROUND_ID
    payload = verdict.to_dict()
    assert isinstance(payload["qualified_finished_at"], str)
    assert payload == {
        "required": True,
        "eligible": True,
        "reason_code": "qualified",
        "evidence_bundle_ref": ROUND_ID,
        "source_round_id": SOURCE_ROUND_ID,
        "qualified_round_id": ROUND_ID,
        "qualified_finished_at": verdict.qualified_finished_at,
        "parameter_values_fingerprint": VALUES_FINGERPRINT,
        "detail": "精确引用的 Phase 6 round 与参数升级候选资格一致。",
    }


def test_fabricated_recommendation_id_cannot_reuse_canonical_round(
    tmp_path: Path,
) -> None:
    _write_round(tmp_path)

    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(recommendation_id="rec_fabricated"),
    )

    assert verdict.required is True
    assert verdict.eligible is False
    assert (
        verdict.reason_code
        == "promotion_recommendation_publication_mismatch"
    )


def test_fabricated_recommendation_id_is_blocked_before_apply_parameter_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aats.data_platform.decision_system import (
        recommendation_registry,
    )
    from aats.data_platform.decision_system.active_parameter_apply import (
        apply_approved_recommendation,
    )
    from aats.data_platform.governance import parameter_registry

    _write_round(tmp_path)
    fabricated = _recommendation(
        recommendation_id="rec_fabricated",
        status="approved",
    )
    parameter_read = Mock()
    for profile_name in (
        "AATS_PROFILE",
        "AATS_ENV_TEMPLATE_PROFILE",
        "AATS_STARTUP_PROFILE",
    ):
        monkeypatch.delenv(profile_name, raising=False)
    monkeypatch.setenv("RDP_ENV", "dev")
    monkeypatch.setattr(
        recommendation_registry,
        "load_recommendation_registry",
        lambda _path: {"recommendations": [fabricated]},
    )
    monkeypatch.setattr(
        recommendation_registry,
        "find_recommendation",
        lambda _registry, _recommendation_id: fabricated,
    )
    monkeypatch.setattr(parameter_registry, "load_registry", parameter_read)

    result = apply_approved_recommendation(
        tmp_path,
        recommendation_id="rec_fabricated",
        dry_run=True,
    )

    assert result["code"] == "promotion_qualification_blocked"
    assert result["promotion_qualification"]["reason_code"] == (
        "promotion_recommendation_publication_mismatch"
    )
    parameter_read.assert_not_called()


def test_missing_formal_step3_candidate_blocks_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_round(tmp_path)
    monkeypatch.setattr(
        qualification,
        "load_validated_formal_step3_candidate",
        lambda *_args, **_kwargs: None,
    )

    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(),
    )

    assert verdict.eligible is False
    assert verdict.reason_code == "promotion_candidate_step3_lineage_invalid"


def test_phase34_candidate_sha_must_match_formal_step3_candidate(
    tmp_path: Path,
) -> None:
    evidence = deepcopy(_evidence())
    for phase_key in ("phase3_evidence", "phase4_evidence"):
        evidence[phase_key]["latest_round"]["combos"]["independent_1h"][
            "source_step3_candidate_sha256"
        ] = "d" * 64
    _write_round(tmp_path, evidence=evidence)

    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(),
    )

    assert verdict.eligible is False
    assert verdict.reason_code == "promotion_candidate_step3_lineage_invalid"


@pytest.mark.parametrize(
    "mutation",
    [
        "evidence_round_missing",
        "evidence_round_drift",
        "evidence_sha_missing",
        "evidence_sha_drift",
        "managed_round_missing",
        "managed_round_drift",
        "managed_sha_missing",
        "managed_sha_drift",
        "managed_snapshot_not_db",
    ],
)
def test_step2_evidence_must_match_step3_managed_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    evidence = deepcopy(_evidence())
    phase2 = evidence["phase2_evidence"]
    step3_snapshot = {
        "data_source": "db",
        "round_id": SOURCE_ROUND_ID,
        "phase": qualification.ROUND_PHASE_STEP3,
        "status": "succeeded",
        "artifacts": {
            "step2_round_id": STEP2_ROUND_ID,
            "step2_snapshot_sha256": STEP2_SNAPSHOT_SHA256,
        },
    }
    if mutation == "evidence_round_missing":
        phase2.pop("canonical_step2_round_id")
    elif mutation == "evidence_round_drift":
        phase2["canonical_step2_round_id"] = "20260827_095959_87654321"
    elif mutation == "evidence_sha_missing":
        phase2.pop("canonical_step2_snapshot_sha256")
    elif mutation == "evidence_sha_drift":
        phase2["canonical_step2_snapshot_sha256"] = "e" * 64
    elif mutation == "managed_round_missing":
        step3_snapshot["artifacts"].pop("step2_round_id")
    elif mutation == "managed_round_drift":
        step3_snapshot["artifacts"]["step2_round_id"] = (
            "20260827_095959_87654321"
        )
    elif mutation == "managed_sha_missing":
        step3_snapshot["artifacts"].pop("step2_snapshot_sha256")
    elif mutation == "managed_sha_drift":
        step3_snapshot["artifacts"]["step2_snapshot_sha256"] = "e" * 64
    elif mutation == "managed_snapshot_not_db":
        step3_snapshot["data_source"] = "file"
    else:  # pragma: no cover - parametrization is exhaustive
        raise AssertionError(mutation)
    monkeypatch.setattr(
        qualification,
        "load_research_round_snapshot",
        lambda **_kwargs: step3_snapshot,
    )
    _write_round(tmp_path, evidence=evidence)

    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(),
    )

    assert verdict.eligible is False
    assert verdict.reason_code == "promotion_candidate_step3_lineage_invalid"


@pytest.mark.parametrize(
    "formal_parameter_set",
    [
        {
            "parameter_set_id": "ps_other",
            "family": "independent",
            "symbol": "BTC-USDT-SWAP",
            "timeframe": "1h",
            "source_round_id": SOURCE_ROUND_ID,
            "source_phase": "step3_merged",
            "values": {"entry_threshold": 1.25},
        },
        {
            "parameter_set_id": "ps_001",
            "family": "independent",
            "symbol": "BTC-USDT-SWAP",
            "timeframe": "1h",
            "source_round_id": SOURCE_ROUND_ID,
            "source_phase": "step3_merged",
            "values": {"entry_threshold": 9.99},
        },
    ],
)
def test_target_parameter_set_must_equal_formal_import_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    formal_parameter_set: dict[str, object],
) -> None:
    _write_round(tmp_path)
    monkeypatch.setattr(
        qualification,
        "materialize_validated_step3_parameter_sets",
        lambda _artifact, *, initial_status: [
            {**formal_parameter_set, "status": initial_status}
        ],
    )

    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(),
    )

    assert verdict.eligible is False
    assert verdict.reason_code == "promotion_candidate_step3_lineage_invalid"


def test_valid_not_ready_report_blocks_promotion(tmp_path: Path) -> None:
    candidates = [_candidate()]
    evidence = deepcopy(_evidence())
    evidence["phase3_evidence"]["latest_round"]["live_query_succeeded"] = False
    manifest = _manifest(candidates=candidates)
    readiness = _readiness(
        candidates=candidates,
        manifest=manifest,
        evidence=evidence,
    )
    manifest["readiness"] = readiness["readiness"]
    _write_round(
        tmp_path,
        candidates=candidates,
        evidence=evidence,
        manifest=manifest,
        readiness=readiness,
    )

    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(),
    )

    assert verdict.reason_code == "promotion_round_not_ready"
    assert verdict.eligible is False


@pytest.mark.parametrize(
    ("mutation", "reason_code"),
    [
        ("missing_field", "promotion_round_readiness_invalid"),
        ("extra_field", "promotion_round_readiness_invalid"),
        ("manifest_mismatch", "promotion_round_readiness_invalid"),
        ("generated_before_start", "promotion_round_readiness_invalid"),
        ("wrong_check_order", "promotion_round_readiness_invalid"),
        ("wrong_counts", "promotion_round_readiness_invalid"),
        ("wrong_blockers", "promotion_round_readiness_invalid"),
        ("duplicate_promoted", "promotion_round_readiness_invalid"),
        ("target_score_mismatch", "promotion_candidate_readiness_mismatch"),
        ("target_not_active", "promotion_round_readiness_invalid"),
    ],
)
def test_readiness_contract_and_target_binding_fail_closed(
    tmp_path: Path,
    mutation: str,
    reason_code: str,
) -> None:
    candidates = [_candidate()]
    ft_decisions = _ft_decisions()
    manifest = _manifest(candidates=candidates)
    readiness = _readiness(candidates=candidates, manifest=manifest)
    if mutation == "missing_field":
        readiness.pop("checks_failed")
    elif mutation == "extra_field":
        readiness["schema_version"] = "unrecognized/v99"
    elif mutation == "manifest_mismatch":
        manifest["readiness"] = "not_ready_more_research_needed"
    elif mutation == "generated_before_start":
        readiness["generated_at"] = (
            datetime.fromisoformat(manifest["started_at"]) - timedelta(seconds=1)
        ).isoformat()
    elif mutation == "wrong_check_order":
        readiness["checks"][0], readiness["checks"][1] = (
            readiness["checks"][1],
            readiness["checks"][0],
        )
    elif mutation == "wrong_counts":
        readiness["checks_passed"] -= 1
        readiness["checks_failed"] += 1
    elif mutation == "wrong_blockers":
        readiness["blockers"] = ["synthetic blocker"]
    elif mutation == "duplicate_promoted":
        readiness["promoted_candidates"].append(
            deepcopy(readiness["promoted_candidates"][0])
        )
    elif mutation == "target_score_mismatch":
        readiness["promoted_candidates"][0]["score_ratio"] = 0.8
    elif mutation == "target_not_active":
        ft_decisions = [
            {
                "family": "directional",
                "timeframe": "1h",
                "combo_key": "directional_1h",
                "decision": "keep_active",
                "confidence": "high",
            }
        ]
        readiness = _readiness(
            candidates=candidates,
            manifest=manifest,
            ft_decisions=ft_decisions,
        )
    else:  # pragma: no cover - parametrization is exhaustive
        raise AssertionError(mutation)
    _write_round(
        tmp_path,
        candidates=candidates,
        manifest=manifest,
        ft_decisions=ft_decisions,
        readiness=readiness,
    )

    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(),
    )

    assert verdict.reason_code == reason_code
    assert verdict.eligible is False


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_ref",
        "escaping_ref",
        "missing_file",
        "missing_ft_ref",
        "escaping_ft_ref",
        "missing_ft_file",
    ],
)
def test_readiness_output_is_an_exact_required_round_artifact(
    tmp_path: Path,
    mutation: str,
) -> None:
    candidates = [_candidate()]
    manifest = _manifest(candidates=candidates)
    if mutation == "missing_ref":
        manifest["output_refs"].pop("readiness_report")
    elif mutation == "escaping_ref":
        manifest["output_refs"]["readiness_report"] = (
            "../promotion_readiness_report.json"
        )
    elif mutation == "missing_ft_ref":
        manifest["output_refs"].pop("ft_decisions")
    elif mutation == "escaping_ft_ref":
        manifest["output_refs"]["ft_decisions"] = (
            "../family_timeframe_decisions.json"
        )
    round_dir = _write_round(tmp_path, candidates=candidates, manifest=manifest)
    if mutation == "missing_file":
        (round_dir / "promotion_readiness_report.json").unlink()
    elif mutation == "missing_ft_file":
        (round_dir / "family_timeframe_decisions.json").unlink()

    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(),
    )

    assert verdict.reason_code == "promotion_round_output_ref_invalid"
    assert verdict.eligible is False


def test_readiness_generated_at_accepts_equivalent_explicit_offset(
    tmp_path: Path,
) -> None:
    candidates = [_candidate()]
    manifest = _manifest(candidates=candidates)
    readiness = _readiness(candidates=candidates, manifest=manifest)
    generated_at = datetime.fromisoformat(readiness["generated_at"])
    readiness["generated_at"] = generated_at.astimezone(
        timezone(-timedelta(hours=4))
    ).isoformat()
    _write_round(
        tmp_path,
        candidates=candidates,
        manifest=manifest,
        readiness=readiness,
    )

    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(),
    )

    assert verdict.reason_code == "qualified"
    assert verdict.eligible is True


@pytest.mark.parametrize("mutation", ["governance_changed", "ft_decisions_changed"])
def test_readiness_is_recomputed_from_exact_round_inputs(
    tmp_path: Path,
    mutation: str,
) -> None:
    candidates = [_candidate()]
    evidence = _evidence()
    ft_decisions = _ft_decisions()
    manifest = _manifest(candidates=candidates)
    readiness = _readiness(
        candidates=candidates,
        manifest=manifest,
        evidence=evidence,
        ft_decisions=ft_decisions,
    )
    if mutation == "governance_changed":
        evidence["phase5_governance_evidence"]["quality_health"] = "unhealthy"
    else:
        ft_decisions[0]["decision"] = "lower_priority"
    _write_round(
        tmp_path,
        candidates=candidates,
        evidence=evidence,
        manifest=manifest,
        ft_decisions=ft_decisions,
        readiness=readiness,
    )

    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(),
    )

    assert verdict.reason_code == "promotion_round_readiness_invalid"
    assert verdict.eligible is False


@pytest.mark.parametrize("mutation", ["duplicate_combo", "identity_mismatch"])
def test_ft_decision_identity_and_uniqueness_are_strict(
    tmp_path: Path,
    mutation: str,
) -> None:
    candidates = [_candidate()]
    ft_decisions = _ft_decisions()
    if mutation == "duplicate_combo":
        ft_decisions.append(
            {
                "family": "independent",
                "timeframe": "1h",
                "combo_key": "independent_1h",
                "decision": "pause",
                "confidence": "high",
            }
        )
    else:
        ft_decisions[0]["family"] = "directional"
    manifest = _manifest(candidates=candidates)
    readiness = _readiness(
        candidates=candidates,
        manifest=manifest,
        ft_decisions=ft_decisions,
    )
    _write_round(
        tmp_path,
        candidates=candidates,
        manifest=manifest,
        ft_decisions=ft_decisions,
        readiness=readiness,
    )

    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(),
    )

    assert verdict.reason_code == "promotion_round_readiness_invalid"
    assert verdict.eligible is False


def test_equivalent_explicit_offsets_are_the_same_canonical_finish_time() -> None:
    finished_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    offset = timezone(-timedelta(hours=4))
    snapshot = {"finished_at": finished_at.astimezone(offset).isoformat()}
    manifest = {"finished_at": finished_at.isoformat()}

    canonical = qualification._canonical_round_finished_at(snapshot, manifest)

    assert canonical == finished_at


@pytest.mark.parametrize(
    ("snapshot_finished_at", "manifest_finished_at"),
    [
        ("2026-08-27T12:00:00", "2026-08-27T12:00:00+00:00"),
        ("2026-08-27T12:00:00+00:00", "2026-08-27T12:01:00+00:00"),
    ],
)
def test_naive_or_inconsistent_finish_times_remain_invalid(
    snapshot_finished_at: str,
    manifest_finished_at: str,
) -> None:
    assert qualification._canonical_round_finished_at(
        {"finished_at": snapshot_finished_at},
        {"finished_at": manifest_finished_at},
    ) is None


@pytest.mark.parametrize(
    ("age_hours", "expected_reason"),
    [
        (167.9, "qualified"),
        (169.0, "promotion_round_stale"),
    ],
)
def test_exact_round_freshness_is_part_of_qualification(
    tmp_path: Path,
    age_hours: float,
    expected_reason: str,
) -> None:
    candidates = [_candidate()]
    finished_at = (
        datetime.now(timezone.utc) - timedelta(hours=age_hours)
    ).isoformat()
    manifest = _manifest(
        candidates=candidates,
        finished_at=finished_at,
    )
    _write_round(tmp_path, candidates=candidates, manifest=manifest)

    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(),
    )

    assert verdict.reason_code == expected_reason
    assert verdict.eligible is (expected_reason == "qualified")


@pytest.mark.parametrize(
    "evidence_ref",
    [
        "../20260827_120000_deadbeef",
        "artifacts/decision_rounds/20260827_120000_deadbeef",
        "C:\\outside\\20260827_120000_deadbeef",
        "20260827_120000_DEADBEEF",
    ],
)
def test_noncanonical_or_escaping_evidence_ref_fails_closed(
    tmp_path: Path,
    evidence_ref: str,
) -> None:
    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(evidence_bundle_ref=evidence_ref),
    )

    assert verdict.reason_code == "evidence_bundle_ref_invalid"
    assert verdict.eligible is False


@pytest.mark.parametrize(
    ("manifest_change", "reason_code"),
    [
        ({"round_id": "20260827_120001_bad00000"}, "promotion_round_id_mismatch"),
        ({"phase": "phase5"}, "promotion_round_phase_invalid"),
        ({"status": "running"}, "promotion_round_status_invalid"),
        (
            {
                "scope": {
                    "symbol": "ETH-USDT-SWAP",
                    "families": ["independent"],
                    "timeframes": ["1h"],
                }
            },
            "promotion_round_scope_mismatch",
        ),
    ],
)
def test_manifest_identity_phase_status_and_scope_are_strict(
    tmp_path: Path,
    manifest_change: dict,
    reason_code: str,
) -> None:
    candidates = [_candidate()]
    manifest = _manifest(candidates=candidates, **manifest_change)
    _write_round(tmp_path, candidates=candidates, manifest=manifest)

    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(),
    )

    assert verdict.reason_code == reason_code
    assert verdict.eligible is False


def test_output_ref_cannot_escape_exact_round(tmp_path: Path) -> None:
    candidates = [_candidate()]
    manifest = _manifest(candidates=candidates)
    manifest["output_refs"]["evidence_summary"] = "../evidence_summary.json"
    _write_round(tmp_path, candidates=candidates, manifest=manifest)

    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(),
    )

    assert verdict.reason_code == "promotion_round_output_ref_invalid"


def test_manifest_candidate_count_must_match_exact_output(tmp_path: Path) -> None:
    candidates = [_candidate()]
    manifest = _manifest(candidates=candidates, upgrade_candidates_count=2)
    _write_round(tmp_path, candidates=candidates, manifest=manifest)

    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(),
    )

    assert verdict.reason_code == "promotion_candidate_count_mismatch"


@pytest.mark.parametrize(
    ("phase2_change", "reason_code"),
    [
        (
            {"promotion_qualification_policy": "legacy-policy/v0"},
            "promotion_policy_unsupported",
        ),
        ({"combo_stats": {}}, "promotion_combo_unavailable"),
        (
            {
                "combo_stats": {
                    "independent_1h": {
                        "available": True,
                        "family": "independent",
                        "timeframe": "1h",
                        "combo_key": "independent_1h",
                        "total_experiments": 1,
                        "experiments_with_openings": 0,
                        "max_opening_count": 0,
                    }
                }
            },
            "promotion_combo_unavailable",
        ),
        (
            {
                "combo_stats": {
                    "independent_1h": {
                        "available": True,
                        "family": "independent",
                        "timeframe": "1h",
                        "combo_key": "independent_1h",
                        "total_experiments": 1,
                        "experiments_with_openings": 1,
                        "max_opening_count": 1,
                        "mean_positive_edge_ratio": 0.0,
                    }
                }
            },
            "promotion_combo_unavailable",
        ),
    ],
)
def test_current_phase2_policy_and_opening_stats_are_required(
    tmp_path: Path,
    phase2_change: dict,
    reason_code: str,
) -> None:
    _write_round(tmp_path, evidence=_evidence(**phase2_change))

    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(),
    )

    assert verdict.reason_code == reason_code
    assert verdict.eligible is False


@pytest.mark.parametrize(
    "mutation",
    [
        "latest_round_id_invalid",
        "trusted_round_missing",
        "latest_round_partial",
        "target_combo_missing",
        "target_combo_partial",
        "target_live_query_failed",
        "target_zero_alignment",
        "target_source_round_missing",
        "target_source_round_mismatch",
        "target_fingerprint_missing",
        "target_fingerprint_mismatch",
        "target_resolved_fingerprint_missing",
        "target_resolved_fingerprint_mismatch",
        "target_candidate_sha_missing",
        "target_candidate_sha_invalid",
        "directory_scan_source",
        "active_index_untrusted",
        "stale_latest_round",
        "future_latest_round",
    ],
)
def test_target_phase3_evidence_must_independently_qualify(
    tmp_path: Path,
    mutation: str,
) -> None:
    candidates = [_candidate()]
    evidence = deepcopy(_evidence())
    phase3 = evidence["phase3_evidence"]
    latest = phase3["latest_round"]
    latest["combos"]["directional_1h"] = {
        "status": "succeeded",
        "live_query_succeeded": True,
        "alignment_stats": {"aligned": 5, "unattributable": 0},
    }
    target = latest["combos"]["independent_1h"]
    if mutation == "latest_round_id_invalid":
        latest["round_id"] = "phase3_latest"
    elif mutation == "trusted_round_missing":
        phase3["trusted_round_count"] = 0
    elif mutation == "latest_round_partial":
        latest["status"] = "partial_success"
    elif mutation == "target_combo_missing":
        latest["combos"].pop("independent_1h")
    elif mutation == "target_combo_partial":
        target["status"] = "partial_success"
    elif mutation == "target_live_query_failed":
        target["live_query_succeeded"] = False
    elif mutation == "target_zero_alignment":
        target["alignment_stats"]["aligned"] = 0
    elif mutation == "target_source_round_missing":
        target.pop("source_step3_round_id")
    elif mutation == "target_source_round_mismatch":
        target["source_step3_round_id"] = "phase2_source_other"
    elif mutation == "target_fingerprint_missing":
        target.pop("parameter_values_fingerprint")
    elif mutation == "target_fingerprint_mismatch":
        target["parameter_values_fingerprint"] = "b" * 64
    elif mutation == "target_resolved_fingerprint_missing":
        target.pop("resolved_parameter_values_fingerprint")
    elif mutation == "target_resolved_fingerprint_mismatch":
        target["resolved_parameter_values_fingerprint"] = "B" * 64
    elif mutation == "target_candidate_sha_missing":
        target.pop("source_step3_candidate_sha256")
    elif mutation == "target_candidate_sha_invalid":
        target["source_step3_candidate_sha256"] = "not-a-sha256"
    elif mutation == "directory_scan_source":
        phase3["evidence_source"] = "directory_scan"
    elif mutation == "active_index_untrusted":
        evidence["governance_index_used"]["active_round_index"] = False
    elif mutation == "stale_latest_round":
        latest["started_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=169)
        ).isoformat()
    elif mutation == "future_latest_round":
        latest["started_at"] = (
            datetime.now(timezone.utc) + timedelta(hours=1)
        ).isoformat()
    else:  # pragma: no cover - parametrization is exhaustive
        raise AssertionError(mutation)
    manifest = _manifest(candidates=candidates)
    readiness = _readiness(
        candidates=candidates,
        manifest=manifest,
        evidence=evidence,
    )
    readiness_blocking_mutations = {
        "latest_round_partial",
        "target_combo_missing",
        "target_combo_partial",
    }
    expected_readiness = (
        "not_ready_attribution_issue"
        if mutation in readiness_blocking_mutations
        else "ready_for_next_live_test"
    )
    assert readiness["readiness"] == expected_readiness
    manifest["readiness"] = readiness["readiness"]
    _write_round(
        tmp_path,
        candidates=candidates,
        evidence=evidence,
        manifest=manifest,
        readiness=readiness,
    )

    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(),
    )

    expected_reason = (
        "promotion_round_not_ready"
        if mutation in readiness_blocking_mutations
        else "promotion_candidate_phase3_evidence_invalid"
    )
    assert verdict.reason_code == expected_reason
    assert verdict.eligible is False


@pytest.mark.parametrize(
    "mutation",
    [
        "latest_round_id_invalid",
        "trusted_round_missing",
        "latest_round_partial",
        "target_combo_missing",
        "target_combo_failed",
        "target_zero_candidates",
        "target_negative_edge",
        "target_low_fill",
        "target_impossible_fill",
        "target_source_round_missing",
        "target_source_round_mismatch",
        "target_fingerprint_missing",
        "target_fingerprint_mismatch",
        "target_resolved_fingerprint_missing",
        "target_resolved_fingerprint_mismatch",
        "target_candidate_sha_missing",
        "target_candidate_sha_mismatch",
        "directory_scan_source",
        "stale_latest_round",
        "future_latest_round",
    ],
)
def test_target_phase4_evidence_must_independently_qualify(
    tmp_path: Path,
    mutation: str,
) -> None:
    candidates = [_candidate()]
    evidence = deepcopy(_evidence())
    phase4 = evidence["phase4_evidence"]
    latest = phase4["latest_round"]
    latest["combos"]["directional_1h"] = {
        "status": "succeeded",
        "cost_summary": {
            "total_candidates": 5,
            "cost_adjusted_edge_mean": 1.0,
            "full_fill_ratio": 0.8,
        },
    }
    target = latest["combos"]["independent_1h"]
    target_cost = target["cost_summary"]
    if mutation == "latest_round_id_invalid":
        latest["round_id"] = "phase4_latest"
    elif mutation == "trusted_round_missing":
        phase4["trusted_round_count"] = 0
    elif mutation == "latest_round_partial":
        latest["status"] = "partial_success"
    elif mutation == "target_combo_missing":
        latest["combos"].pop("independent_1h")
    elif mutation == "target_combo_failed":
        target["status"] = "failed"
    elif mutation == "target_zero_candidates":
        target_cost["total_candidates"] = 0
    elif mutation == "target_negative_edge":
        target_cost["cost_adjusted_edge_mean"] = -0.1
    elif mutation == "target_low_fill":
        target_cost["full_fill_ratio"] = 0.29
    elif mutation == "target_impossible_fill":
        target_cost["full_fill_ratio"] = 1.01
    elif mutation == "target_source_round_missing":
        target.pop("source_step3_round_id")
    elif mutation == "target_source_round_mismatch":
        target["source_step3_round_id"] = "phase2_source_other"
    elif mutation == "target_fingerprint_missing":
        target.pop("parameter_values_fingerprint")
    elif mutation == "target_fingerprint_mismatch":
        target["parameter_values_fingerprint"] = "b" * 64
    elif mutation == "target_resolved_fingerprint_missing":
        target.pop("resolved_parameter_values_fingerprint")
    elif mutation == "target_resolved_fingerprint_mismatch":
        target["resolved_parameter_values_fingerprint"] = "b" * 64
    elif mutation == "target_candidate_sha_missing":
        target.pop("source_step3_candidate_sha256")
    elif mutation == "target_candidate_sha_mismatch":
        target["source_step3_candidate_sha256"] = "d" * 64
    elif mutation == "directory_scan_source":
        phase4["evidence_source"] = "directory_scan"
    elif mutation == "stale_latest_round":
        latest["started_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=169)
        ).isoformat()
    elif mutation == "future_latest_round":
        latest["started_at"] = (
            datetime.now(timezone.utc) + timedelta(hours=1)
        ).isoformat()
    else:  # pragma: no cover - parametrization is exhaustive
        raise AssertionError(mutation)
    ft_decisions = _ft_decisions()
    if mutation == "target_negative_edge":
        ft_decisions[0]["confidence"] = "medium"
    manifest = _manifest(candidates=candidates)
    readiness = _readiness(
        candidates=candidates,
        manifest=manifest,
        evidence=evidence,
        ft_decisions=ft_decisions,
    )
    readiness_blocking_mutations = {
        "latest_round_partial",
        "target_combo_missing",
        "target_combo_failed",
        "target_zero_candidates",
    }
    expected_readiness = (
        "not_ready_execution_issue"
        if mutation in readiness_blocking_mutations
        else "ready_for_next_live_test"
    )
    assert readiness["readiness"] == expected_readiness
    manifest["readiness"] = readiness["readiness"]
    _write_round(
        tmp_path,
        candidates=candidates,
        evidence=evidence,
        manifest=manifest,
        ft_decisions=ft_decisions,
        readiness=readiness,
    )

    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(),
    )

    expected_reason = (
        "promotion_round_not_ready"
        if mutation in readiness_blocking_mutations
        else "promotion_candidate_phase4_evidence_invalid"
    )
    assert verdict.reason_code == expected_reason
    assert verdict.eligible is False


@pytest.mark.parametrize(
    ("candidates", "reason_code"),
    [
        ([], "promotion_candidate_missing"),
        ([_candidate(), _candidate()], "promotion_candidate_ambiguous"),
        ([_candidate(decision="hold")], "promotion_candidate_decision_invalid"),
        (
            [_candidate(symbol="ETH-USDT-SWAP")],
            "promotion_candidate_identity_mismatch",
        ),
        (
            [_candidate(source_round_id="phase2_source_other")],
            "promotion_source_round_mismatch",
        ),
        (
            [_candidate(parameter_values_fingerprint=None)],
            "promotion_candidate_values_fingerprint_invalid",
        ),
        (
            [_candidate(parameter_values_fingerprint="A" * 64)],
            "promotion_candidate_values_fingerprint_invalid",
        ),
        (
            [_candidate(score_ratio=0.1, confidence="low")],
            "promotion_candidate_readiness_mismatch",
        ),
        (
            [_candidate(score_ratio=1.01)],
            "promotion_candidate_readiness_mismatch",
        ),
        (
            [_candidate(confidence="medium")],
            "promotion_candidate_readiness_mismatch",
        ),
    ],
)
def test_exact_promote_candidate_identity_is_required(
    tmp_path: Path,
    candidates: list[dict],
    reason_code: str,
) -> None:
    _write_round(tmp_path, candidates=candidates)

    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(),
    )

    assert verdict.reason_code == reason_code
    assert verdict.eligible is False


def test_nonfinite_evidence_json_fails_closed(tmp_path: Path) -> None:
    round_dir = _write_round(tmp_path)
    (round_dir / "evidence_summary.json").write_text(
        '{"phase2_evidence":{"score":NaN}}',
        encoding="utf-8",
    )

    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(),
    )

    assert verdict.reason_code == "promotion_round_evidence_invalid"


class _FakeSessionContext:
    def __enter__(self):
        return object()

    def __exit__(self, exc_type, exc, traceback):
        return False


def test_reachable_db_missing_exact_round_never_falls_back_to_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_round(tmp_path)
    engine = SimpleNamespace(dispose=Mock())
    monkeypatch.setattr(qualification, "try_governance_db", lambda: (engine, True))
    monkeypatch.setattr(qualification, "Session", lambda _engine: _FakeSessionContext())
    loader = Mock(return_value={})
    monkeypatch.setattr(qualification, "db_load_decision_round_snapshots", loader)

    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(),
    )

    assert verdict.reason_code == "promotion_round_not_found"
    loader.assert_called_once()
    engine.dispose.assert_called_once_with()


def test_batch_uses_one_db_session_and_one_exact_batch_load(
    tmp_path: Path,
    monkeypatch,
) -> None:
    engine = SimpleNamespace(dispose=Mock())
    db_probe = Mock(return_value=(engine, True))
    monkeypatch.setattr(qualification, "try_governance_db", db_probe)
    session_factory = Mock(return_value=_FakeSessionContext())
    monkeypatch.setattr(qualification, "Session", session_factory)
    recommendations = [
        _recommendation(recommendation_id="rec_001"),
        _recommendation(recommendation_id="rec_002"),
    ]
    manifest = _manifest(
        candidates=[_candidate()],
        control_plane_publication=_control_plane_publication(
            *recommendations
        ),
    )
    loader = Mock(return_value={ROUND_ID: _snapshot(manifest=manifest)})
    monkeypatch.setattr(qualification, "db_load_decision_round_snapshots", loader)

    verdicts = qualification.evaluate_promotion_qualifications(
        tmp_path,
        recommendations,
    )

    assert set(verdicts) == {"rec_001", "rec_002"}
    assert all(verdict.eligible for verdict in verdicts.values())
    db_probe.assert_called_once_with()
    session_factory.assert_called_once_with(engine)
    loader.assert_called_once_with(
        loader.call_args.args[0],
        round_ids=[ROUND_ID],
    )
    engine.dispose.assert_called_once_with()


def test_db_snapshot_without_readiness_fails_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    engine = SimpleNamespace(dispose=Mock())
    monkeypatch.setattr(qualification, "try_governance_db", lambda: (engine, True))
    monkeypatch.setattr(qualification, "Session", lambda _engine: _FakeSessionContext())
    snapshot = _snapshot()
    snapshot.pop("promotion_readiness_assessment")
    monkeypatch.setattr(
        qualification,
        "db_load_decision_round_snapshots",
        lambda _session, *, round_ids: {ROUND_ID: snapshot},
    )

    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(),
    )

    assert verdict.reason_code == "promotion_round_readiness_invalid"
    assert verdict.eligible is False
    engine.dispose.assert_called_once_with()


def test_batch_loads_distinct_rounds_once_and_marks_missing_exact_id(
    tmp_path: Path,
    monkeypatch,
) -> None:
    second_round_id = "20260827_130000_cafebabe"
    engine = SimpleNamespace(dispose=Mock())
    monkeypatch.setattr(qualification, "try_governance_db", lambda: (engine, True))
    monkeypatch.setattr(qualification, "Session", lambda _engine: _FakeSessionContext())
    present = _recommendation(recommendation_id="rec_present")
    manifest = _manifest(
        candidates=[_candidate()],
        control_plane_publication=_control_plane_publication(present),
    )
    loader = Mock(return_value={ROUND_ID: _snapshot(manifest=manifest)})
    monkeypatch.setattr(qualification, "db_load_decision_round_snapshots", loader)

    verdicts = qualification.evaluate_promotion_qualifications(
        tmp_path,
        [
            present,
            _recommendation(
                recommendation_id="rec_missing",
                evidence_bundle_ref=second_round_id,
            ),
        ],
    )

    assert verdicts["rec_present"].reason_code == "qualified"
    assert verdicts["rec_missing"].reason_code == "promotion_round_not_found"
    loader.assert_called_once_with(
        loader.call_args.args[0],
        round_ids=[ROUND_ID, second_round_id],
    )


def test_db_exception_fails_closed_without_file_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_round(tmp_path)
    engine = SimpleNamespace(dispose=Mock())
    monkeypatch.setattr(qualification, "try_governance_db", lambda: (engine, True))
    monkeypatch.setattr(qualification, "Session", lambda _engine: _FakeSessionContext())
    monkeypatch.setattr(
        qualification,
        "db_load_decision_round_snapshots",
        Mock(side_effect=RuntimeError("database failure")),
    )

    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(),
    )

    assert verdict.reason_code == "promotion_round_db_error"
    assert verdict.eligible is False


def test_db_probe_exception_fails_closed_without_file_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_round(tmp_path)
    monkeypatch.setattr(
        qualification,
        "try_governance_db",
        Mock(side_effect=RuntimeError("database probe failure")),
    )

    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(),
    )

    assert verdict.reason_code == "promotion_round_db_error"
    assert verdict.eligible is False


def test_configured_but_unreachable_db_never_falls_back_to_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_round(tmp_path)
    monkeypatch.setattr(
        qualification,
        "has_explicit_governance_db_configuration",
        lambda _root: True,
    )
    monkeypatch.setattr(
        qualification,
        "try_governance_db",
        lambda: (None, False),
    )

    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(),
    )

    assert verdict.reason_code == "promotion_round_db_error"
    assert verdict.eligible is False


def test_unconfigured_default_local_db_allows_offline_file_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_round(tmp_path)
    monkeypatch.delenv("AATS_ACTIVE_PARAMETER_DB_URL", raising=False)
    monkeypatch.delenv("RDP_DATABASE_URL", raising=False)
    monkeypatch.setattr(
        qualification,
        "has_explicit_governance_db_configuration",
        lambda _root: False,
    )
    monkeypatch.setattr(
        qualification,
        "try_governance_db",
        lambda: (None, False),
    )

    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(),
    )

    assert verdict.reason_code == "qualified"
    assert verdict.eligible is True


class _ReaderSession:
    def __init__(self, row):
        self.row = row
        self.statement = ""
        self.params = None

    def execute(self, statement, params):
        self.statement = str(statement)
        self.params = params
        return SimpleNamespace(fetchone=lambda: self.row)


class _BatchReaderSession:
    def __init__(self, rows):
        self.rows = rows
        self.execute_count = 0
        self.statement = ""
        self.params = None

    def execute(self, statement, params):
        self.execute_count += 1
        self.statement = str(statement)
        self.params = params
        return SimpleNamespace(fetchall=lambda: self.rows)


def test_exact_id_db_reader_uses_bound_round_id_and_normalizes_snapshot() -> None:
    now = datetime(
        2026,
        8,
        27,
        8,
        tzinfo=timezone(-timedelta(hours=4)),
    )
    row = SimpleNamespace(
        round_id=ROUND_ID,
        started_at=now,
        finished_at=now,
        evidence_summary_json=json.dumps(_evidence()),
        parameter_upgrade_candidates_json=json.dumps([_candidate()]),
        family_timeframe_decisions_json="[]",
        promotion_readiness_json="{}",
        manifest_json=json.dumps(_manifest(candidates=[_candidate()])),
        conclusion_markdown="done",
    )
    session = _ReaderSession(row)

    snapshot = db_load_decision_round_snapshot(session, round_id=ROUND_ID)

    assert snapshot is not None
    assert snapshot["round_id"] == ROUND_ID
    assert snapshot["started_at"] == "2026-08-27T12:00:00+00:00"
    assert snapshot["finished_at"] == "2026-08-27T12:00:00+00:00"
    assert snapshot["manifest"]["phase"] == "phase6"
    assert "WHERE round_id = :round_id" in session.statement
    assert session.params == {"round_id": ROUND_ID}


def test_exact_id_db_reader_returns_none_without_substitution() -> None:
    session = _ReaderSession(None)

    assert db_load_decision_round_snapshot(session, round_id=ROUND_ID) is None
    assert session.params == {"round_id": ROUND_ID}


def test_exact_id_db_batch_reader_uses_one_bound_query() -> None:
    second_round_id = "20260827_130000_cafebabe"
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)

    def _row(round_id: str):
        manifest = _manifest(candidates=[_candidate()], round_id=round_id)
        return SimpleNamespace(
            round_id=round_id,
            started_at=now,
            finished_at=now,
            evidence_summary_json=json.dumps(_evidence()),
            parameter_upgrade_candidates_json=json.dumps([_candidate()]),
            family_timeframe_decisions_json="[]",
            promotion_readiness_json="{}",
            manifest_json=json.dumps(manifest),
            conclusion_markdown="done",
        )

    session = _BatchReaderSession([_row(ROUND_ID), _row(second_round_id)])

    snapshots = db_load_decision_round_snapshots(
        session,
        round_ids=[second_round_id, ROUND_ID, ROUND_ID],
    )

    assert set(snapshots) == {ROUND_ID, second_round_id}
    assert session.execute_count == 1
    assert "WHERE round_id IN" in session.statement
    assert session.params == {"round_ids": (ROUND_ID, second_round_id)}


def test_duplicate_batch_recommendation_id_is_ineligible(
    tmp_path: Path,
) -> None:
    _write_round(tmp_path)
    recommendations = [_recommendation(), deepcopy(_recommendation())]

    verdicts = qualification.evaluate_promotion_qualifications(
        tmp_path,
        recommendations,
    )

    assert verdicts["rec_001"].reason_code == "recommendation_id_duplicate"
    assert verdicts["rec_001"].eligible is False
