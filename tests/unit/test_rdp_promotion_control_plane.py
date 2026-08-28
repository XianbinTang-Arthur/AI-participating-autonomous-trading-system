from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aats.api.rdp_routes import rdp_router
from aats.data_platform.decision_system.active_parameter_apply import (
    apply_approved_recommendation,
)
from aats.data_platform.decision_system.promotion_guard import (
    PromotionQualificationBlockedError,
    issue_promotion_authorization,
    promotion_qualification_failure,
    require_promotion_authorization,
    validate_promotion_qualification_verdict,
)
from aats.data_platform.decision_system.promotion_qualification import (
    PromotionQualificationVerdict,
)
from aats.data_platform.decision_system.recommendation_registry import (
    approve_recommendation,
    reject_recommendation,
)
from aats.data_platform.production_workflow.gate_rules import (
    DEFAULT_GATE_RULES,
    check_promotion_qualification,
)
from aats.data_platform.production_workflow.pre_apply_gate import build_gate_context
from aats.data_platform.production_workflow.release_registry import (
    create_parameter_release,
)


@pytest.fixture(autouse=True)
def _isolate_managed_profile_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "AATS_PROFILE",
        "AATS_ENV_TEMPLATE_PROFILE",
        "AATS_STARTUP_PROFILE",
    ):
        monkeypatch.delenv(name, raising=False)


@dataclass(frozen=True)
class _Verdict:
    required: bool
    eligible: bool
    reason_code: str
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {
            "required": self.required,
            "eligible": self.eligible,
            "reason_code": self.reason_code,
            "detail": self.detail,
        }


def _blocked_payload(rec_id: str) -> dict[str, object]:
    return PromotionQualificationBlockedError(
        rec_id,
        _Verdict(True, False, "legacy_policy", "证据策略版本过旧"),
    ).to_dict()


def _apply_capable_rec(rec_id: str, status: str) -> dict[str, object]:
    return {
        "recommendation_id": rec_id,
        "recommendation_type": "parameter_upgrade",
        "target_parameter_set_id": "ps_1",
        "family": "independent",
        "timeframe": "15m",
        "symbol": "BTC-USDT-SWAP",
        "status": status,
    }


def test_approve_blocks_before_memory_or_db_status_write(tmp_path: Path) -> None:
    rec = _apply_capable_rec("rec_approve_block", "draft")
    db_update = MagicMock()
    with (
        patch(
            "aats.data_platform.decision_system.promotion_guard.require_promotion_qualification",
            side_effect=PromotionQualificationBlockedError(
                "rec_approve_block",
                _Verdict(True, False, "legacy_policy", "legacy"),
            ),
        ),
        patch(
            "aats.data_platform.decision_system.recommendation_registry._db_update_rec_status",
            db_update,
        ),
        pytest.raises(PromotionQualificationBlockedError) as exc_info,
    ):
        approve_recommendation(
            {"recommendations": [rec]},
            "rec_approve_block",
            project_root=tmp_path,
        )

    assert exc_info.value.to_dict()["promotion_qualification"]["reason_code"] == (
        "legacy_policy"
    )
    assert rec["status"] == "draft"
    assert "approved_at" not in rec
    db_update.assert_not_called()


def test_release_skip_gate_blocks_before_parameter_reads(tmp_path: Path) -> None:
    rec = _apply_capable_rec("rec_release_block", "approved")
    parameter_read = MagicMock()
    with (
        patch.dict("os.environ", {"RDP_ENV": "dev"}, clear=False),
        patch(
            "aats.data_platform.decision_system.recommendation_registry.load_recommendation_registry",
            return_value={"recommendations": [rec]},
        ),
        patch(
            "aats.data_platform.decision_system.recommendation_registry.find_recommendation",
            return_value=rec,
        ),
        patch(
            "aats.data_platform.decision_system.promotion_guard.promotion_qualification_failure",
            return_value=_blocked_payload("rec_release_block"),
        ),
        patch(
            "aats.data_platform.governance.parameter_registry.load_registry",
            parameter_read,
        ),
    ):
        result = create_parameter_release(
            tmp_path,
            recommendation_id="rec_release_block",
            run_gate=False,
            run_apply=False,
        )

    assert result["code"] == "promotion_qualification_blocked"
    assert result["promotion_qualification"]["reason_code"] == "legacy_policy"
    parameter_read.assert_not_called()


