import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aats.data_platform.research_factory.integrity import (
    EvidenceReferenceIntegrityReport,
    validate_preapply_package_reference_integrity,
)
from aats.data_platform.research_factory.preapply import PreApplyEvidencePackage

UTC = timezone.utc


def dt(day: int) -> datetime:
    return datetime(2026, 1, day, tzinfo=UTC)


def artifact_root(tmp_path: Path) -> Path:
    return tmp_path / "artifacts" / "research" / "research_factory" / "preapply" / "preapply_obs_1"


def preapply_package() -> PreApplyEvidencePackage:
    return PreApplyEvidencePackage(
        package_id="preapply_obs_1",
        candidate_id="cand_integrity_1",
        recommendation_id="rec_cand_integrity_1",
        observation_id="obs_cand_integrity_1",
        experiment_id="exp_integrity_1",
        status="preapply_ready",
        evidence_refs={
            "candidate_artifact": "candidate_artifact.json",
            "research_recommendation": "research_recommendation.json",
            "metrics_snapshot": "metrics_snapshot.json",
            "dataset_quality_report": "dataset_quality_report.json",
            "source_integrity_report": "source_integrity_report.json",
            "execution_evidence_report": "execution_evidence_report.json",
            "evidence_bundle": "evidence_bundle.json",
            "observation_result": "observation_result.json",
            "review_outcome": "review_outcome.json",
            "rollback_plan": "research_recommendation.json",
        },
        gate_refs={
            "candidate_gate": "candidate_artifact.json",
            "observation_gate_result": "observation_gate_result.json",
        },
        review_decision="eligible_for_preapply",
        candidate_gate_passed=True,
        evidence_bundle_passed=True,
        observation_gate_passed=True,
        created_at=dt(13),
    )


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def seed_integrity_artifacts(root: Path) -> None:
    package = preapply_package()
    common = {
        "candidate_id": package.candidate_id,
        "experiment_id": package.experiment_id,
    }
    observation = {
        "observation_id": package.observation_id,
        "recommendation_id": package.recommendation_id,
        **common,
    }
    payloads = {
        "candidate_artifact.json": {
            **common,
            "gate": {"passed": True},
        },
        "research_recommendation.json": {
            "recommendation_id": package.recommendation_id,
            **common,
        },
        "metrics_snapshot.json": {"net_annualized_return": 0.03},
        "dataset_quality_report.json": {"passed": True},
        "source_integrity_report.json": {"passed": True},
        "execution_evidence_report.json": {"passed": True},
        "evidence_bundle.json": {"passed": True},
        "observation_result.json": observation,
        "observation_gate_result.json": {**observation, "passed": True},
        "review_outcome.json": {
            **observation,
            "decision": package.review_decision,
        },
    }
    for name, payload in payloads.items():
        write_json(root / name, payload)


def test_validate_preapply_package_reference_integrity_passes(tmp_path: Path) -> None:
    root = artifact_root(tmp_path)
    seed_integrity_artifacts(root)
    package = preapply_package()

    report = validate_preapply_package_reference_integrity(
        package,
        root,
        created_at=dt(14),
    )

    assert report.passed is True
    assert report.failures == ()
    assert report.subject_id == package.package_id
    assert report.subject_type == "preapply_evidence_package"
    assert report.checked_refs["evidence_refs.candidate_artifact"] == "candidate_artifact.json"
    assert report.checked_refs["gate_refs.observation_gate_result"] == "observation_gate_result.json"


def test_validate_preapply_package_reference_integrity_reports_missing_refs(tmp_path: Path) -> None:
    root = artifact_root(tmp_path)
    seed_integrity_artifacts(root)
    (root / "observation_result.json").unlink()

    report = validate_preapply_package_reference_integrity(preapply_package(), root, created_at=dt(14))

    assert report.passed is False
    assert report.missing_refs == ("evidence_refs.observation_result: missing artifact observation_result.json",)
    assert report.failures == report.missing_refs


def test_validate_preapply_package_reference_integrity_reports_identity_mismatch(tmp_path: Path) -> None:
    root = artifact_root(tmp_path)
    seed_integrity_artifacts(root)
    payload = json.loads((root / "review_outcome.json").read_text(encoding="utf-8"))
    payload["candidate_id"] = "cand_other"
    write_json(root / "review_outcome.json", payload)

    report = validate_preapply_package_reference_integrity(preapply_package(), root, created_at=dt(14))

    assert report.passed is False
    assert report.identity_mismatches == (
        "evidence_refs.review_outcome: candidate_id='cand_other' does not match expected 'cand_integrity_1'",
    )
    assert report.failures == report.identity_mismatches


def test_validate_preapply_package_reference_integrity_rejects_non_research_root(tmp_path: Path) -> None:
    root = tmp_path / "configs"
    root.mkdir()

    with pytest.raises(ValueError, match="under artifacts/research"):
        validate_preapply_package_reference_integrity(preapply_package(), root)


def test_integrity_report_rejects_passing_report_with_failures() -> None:
    with pytest.raises(ValueError, match="must not contain failures"):
        EvidenceReferenceIntegrityReport(
            subject_id="preapply_bad",
            subject_type="preapply_evidence_package",
            artifact_root="artifacts/research/preapply_bad",
            checked_refs={},
            passed=True,
            failures=("missing ref",),
            created_at=dt(14),
        )