@pytest.mark.parametrize(
    ("operation", "dry_run"),
    [("release", False), ("apply", False), ("apply", True)],
)
def test_forward_paths_fail_closed_on_malformed_verdict(
    tmp_path: Path,
    operation: str,
    dry_run: bool,
) -> None:
    rec = _apply_capable_rec("rec_malformed_verdict", "approved")
    malformed = SimpleNamespace(
        required=False,
        eligible=True,
        reason_code="not_required",
        detail="malformed semantic bypass",
        to_dict=lambda: {
            "required": False,
            "eligible": True,
            "reason_code": "not_required",
            "detail": "malformed semantic bypass",
        },
    )
    parameter_read = MagicMock()
    with (
        patch.dict("os.environ", {"RDP_ENV": "dev"}, clear=False),
        patch(
            "aats.data_platform.decision_system.recommendation_registry.load_recommendation_registry",
            return_value={"recommendations": [rec]},
        ),
        patch(
            "aats.data_platform.decision_system.recommendation_registry.find_recommendation",
            return_value=rec,
        ),
        patch(
            "aats.data_platform.decision_system.promotion_qualification.evaluate_promotion_qualification",
            return_value=malformed,
        ),
        patch(
            "aats.data_platform.governance.parameter_registry.load_registry",
            parameter_read,
        ),
    ):
        if operation == "release":
            result = create_parameter_release(
                tmp_path,
                recommendation_id="rec_malformed_verdict",
                run_gate=False,
                run_apply=False,
            )
        else:
            result = apply_approved_recommendation(
                tmp_path,
                recommendation_id="rec_malformed_verdict",
                dry_run=dry_run,
            )

    assert result["code"] == "promotion_qualification_blocked"
    assert result["promotion_qualification"]["reason_code"] == (
        "promotion_qualification_invalid"
    )
    parameter_read.assert_not_called()


def test_approval_fails_closed_when_apply_verdict_claims_not_required(
    tmp_path: Path,
) -> None:
    rec = _apply_capable_rec("rec_approval_not_required", "draft")
    malformed = SimpleNamespace(
        required=False,
        eligible=True,
        reason_code="not_required",
        detail="malformed semantic bypass",
        to_dict=lambda: {
            "required": False,
            "eligible": True,
            "reason_code": "not_required",
            "detail": "malformed semantic bypass",
        },
    )
    db_update = MagicMock()
    with (
        patch(
            "aats.data_platform.decision_system.promotion_qualification.evaluate_promotion_qualification",
            return_value=malformed,
        ),
        patch(
            "aats.data_platform.decision_system.recommendation_registry._db_update_rec_status",
            db_update,
        ),
        pytest.raises(PromotionQualificationBlockedError) as exc_info,
    ):
        approve_recommendation(
            {"recommendations": [rec]},
            "rec_approval_not_required",
            project_root=tmp_path,
        )

    assert exc_info.value.to_dict()["promotion_qualification"]["reason_code"] == (
        "promotion_qualification_invalid"
    )
    assert rec["status"] == "draft"
    db_update.assert_not_called()


def test_guard_fails_closed_on_evaluator_exception(tmp_path: Path) -> None:
    rec = _apply_capable_rec("rec_evaluator_error", "approved")
    with patch(
        "aats.data_platform.decision_system.promotion_qualification.evaluate_promotion_qualification",
        side_effect=RuntimeError("synthetic evaluator failure"),
    ):
        failure = promotion_qualification_failure(tmp_path, rec)

    assert failure is not None
    assert failure["promotion_qualification"]["reason_code"] == (
        "promotion_qualification_invalid"
    )


@pytest.mark.parametrize("recommendation_type", ["keep_active", "pause", None])
def test_release_rejects_non_apply_type_even_when_target_parameter_is_present(
    tmp_path: Path,
    recommendation_type: str | None,
) -> None:
    rec = _apply_capable_rec("rec_release_wrong_type", "approved")
    rec["recommendation_type"] = recommendation_type
    parameter_read = MagicMock()
    with (
        patch.dict("os.environ", {"RDP_ENV": "dev"}, clear=False),
        patch(
            "aats.data_platform.decision_system.recommendation_registry.load_recommendation_registry",
            return_value={"recommendations": [rec]},
        ),
        patch(
            "aats.data_platform.decision_system.recommendation_registry.find_recommendation",
            return_value=rec,
        ),
        patch(
            "aats.data_platform.governance.parameter_registry.load_registry",
            parameter_read,
        ),
    ):
        result = create_parameter_release(
            tmp_path,
            recommendation_id="rec_release_wrong_type",
            run_gate=False,
            run_apply=False,
        )

    assert result["code"] == "promotion_qualification_blocked"
    assert result["promotion_qualification"]["reason_code"] == (
        "recommendation_type_not_apply_capable"
    )
    parameter_read.assert_not_called()


@pytest.mark.parametrize("dry_run", [False, True])
def test_apply_blocks_direct_and_dry_run_before_parameter_reads(
    tmp_path: Path,
    dry_run: bool,
) -> None:
    rec = _apply_capable_rec("rec_apply_block", "approved")
    parameter_read = MagicMock()
    with (
        patch.dict("os.environ", {"RDP_ENV": "dev"}, clear=False),
        patch(
            "aats.data_platform.decision_system.recommendation_registry.load_recommendation_registry",
            return_value={"recommendations": [rec]},
        ),
        patch(
            "aats.data_platform.decision_system.recommendation_registry.find_recommendation",
            return_value=rec,
        ),
        patch(
            "aats.data_platform.decision_system.promotion_guard.promotion_qualification_failure",
            return_value=_blocked_payload("rec_apply_block"),
        ),
        patch(
            "aats.data_platform.governance.parameter_registry.load_registry",
            parameter_read,
        ),
    ):
        result = apply_approved_recommendation(
            tmp_path,
            recommendation_id="rec_apply_block",
            dry_run=dry_run,
        )

    assert result["code"] == "promotion_qualification_blocked"
    assert result["environment"] == "dev"
    parameter_read.assert_not_called()


@pytest.mark.parametrize("recommendation_type", ["keep_active", "pause", None])
@pytest.mark.parametrize("dry_run", [False, True])
def test_apply_rejects_non_apply_type_even_when_target_parameter_is_present(
    tmp_path: Path,
    recommendation_type: str | None,
    dry_run: bool,
) -> None:
    rec = _apply_capable_rec("rec_apply_wrong_type", "approved")
    rec["recommendation_type"] = recommendation_type
    parameter_read = MagicMock()
    with (
        patch.dict("os.environ", {"RDP_ENV": "dev"}, clear=False),
        patch(
            "aats.data_platform.decision_system.recommendation_registry.load_recommendation_registry",
            return_value={"recommendations": [rec]},
        ),
        patch(
            "aats.data_platform.decision_system.recommendation_registry.find_recommendation",
            return_value=rec,
        ),
        patch(
            "aats.data_platform.governance.parameter_registry.load_registry",
            parameter_read,
        ),
    ):
        result = apply_approved_recommendation(
            tmp_path,
            recommendation_id="rec_apply_wrong_type",
            dry_run=dry_run,
        )

    assert result["code"] == "promotion_qualification_blocked"
    assert result["promotion_qualification"]["reason_code"] == (
        "recommendation_type_not_apply_capable"
    )
    parameter_read.assert_not_called()


def test_gate_rule_is_default_block_with_stable_reason() -> None:
    result = check_promotion_qualification(
        {
            "recommendation": _apply_capable_rec("rec_gate_block", "approved"),
            "promotion_qualification": _Verdict(
                True,
                False,
                "evidence_ref_missing",
                "缺少精确证据引用",
            )
        }
    )

    assert result.name == "promotion_qualification"
    assert result.passed is False
    assert result.severity == "block"
    assert result.reason_code == "evidence_ref_missing"
    default_names = [rule.__name__ for rule in DEFAULT_GATE_RULES]
    assert "check_promotion_qualification" in default_names
    assert "check_evidence_completeness" not in default_names
    assert "check_latest_round_health" not in default_names

    malformed = check_promotion_qualification(
        {
            "recommendation": _apply_capable_rec("rec_gate_malformed", "approved"),
            "promotion_qualification": SimpleNamespace(required="false"),
        }
    )
    assert malformed.passed is False
    assert malformed.severity == "block"
    assert malformed.reason_code == "promotion_qualification_invalid"


@pytest.mark.parametrize("required", [False, 0, "false", None])
def test_gate_rejects_parameter_upgrade_required_contract_drift(
    required: object,
) -> None:
    verdict = SimpleNamespace(
        required=required,
        eligible=False,
        reason_code="not_required",
        detail="malformed qualification",
        to_dict=lambda: {
            "required": required,
            "eligible": False,
            "reason_code": "not_required",
            "detail": "malformed qualification",
        },
    )

    result = check_promotion_qualification({
        "recommendation": _apply_capable_rec("rec_gate_drift", "approved"),
        "promotion_qualification": verdict,
    })

    assert result.passed is False
    assert result.severity == "block"
    assert result.reason_code == "promotion_qualification_invalid"


def test_process_authorization_binds_root_and_complete_recommendation_identity(
    tmp_path: Path,
) -> None:
    round_id = "20260827_120000_deadbeef"
    rec = {
        **_apply_capable_rec("rec_auth_identity", "draft"),
        "source_round_id": "research_round_1",
        "evidence_bundle_ref": round_id,
        "confidence": "high",
        "reason": "immutable qualified rationale",
    }
    verdict = PromotionQualificationVerdict(
        required=True,
        eligible=True,
        reason_code="qualified",
        evidence_bundle_ref=round_id,
        source_round_id="research_round_1",
        qualified_round_id=round_id,
        detail="qualified",
        qualified_finished_at=datetime.now(timezone.utc).isoformat(),
        parameter_values_fingerprint="a" * 64,
    )

    with patch(
        "aats.data_platform.decision_system.promotion_qualification.evaluate_promotion_qualification",
        return_value=verdict,
    ):
        authorization = issue_promotion_authorization(tmp_path, rec)

    assert require_promotion_authorization(tmp_path, rec, authorization) is verdict
    drifted = dict(rec)
    drifted["target_parameter_set_id"] = "ps_drifted"
    with pytest.raises(PromotionQualificationBlockedError) as exc_info:
        require_promotion_authorization(tmp_path, drifted, authorization)
    assert exc_info.value.verdict.reason_code == "promotion_authorization_invalid"
    for field_name in ("confidence", "reason"):
        drifted = dict(rec)
        drifted[field_name] = "drifted"
        with pytest.raises(PromotionQualificationBlockedError) as exc_info:
            require_promotion_authorization(tmp_path, drifted, authorization)
        assert (
            exc_info.value.verdict.reason_code
            == "promotion_authorization_invalid"
        )


def _qualified_authorization_fixture(
    *,
    now: datetime,
    finished_at: datetime,
) -> tuple[dict[str, object], PromotionQualificationVerdict]:
    round_id = "20260827_120000_deadbeef"
    rec = {
        **_apply_capable_rec("rec_auth_expiry", "approved"),
        "source_round_id": "research_round_1",
        "evidence_bundle_ref": round_id,
    }
    verdict = PromotionQualificationVerdict(
        required=True,
        eligible=True,
        reason_code="qualified",
        evidence_bundle_ref=round_id,
        source_round_id="research_round_1",
        qualified_round_id=round_id,
        detail="qualified",
        qualified_finished_at=finished_at.isoformat(),
        parameter_values_fingerprint="a" * 64,
    )
    return rec, verdict


def test_process_authorization_expires_after_short_composite_ttl(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 27, 12, tzinfo=timezone.utc)
    rec, verdict = _qualified_authorization_fixture(
        now=now,
        finished_at=now - timedelta(hours=1),
    )
    with (
        patch(
            "aats.data_platform.decision_system.promotion_qualification.evaluate_promotion_qualification",
            return_value=verdict,
        ),
        patch(
            "aats.data_platform.decision_system.promotion_guard._utc_now",
            return_value=now,
        ),
    ):
        authorization = issue_promotion_authorization(tmp_path, rec)

    with patch(
        "aats.data_platform.decision_system.promotion_guard._utc_now",
        return_value=now + timedelta(minutes=4),
    ):
        assert require_promotion_authorization(tmp_path, rec, authorization) is verdict
    with (
        patch(
            "aats.data_platform.decision_system.promotion_guard._utc_now",
            return_value=now + timedelta(minutes=5),
        ),
        pytest.raises(PromotionQualificationBlockedError),
    ):
        require_promotion_authorization(tmp_path, rec, authorization)


def test_process_authorization_never_outlives_qualified_evidence(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 27, 12, tzinfo=timezone.utc)
    rec, verdict = _qualified_authorization_fixture(
        now=now,
        finished_at=now - timedelta(hours=167, minutes=59),
    )
    with (
        patch(
            "aats.data_platform.decision_system.promotion_qualification.evaluate_promotion_qualification",
            return_value=verdict,
        ),
        patch(
            "aats.data_platform.decision_system.promotion_guard._utc_now",
            return_value=now,
        ),
    ):
        authorization = issue_promotion_authorization(tmp_path, rec)

    assert authorization.expires_at_utc == now + timedelta(minutes=1)
    with (
        patch(
            "aats.data_platform.decision_system.promotion_guard._utc_now",
            return_value=now + timedelta(minutes=1),
        ),
        pytest.raises(PromotionQualificationBlockedError),
    ):
        require_promotion_authorization(tmp_path, rec, authorization)


def test_process_authorization_rejects_naive_or_future_issue_time(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 27, 12, tzinfo=timezone.utc)
    rec, verdict = _qualified_authorization_fixture(
        now=now,
        finished_at=now - timedelta(hours=1),
    )
    with (
        patch(
            "aats.data_platform.decision_system.promotion_qualification.evaluate_promotion_qualification",
            return_value=verdict,
        ),
        patch(
            "aats.data_platform.decision_system.promotion_guard._utc_now",
            return_value=now,
        ),
    ):
        authorization = issue_promotion_authorization(tmp_path, rec)

    invalid_authorizations = (
        replace(authorization, issued_at_utc=now.replace(tzinfo=None)),
        replace(
            authorization,
            issued_at_utc=now + timedelta(minutes=1),
            expires_at_utc=now + timedelta(minutes=2),
        ),
    )
    with patch(
        "aats.data_platform.decision_system.promotion_guard._utc_now",
        return_value=now,
    ):
        for invalid in invalid_authorizations:
            with pytest.raises(PromotionQualificationBlockedError):
                require_promotion_authorization(tmp_path, rec, invalid)


def test_expired_process_authorization_blocks_release_before_parameter_reads(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 27, 12, tzinfo=timezone.utc)
    rec, verdict = _qualified_authorization_fixture(
        now=now,
        finished_at=now - timedelta(hours=1),
    )
    with (
        patch(
            "aats.data_platform.decision_system.promotion_qualification.evaluate_promotion_qualification",
            return_value=verdict,
        ),
        patch(
            "aats.data_platform.decision_system.promotion_guard._utc_now",
            return_value=now,
        ),
    ):
        authorization = issue_promotion_authorization(tmp_path, rec)

    parameter_read = MagicMock()
    with (
        patch.dict("os.environ", {"RDP_ENV": "dev"}, clear=False),
        patch(
            "aats.data_platform.decision_system.recommendation_registry.load_recommendation_registry",
            return_value={"recommendations": [rec]},
        ),
        patch(
            "aats.data_platform.decision_system.recommendation_registry.find_recommendation",
            return_value=rec,
        ),
        patch(
            "aats.data_platform.governance.parameter_registry.load_registry",
            parameter_read,
        ),
        patch(
            "aats.data_platform.decision_system.promotion_guard._utc_now",
            return_value=now + timedelta(minutes=5),
        ),
    ):
        result = create_parameter_release(
            tmp_path,
            recommendation_id=str(rec["recommendation_id"]),
            run_gate=False,
            run_apply=False,
            promotion_authorization=authorization,
        )

    assert result["code"] == "promotion_qualification_blocked"
    assert result["promotion_qualification"]["reason_code"] == (
        "promotion_authorization_invalid"
    )
    parameter_read.assert_not_called()


@pytest.mark.parametrize(
    ("reason_code", "evidence_ref", "source_round_id", "qualified_round_id"),
    [
        ("legacy_allow", "round_1", "source_1", "round_1"),
        ("qualified", "evil_round", "source_1", "evil_round"),
        ("qualified", "round_1", "evil_source", "round_1"),
        ("qualified", "round_1", "source_1", "evil_round"),
    ],
)
def test_eligible_verdict_must_bind_exact_qualified_evidence(
    reason_code: str,
    evidence_ref: str,
    source_round_id: str,
    qualified_round_id: str,
) -> None:
    rec = {
        **_apply_capable_rec("rec_exact_verdict", "approved"),
        "evidence_bundle_ref": "round_1",
        "source_round_id": "source_1",
    }
    verdict = PromotionQualificationVerdict(
        required=True,
        eligible=True,
        reason_code=reason_code,
        evidence_bundle_ref=evidence_ref,
        source_round_id=source_round_id,
        qualified_round_id=qualified_round_id,
        detail="synthetic verdict",
        qualified_finished_at=datetime.now(timezone.utc).isoformat(),
        parameter_values_fingerprint="a" * 64,
    )

    guarded = validate_promotion_qualification_verdict(verdict, rec)

    assert guarded.eligible is False
    assert guarded.reason_code == "promotion_qualification_invalid"


def test_gate_context_binds_exact_verifier_result(tmp_path: Path) -> None:
    rec = _apply_capable_rec("rec_exact_context", "approved")
    verdict = _Verdict(True, False, "promotion_policy_unsupported", "legacy")
    evaluator = MagicMock(return_value=verdict)
    with (
        patch(
            "aats.data_platform.decision_system.recommendation_registry.load_recommendation_registry",
            return_value={"recommendations": [rec]},
        ),
        patch(
            "aats.data_platform.decision_system.recommendation_registry.find_recommendation",
            return_value=rec,
        ),
        patch(
            "aats.data_platform.decision_system.recommendation_registry.load_active_decision_registry",
            return_value={"decisions": []},
        ),
        patch(
            "aats.data_platform.production_workflow.pre_apply_gate.load_governance_snapshot",
            return_value=None,
        ),
        patch(
            "aats.data_platform.production_workflow.pre_apply_gate.try_governance_db",
            return_value=(None, False),
        ),
        patch(
            "aats.data_platform.governance.parameter_registry.load_registry",
            return_value={"parameter_sets": []},
        ),
        patch(
            "aats.data_platform.production_workflow.pre_apply_gate.build_gate_runtime_contract",
            return_value={
                "current_alerts": None,
                "latest_workflow_runs": {},
                "live_db_health": {},
            },
        ),
        patch(
            "aats.data_platform.decision_system.promotion_qualification.evaluate_promotion_qualification",
            evaluator,
        ),
    ):
        ctx = build_gate_context(tmp_path, "rec_exact_context")

    assert ctx["promotion_qualification"] is verdict
    evaluator.assert_called_once_with(project_root=tmp_path, recommendation=rec)


def test_non_apply_approval_and_reject_remain_available(tmp_path: Path) -> None:
    approve_rec = {
        "recommendation_id": "rec_pause",
        "recommendation_type": "pause",
        "status": "draft",
    }
    reject_rec = _apply_capable_rec("rec_reject_legacy", "draft")
    with patch(
        "aats.data_platform.decision_system.recommendation_registry._db_update_rec_status",
        return_value=True,
    ):
        approved = approve_recommendation(
            {"recommendations": [approve_rec]},
            "rec_pause",
            project_root=tmp_path,
        )
        rejected = reject_recommendation(
            {"recommendations": [reject_rec]},
            "rec_reject_legacy",
        )

    assert approved is approve_rec
    assert approve_rec["status"] == "approved"
    assert rejected is reject_rec
    assert reject_rec["status"] == "rejected"


def test_registry_db_status_write_binds_exact_recommendation_identity() -> None:
    from aats.data_platform.decision_system import recommendation_registry

    rec = {
        **_apply_capable_rec("rec_identity_cas", "approved"),
        "source_round_id": "20260827_120000_aaaaaaaa",
        "evidence_bundle_ref": "20260827_120000_aaaaaaaa",
        "approved_by": "operator",
    }
    db_update = MagicMock(return_value=True)
    engine = MagicMock()

    class _Session:
        def __init__(self, _engine: object) -> None:
            pass

        def __enter__(self) -> _Session:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def begin(self) -> nullcontext[None]:
            return nullcontext()

    with (
        patch.object(
            recommendation_registry,
            "try_governance_db",
            return_value=(engine, True),
        ),
        patch("sqlalchemy.orm.Session", _Session),
        patch(
            "aats.data_platform.governance.recommendations_db.db_update_recommendation_status",
            db_update,
        ),
    ):
        assert recommendation_registry._db_update_rec_status(
            rec,
            expected_current_status="draft",
        )

    expected_identity = db_update.call_args.kwargs["expected_identity"]
    assert expected_identity == {
        "family": "independent",
        "symbol": "BTC-USDT-SWAP",
        "timeframe": "15m",
        "recommendation_type": "parameter_upgrade",
            "target_parameter_set_id": "ps_1",
            "source_round_id": "20260827_120000_aaaaaaaa",
            "confidence": None,
            "reason": None,
            "evidence_bundle_ref": "20260827_120000_aaaaaaaa",
        }


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(rdp_router)
    app.state.runtime = SimpleNamespace(
        settings=SimpleNamespace(
            operator_auth_enabled=False,
            operator_control_plane_execution_ledger_enabled=False,
            operator_unsafe_write_without_auth=True,
            storage_mode="memory",
        ),
        environment_capabilities=SimpleNamespace(local_only=True),
    )
    return app


@pytest.mark.parametrize(
    "path",
    [
        "/rdp/recommendations/rec_route_block/approve",
        "/rdp/recommendations/rec_route_block/approve-and-release",
    ],
)
def test_approval_routes_map_qualification_failure_to_structured_409(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    rec = _apply_capable_rec("rec_route_block", "draft")
    reg_path = tmp_path / "artifacts/decision_system/recommendation_registry.json"
    reg_path.parent.mkdir(parents=True)
    reg_path.write_text(
        json.dumps({"recommendations": [rec]}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setenv("RDP_APPLY_TOKEN_SECRET", "promotion-control-test-secret")

    with (
        patch("aats.api.rdp_routes._project_root", return_value=tmp_path),
        patch("aats.api.rdp_routes._step2_integrity_blocking_reason", return_value=None),
        patch(
            "aats.data_platform.decision_system.recommendation_registry.try_governance_db",
            return_value=(None, False),
        ),
        patch(
            "aats.data_platform.decision_system.recommendation_registry.has_explicit_governance_db_configuration",
            return_value=False,
        ),
        patch(
            "aats.data_platform.decision_system.promotion_guard.require_promotion_qualification",
            side_effect=PromotionQualificationBlockedError(
                "rec_route_block",
                _Verdict(True, False, "legacy_policy", "legacy"),
            ),
        ),
    ):
        headers: dict[str, str] = {}
        if path.endswith("approve-and-release"):
            from aats.api.rdp_apply_token import emit_token

            headers["X-Rdp-Apply-Token"] = emit_token(
                actor="operator", action="apply"
            )
        response = TestClient(_build_app(), headers=headers).post(path, json={})

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "promotion_qualification_blocked"
    assert detail["promotion_qualification"]["reason_code"] == "legacy_policy"
    persisted = json.loads(reg_path.read_text(encoding="utf-8"))
    assert persisted["recommendations"][0]["status"] == "draft"


def test_supersede_closes_approved_audit_record_when_step2_is_blocked(
    tmp_path: Path,
) -> None:
    rec = {
        **_apply_capable_rec("rec_audit_supersede", "approved"),
        "source_round_id": "legacy_round",
        "evidence_bundle_ref": "legacy_round",
    }
    reg_path = tmp_path / "artifacts/decision_system/recommendation_registry.json"
    reg_path.parent.mkdir(parents=True)
    reg_path.write_text(
        json.dumps({"version": 0, "recommendations": [rec]}, ensure_ascii=False),
        encoding="utf-8",
    )
    step2_check = MagicMock(return_value="Step2 degraded")

    with (
        patch("aats.api.rdp_routes._project_root", return_value=tmp_path),
        patch(
            "aats.api.rdp_routes._step2_integrity_blocking_reason",
            step2_check,
        ),
        patch(
            "aats.data_platform.decision_system.recommendation_registry.try_governance_db",
            return_value=(None, False),
        ),
        patch(
            "aats.data_platform.decision_system.recommendation_registry.has_explicit_governance_db_configuration",
            return_value=False,
        ),
        patch(
            "aats.data_platform.decision_system.recommendation_registry._db_update_rec_status",
            return_value=True,
        ),
    ):
        response = TestClient(_build_app()).post(
            "/rdp/recommendations/rec_audit_supersede/supersede",
            json={"actor": "operator", "notes": "close legacy audit record"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["recommendation"]["status"] == "superseded"
    assert payload["recommendation_mirror_status"] == "degraded"
    assert payload["recommendation_mirror_refreshed"] is False
    # 本测试只模拟了 DB CAS 成功，未提供 canonical DB readback。因此路由
    # 应保持 200 canonical 结果并显式标记镜像降级，而不是用请求旧快照
    # 盲写 JSON；镜像仍保留之前的 approved 是预期的 fail-safe 语义。
    assert json.loads(reg_path.read_text(encoding="utf-8"))["recommendations"][0][
        "status"
    ] == "approved"
    step2_check.assert_not_called()
