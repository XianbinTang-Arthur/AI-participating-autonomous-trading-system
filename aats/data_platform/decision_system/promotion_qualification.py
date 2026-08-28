"""Exact-round promotion qualification for apply-capable recommendations.

The verifier is intentionally read-only and fail-closed.  A reachable
governance database is authoritative; project-local files are used only when
the database itself is unavailable.  No latest-round lookup is permitted.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from sqlalchemy.orm import Session

from aats.data_platform.decision_system.evidence_bundle import (
    PHASE2_PROMOTION_QUALIFICATION_POLICY,
    get_phase2_combo_stats,
    make_combo_key,
    normalize_timeframe_value,
)
from aats.data_platform.decision_system.decision_engine import (
    decide_family_timeframe_status,
)
from aats.data_platform.decision_system.readiness_evaluator import (
    evaluate_promotion_readiness,
)
from aats.data_platform.decision_system.promotion_policy import (
    phase2_combo_meets_promotion_gate,
)
from aats.data_platform.governance._db_util import (
    VALID_REC_TYPES,
    has_explicit_governance_db_configuration,
    try_governance_db,
)
from aats.data_platform.governance.decision_rounds_db import (
    db_load_decision_round_snapshots,
)
from aats.data_platform.governance._time_util import parse_iso_datetime_utc
from aats.data_platform.governance.auto_import_candidates import (
    load_validated_formal_step3_candidate,
    materialize_validated_step3_parameter_sets,
)
from aats.data_platform.governance.parameter_identity import (
    parameter_values_fingerprint,
)
from aats.data_platform.governance.research_artifact_contract import (
    read_stable_json_artifact,
)
from aats.data_platform.governance.snapshot_db import (
    ROUND_PHASE_STEP3,
    load_research_round_snapshot,
)

_ROUND_ID = re.compile(r"^[0-9]{8}_[0-9]{6}_[0-9a-f]{8}$")
_PARAMETER_VALUES_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
_PROMOTE_SCORE_RATIO_MIN = 0.7
_PROMOTE_SCORE_RATIO_MAX = 1.0
_FT_DECISIONS = frozenset(
    {"keep_active", "lower_priority", "pause", "require_review"}
)
_DECISION_ROUNDS_ROOT = Path("artifacts") / "decision_rounds"
_REQUIRED_OUTPUT_REFS = {
    "evidence_summary": "evidence_summary.json",
    "upgrade_candidates": "parameter_upgrade_candidates.json",
    "ft_decisions": "family_timeframe_decisions.json",
    "readiness_report": "promotion_readiness_report.json",
}
PROMOTION_MAX_EVIDENCE_AGE = timedelta(hours=168)
_EXPLICIT_TIMEZONE_SUFFIX = re.compile(r"(?:Z|[+-][0-9]{2}:[0-9]{2})$")
_READY_STATUS = "ready_for_next_live_test"
_READINESS_STATUSES = frozenset(
    {
        _READY_STATUS,
        "not_ready_more_research_needed",
        "not_ready_attribution_issue",
        "not_ready_execution_issue",
        "not_ready_governance_issue",
    }
)
_READINESS_CHECKS = (
    "research_stability",
    "attribution_no_severe_issue",
    "execution_not_severe",
    "governance_healthy",
    "parameter_traceable",
    "has_promote_candidate",
    "has_keep_active_ft",
)
_READINESS_FIELDS = frozenset(
    {
        "generated_at",
        "readiness",
        "overall_confidence",
        "checks_total",
        "checks_passed",
        "checks_failed",
        "blockers",
        "checks",
        "promoted_candidates",
        "active_family_timeframes",
    }
)
_CONTROL_PLANE_PUBLICATION_FIELDS = frozenset(
    {
        "schema_version",
        "recommendations",
        "active_decisions",
        "evidence_bundle",
    }
)
_PUBLICATION_RECOMMENDATION_FIELDS = frozenset(
    {
        "producer_index",
        "recommendation_id",
        "created_at",
        "family",
        "symbol",
        "timeframe",
        "recommendation_type",
        "target_parameter_set_id",
        "source_round_id",
        "confidence",
        "reason",
        "evidence_bundle_ref",
    }
)

_DETAILS = {
    "not_required": "该 recommendation 不具备参数应用能力，无需本资格门闸。",
    "qualified": "精确引用的 Phase 6 round 与参数升级候选资格一致。",
    "recommendation_invalid": "Recommendation 结构或身份字段无效。",
    "recommendation_id_duplicate": "同一批次存在重复 recommendation_id。",
    "target_parameter_set_required": "参数升级缺少目标 parameter set。",
    "source_round_id_required": "参数升级缺少来源研究 round。",
    "evidence_bundle_ref_required": "参数升级缺少 evidence bundle 精确引用。",
    "evidence_bundle_ref_invalid": "Evidence bundle 引用不是规范 decision round ID。",
    "promotion_round_db_error": "治理数据库可达，但精确 round 查询失败。",
    "promotion_round_not_found": "未找到 recommendation 精确引用的 decision round。",
    "promotion_round_path_escape": "Decision round 或输出引用越出项目内精确目录。",
    "promotion_round_manifest_invalid": "Decision round manifest 缺失或结构无效。",
    "promotion_round_id_mismatch": "Snapshot、manifest 与 evidence 引用的 round ID 不一致。",
    "promotion_round_phase_invalid": "精确 round 不是 Phase 6。",
    "promotion_round_status_invalid": "精确 round 未成功完成。",
    "promotion_recommendation_publication_mismatch": (
        "Recommendation 不属于精确 round 的不可变 control-plane 发布映射。"
    ),
    "promotion_round_finished_at_invalid": "精确 round 缺少可信且一致的完成时间。",
    "promotion_round_stale": "精确 round 已超过 168 小时资格有效期。",
    "promotion_round_scope_mismatch": "Manifest scope 与 recommendation 身份不一致。",
    "promotion_round_output_ref_invalid": "Manifest 的关键输出引用无效。",
    "promotion_round_readiness_invalid": "Promotion readiness 产物缺失、失配或结构无效。",
    "promotion_round_not_ready": "精确 round 未达到 ready_for_next_live_test。",
    "promotion_round_evidence_invalid": "Evidence summary 缺失或结构无效。",
    "promotion_policy_unsupported": "精确 round 未使用现行 Phase 2 promotion policy。",
    "promotion_combo_unavailable": "目标 family/timeframe 没有合格且发生 opening 的 Phase 2 证据。",
    "promotion_candidate_list_invalid": "参数升级候选输出缺失或结构无效。",
    "promotion_candidate_count_mismatch": "Manifest 候选数量与精确输出不一致。",
    "promotion_candidate_missing": "精确 round 中不存在目标 parameter set 候选。",
    "promotion_candidate_ambiguous": "精确 round 中目标 parameter set 候选不唯一。",
    "promotion_candidate_decision_invalid": "目标候选不是 promote_candidate。",
    "promotion_candidate_identity_mismatch": "候选 family/timeframe/symbol 与 recommendation 不一致。",
    "promotion_source_round_mismatch": "候选与 recommendation 的 source_round_id 不一致。",
    "promotion_candidate_values_fingerprint_invalid": (
        "目标候选缺少可验证的参数值指纹，必须用当前代码重新运行 Phase 6。"
    ),
    "promotion_candidate_step3_lineage_invalid": (
        "目标参数集无法回查到当前项目内完整且可信的正式 Step 3 候选身份。"
    ),
    "promotion_candidate_readiness_mismatch": (
        "Promotion readiness 未唯一绑定目标参数候选及其精确评分。"
    ),
    "promotion_candidate_phase3_evidence_invalid": (
        "目标候选缺少同 family/timeframe 的成功 Phase 3 实盘归因证据。"
    ),
    "promotion_candidate_phase4_evidence_invalid": (
        "目标候选缺少同 family/timeframe 的成功 Phase 4 执行可行性证据。"
    ),
    "promotion_qualification_internal_error": "资格校验发生异常，已失败关闭。",
}


@dataclass(frozen=True, slots=True)
class PromotionQualificationVerdict:
    required: bool
    eligible: bool
    reason_code: str
    evidence_bundle_ref: str | None
    source_round_id: str | None
    qualified_round_id: str | None
    detail: str
    qualified_finished_at: str | None = None
    parameter_values_fingerprint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "required": self.required,
            "eligible": self.eligible,
            "reason_code": self.reason_code,
            "evidence_bundle_ref": self.evidence_bundle_ref,
            "source_round_id": self.source_round_id,
            "qualified_round_id": self.qualified_round_id,
            "qualified_finished_at": self.qualified_finished_at,
            "parameter_values_fingerprint": self.parameter_values_fingerprint,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class _QualificationRequest:
    recommendation_id: str
    recommendation_type: str
    evidence_bundle_ref: str
    source_round_id: str
    target_parameter_set_id: str
    family: str
    timeframe: str
    symbol: str
    confidence: str
    reason: str
    created_at: str


@dataclass(frozen=True, slots=True)
class _SnapshotResolution:
    snapshot: Mapping[str, Any] | None
    reason_code: str | None = None


def evaluate_promotion_qualification(
    project_root: Path,
    recommendation: Mapping[str, Any],
) -> PromotionQualificationVerdict:
    """Evaluate one recommendation against its exact Phase 6 round."""

    return _evaluate_many(project_root, [recommendation])[0]


def evaluate_promotion_qualifications(
    project_root: Path,
    recommendations: Sequence[Mapping[str, Any]],
) -> dict[str, PromotionQualificationVerdict]:
    """Batch-evaluate recommendations with one DB session and round cache.

    Valid registries have unique recommendation IDs.  A duplicate is returned
    as an ineligible verdict for that ID instead of allowing last-write-wins to
    hide the malformed batch.
    """

    items = list(recommendations)
    verdicts = _evaluate_many(project_root, items)
    output: dict[str, PromotionQualificationVerdict] = {}
    for index, (recommendation, verdict) in enumerate(zip(items, verdicts, strict=True)):
        recommendation_id = _exact_nonempty_string(
            recommendation.get("recommendation_id")
        )
        key = recommendation_id or f"__invalid_recommendation_{index}"
        if key in output:
            output[key] = _verdict(
                required=True,
                eligible=False,
                reason_code="recommendation_id_duplicate",
                recommendation=recommendation,
            )
        else:
            output[key] = verdict
    return output


def _evaluate_many(
    project_root: Path,
    recommendations: Sequence[Mapping[str, Any]],
) -> list[PromotionQualificationVerdict]:
    preliminary: list[PromotionQualificationVerdict | _QualificationRequest] = [
        _prepare_request(recommendation) for recommendation in recommendations
    ]
    round_ids = {
        item.evidence_bundle_ref
        for item in preliminary
        if isinstance(item, _QualificationRequest)
    }
    resolutions = _load_exact_rounds(project_root, round_ids) if round_ids else {}

    verdicts: list[PromotionQualificationVerdict] = []
    for item, recommendation in zip(preliminary, recommendations, strict=True):
        if isinstance(item, PromotionQualificationVerdict):
            verdicts.append(item)
            continue
        resolution = resolutions.get(item.evidence_bundle_ref)
        if resolution is None or resolution.snapshot is None:
            verdicts.append(
                _verdict(
                    required=True,
                    eligible=False,
                    reason_code=(
                        resolution.reason_code
                        if resolution is not None and resolution.reason_code is not None
                        else "promotion_round_not_found"
                    ),
                    recommendation=recommendation,
                )
            )
            continue
        try:
            verdicts.append(
                _evaluate_snapshot(
                    project_root,
                    item,
                    recommendation,
                    resolution.snapshot,
                )
            )
        except Exception:
            verdicts.append(
                _verdict(
                    required=True,
                    eligible=False,
                    reason_code="promotion_qualification_internal_error",
                    recommendation=recommendation,
                )
            )
    return verdicts


def _prepare_request(
    recommendation: Mapping[str, Any],
) -> PromotionQualificationVerdict | _QualificationRequest:
    raw_recommendation_type = recommendation.get("recommendation_type")
    recommendation_type = _exact_nonempty_string(raw_recommendation_type)
    if raw_recommendation_type is not None and recommendation_type is None:
        return _verdict(
            required=True,
            eligible=False,
            reason_code="recommendation_invalid",
            recommendation=recommendation,
        )
    raw_target_parameter_set_id = recommendation.get("target_parameter_set_id")
    target_parameter_set_id = _exact_nonempty_string(raw_target_parameter_set_id)
    if raw_target_parameter_set_id is not None and target_parameter_set_id is None:
        return _verdict(
            required=True,
            eligible=False,
            reason_code="recommendation_invalid",
            recommendation=recommendation,
        )
    if recommendation_type is not None and recommendation_type not in VALID_REC_TYPES:
        return _verdict(
            required=True,
            eligible=False,
            reason_code="recommendation_invalid",
            recommendation=recommendation,
        )
    if (
        recommendation_type not in {None, "parameter_upgrade"}
        and target_parameter_set_id is not None
    ):
        # A non-apply type carrying an apply target is internally inconsistent.
        # Treating it as not-required would let a caller relabel a parameter
        # upgrade as ``pause`` while retaining its target and bypass approval.
        return _verdict(
            required=True,
            eligible=False,
            reason_code="recommendation_invalid",
            recommendation=recommendation,
        )
    required = (
        recommendation_type == "parameter_upgrade"
        or target_parameter_set_id is not None
    )
    if not required:
        return _verdict(
            required=False,
            eligible=True,
            reason_code="not_required",
            recommendation=recommendation,
        )

    recommendation_id = _exact_nonempty_string(
        recommendation.get("recommendation_id")
    )
    if recommendation_id is None:
        return _verdict(
            required=True,
            eligible=False,
            reason_code="recommendation_invalid",
            recommendation=recommendation,
        )
    if target_parameter_set_id is None:
        return _verdict(
            required=True,
            eligible=False,
            reason_code="target_parameter_set_required",
            recommendation=recommendation,
        )
    source_round_id = _exact_nonempty_string(recommendation.get("source_round_id"))
    if source_round_id is None:
        return _verdict(
            required=True,
            eligible=False,
            reason_code="source_round_id_required",
            recommendation=recommendation,
        )
    evidence_bundle_ref = _exact_nonempty_string(
        recommendation.get("evidence_bundle_ref")
    )
    if evidence_bundle_ref is None:
        return _verdict(
            required=True,
            eligible=False,
            reason_code="evidence_bundle_ref_required",
            recommendation=recommendation,
        )
    if _ROUND_ID.fullmatch(evidence_bundle_ref) is None:
        return _verdict(
            required=True,
            eligible=False,
            reason_code="evidence_bundle_ref_invalid",
            recommendation=recommendation,
        )

    family = _exact_nonempty_string(recommendation.get("family"))
    timeframe_raw = _exact_nonempty_string(recommendation.get("timeframe"))
    symbol = _exact_nonempty_string(recommendation.get("symbol"))
    timeframe = normalize_timeframe_value(timeframe_raw)
    if family is None or timeframe is None or symbol is None:
        return _verdict(
            required=True,
            eligible=False,
            reason_code="recommendation_invalid",
            recommendation=recommendation,
        )
    confidence = _exact_nonempty_string(recommendation.get("confidence"))
    reason = recommendation.get("reason")
    created_at = _canonical_publication_created_at(
        recommendation.get("created_at")
    )
    if (
        confidence not in {"low", "medium", "high"}
        or type(reason) is not str
        or created_at is None
    ):
        return _verdict(
            required=True,
            eligible=False,
            reason_code="recommendation_invalid",
            recommendation=recommendation,
        )
    return _QualificationRequest(
        recommendation_id=recommendation_id,
        recommendation_type=recommendation_type,
        evidence_bundle_ref=evidence_bundle_ref,
        source_round_id=source_round_id,
        target_parameter_set_id=target_parameter_set_id,
        family=family,
        timeframe=timeframe,
        symbol=symbol,
        confidence=confidence,
        reason=reason,
        created_at=created_at,
    )


def _load_exact_rounds(
    project_root: Path,
    round_ids: set[str],
) -> dict[str, _SnapshotResolution]:
    try:
        engine, db_available = try_governance_db()
    except Exception:
        return {
            round_id: _SnapshotResolution(None, "promotion_round_db_error")
            for round_id in round_ids
        }
    if not db_available:
        # ``try_governance_db`` deliberately uses the same ``False`` result for
        # both file-only development and a configured-but-unreachable DB.  The
        # latter must not downgrade to mutable local artefacts: once a DB URL is
        # configured, its exact-ID snapshot is the authoritative source.
        if has_explicit_governance_db_configuration(project_root):
            return {
                round_id: _SnapshotResolution(None, "promotion_round_db_error")
                for round_id in round_ids
            }
        return {
            round_id: _load_file_snapshot(project_root, round_id)
            for round_id in sorted(round_ids)
        }

    resolutions: dict[str, _SnapshotResolution] = {}
    try:
        with Session(engine) as session:
            snapshots = db_load_decision_round_snapshots(
                session,
                round_ids=sorted(round_ids),
            )
            for round_id in round_ids:
                snapshot = snapshots.get(round_id)
                resolutions[round_id] = _SnapshotResolution(
                    snapshot,
                    None if snapshot is not None else "promotion_round_not_found",
                )
    except Exception:
        resolutions = {
            round_id: _SnapshotResolution(None, "promotion_round_db_error")
            for round_id in round_ids
        }
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                pass
    return resolutions


def _load_file_snapshot(project_root: Path, round_id: str) -> _SnapshotResolution:
    try:
        root = project_root.resolve(strict=False)
        rounds_root = (root / _DECISION_ROUNDS_ROOT).resolve(strict=False)
        if not rounds_root.is_relative_to(root):
            return _SnapshotResolution(None, "promotion_round_path_escape")
        round_dir = (rounds_root / round_id).resolve(strict=True)
        if round_dir.parent != rounds_root or not round_dir.is_dir():
            return _SnapshotResolution(None, "promotion_round_path_escape")
    except FileNotFoundError:
        return _SnapshotResolution(None, "promotion_round_not_found")
    except (OSError, RuntimeError, ValueError):
        return _SnapshotResolution(None, "promotion_round_path_escape")

    manifest = _strict_json(round_dir / "round_manifest.json", expected=dict)
    if manifest is None:
        return _SnapshotResolution(None, "promotion_round_manifest_invalid")
    output_refs = _validated_output_refs(manifest, round_dir=round_dir)
    if output_refs is None:
        return _SnapshotResolution(None, "promotion_round_output_ref_invalid")
    evidence = _strict_json(output_refs["evidence_summary"], expected=dict)
    if evidence is None:
        return _SnapshotResolution(None, "promotion_round_evidence_invalid")
    candidates = _strict_json(output_refs["upgrade_candidates"], expected=list)
    if candidates is None:
        return _SnapshotResolution(None, "promotion_candidate_list_invalid")
    ft_decisions = _strict_json(output_refs["ft_decisions"], expected=list)
    if ft_decisions is None:
        return _SnapshotResolution(None, "promotion_round_readiness_invalid")
    readiness = _strict_json(output_refs["readiness_report"], expected=dict)
    if readiness is None:
        return _SnapshotResolution(None, "promotion_round_readiness_invalid")
    return _SnapshotResolution(
        {
            "round_id": round_id,
            "started_at": manifest.get("started_at"),
            "finished_at": manifest.get("finished_at"),
            "manifest": manifest,
            "evidence_bundle_summary": evidence,
            "parameter_upgrade_candidates": candidates,
            "family_timeframe_decisions": ft_decisions,
            "promotion_readiness_assessment": readiness,
        }
    )


def _evaluate_snapshot(
    project_root: Path,
    request: _QualificationRequest,
    recommendation: Mapping[str, Any],
    snapshot: Mapping[str, Any],
) -> PromotionQualificationVerdict:
    if not isinstance(snapshot, Mapping) or not _json_value_is_finite(snapshot):
        return _failed("promotion_round_manifest_invalid", recommendation)
    if snapshot.get("round_id") != request.evidence_bundle_ref:
        return _failed("promotion_round_id_mismatch", recommendation)

    manifest = snapshot.get("manifest")
    if not isinstance(manifest, Mapping) or not _json_value_is_finite(manifest):
        return _failed("promotion_round_manifest_invalid", recommendation)
    if manifest.get("round_id") != request.evidence_bundle_ref:
        return _failed("promotion_round_id_mismatch", recommendation)
    if manifest.get("phase") != "phase6":
        return _failed("promotion_round_phase_invalid", recommendation)
    if manifest.get("status") != "succeeded":
        return _failed("promotion_round_status_invalid", recommendation)
    finished_at = _canonical_round_finished_at(snapshot, manifest)
    if finished_at is None:
        return _failed("promotion_round_finished_at_invalid", recommendation)
    if datetime.now(timezone.utc) - finished_at > PROMOTION_MAX_EVIDENCE_AGE:
        return _failed("promotion_round_stale", recommendation)
    if not _manifest_scope_matches(manifest, request):
        return _failed("promotion_round_scope_mismatch", recommendation)
    if _validated_output_refs(manifest) is None:
        return _failed("promotion_round_output_ref_invalid", recommendation)

    candidates = snapshot.get("parameter_upgrade_candidates")
    if (
        not isinstance(candidates, list)
        or not _json_value_is_finite(candidates)
        or not all(isinstance(candidate, Mapping) for candidate in candidates)
    ):
        return _failed("promotion_candidate_list_invalid", recommendation)
    declared_count = manifest.get("upgrade_candidates_count")
    if (
        type(declared_count) is not int
        or declared_count < 0
        or declared_count != len(candidates)
    ):
        return _failed("promotion_candidate_count_mismatch", recommendation)
    ft_decisions = snapshot.get("family_timeframe_decisions")
    declared_ft_count = manifest.get("ft_decisions_count")
    if (
        not isinstance(ft_decisions, list)
        or not _json_value_is_finite(ft_decisions)
        or not all(isinstance(item, Mapping) for item in ft_decisions)
        or type(declared_ft_count) is not int
        or declared_ft_count < 0
        or declared_ft_count != len(ft_decisions)
    ):
        return _failed("promotion_round_readiness_invalid", recommendation)
    target_ft_decision = _validated_target_ft_decision(ft_decisions, request)
    if target_ft_decision is None:
        return _failed("promotion_round_readiness_invalid", recommendation)

    evidence = snapshot.get("evidence_bundle_summary")
    if not isinstance(evidence, Mapping) or not _json_value_is_finite(evidence):
        return _failed("promotion_round_evidence_invalid", recommendation)
    phase2 = evidence.get("phase2_evidence")
    if not isinstance(phase2, Mapping):
        return _failed("promotion_round_evidence_invalid", recommendation)
    if (
        phase2.get("promotion_qualification_policy")
        != PHASE2_PROMOTION_QUALIFICATION_POLICY
    ):
        return _failed("promotion_policy_unsupported", recommendation)
    stats = get_phase2_combo_stats(dict(phase2), request.family, request.timeframe)
    if not _qualified_combo_stats(stats, request):
        return _failed("promotion_combo_unavailable", recommendation)
    try:
        recomputed_target_ft = decide_family_timeframe_status(
            request.family,
            request.timeframe,
            dict(evidence),
        )
    except Exception:
        return _failed("promotion_round_readiness_invalid", recommendation)
    if (
        target_ft_decision.get("decision")
        != recomputed_target_ft.get("decision")
        or target_ft_decision.get("confidence")
        != recomputed_target_ft.get("confidence")
    ):
        return _failed("promotion_round_readiness_invalid", recommendation)

    parameter_matches = [
        candidate
        for candidate in candidates
        if candidate.get("parameter_set_id") == request.target_parameter_set_id
    ]
    if not parameter_matches:
        return _failed("promotion_candidate_missing", recommendation)
    if len(parameter_matches) != 1:
        return _failed("promotion_candidate_ambiguous", recommendation)
    candidate = parameter_matches[0]
    if candidate.get("decision") != "promote_candidate":
        return _failed("promotion_candidate_decision_invalid", recommendation)
    if (
        candidate.get("family") != request.family
        or normalize_timeframe_value(candidate.get("timeframe")) != request.timeframe
        or candidate.get("symbol") != request.symbol
    ):
        return _failed("promotion_candidate_identity_mismatch", recommendation)
    candidate_source_round_id = _exact_nonempty_string(
        candidate.get("source_round_id")
    )
    if candidate_source_round_id != request.source_round_id:
        return _failed("promotion_source_round_mismatch", recommendation)
    parameter_values_fingerprint = _exact_nonempty_string(
        candidate.get("parameter_values_fingerprint")
    )
    if (
        parameter_values_fingerprint is None
        or _PARAMETER_VALUES_FINGERPRINT.fullmatch(parameter_values_fingerprint) is None
    ):
        return _failed(
            "promotion_candidate_values_fingerprint_invalid",
            recommendation,
        )

    round_started_at = _canonical_round_started_at(snapshot, manifest)
    readiness_reason = _validate_readiness_assessment(
        snapshot.get("promotion_readiness_assessment"),
        manifest=manifest,
        evidence=evidence,
        candidates=candidates,
        ft_decisions=ft_decisions,
        candidate=candidate,
        request=request,
        round_started_at=round_started_at,
        round_finished_at=finished_at,
    )
    if readiness_reason is not None:
        return _failed(readiness_reason, recommendation)
    phase3_lineage = _target_parameter_evidence_lineage(
        evidence,
        phase_key="phase3_evidence",
        request=request,
        parameter_values_fingerprint=parameter_values_fingerprint,
    )
    if phase3_lineage is None or not _target_phase3_evidence_qualified(
        evidence,
        request,
        phase6_started_at=round_started_at,
    ):
        return _failed(
            "promotion_candidate_phase3_evidence_invalid",
            recommendation,
        )
    phase4_lineage = _target_parameter_evidence_lineage(
        evidence,
        phase_key="phase4_evidence",
        request=request,
        parameter_values_fingerprint=parameter_values_fingerprint,
    )
    if (
        phase4_lineage is None
        or phase4_lineage != phase3_lineage
        or not _target_phase4_evidence_qualified(
            evidence,
            request,
            phase6_started_at=round_started_at,
        )
    ):
        return _failed(
            "promotion_candidate_phase4_evidence_invalid",
            recommendation,
        )
    if not _target_formal_step3_candidate_qualified(
        project_root,
        request,
        expected_values_fingerprint=parameter_values_fingerprint,
        candidate_sha256=phase3_lineage[1],
        canonical_step2_round_id=(
            evidence.get("phase2_evidence", {}).get(
                "canonical_step2_round_id"
            )
            if isinstance(evidence.get("phase2_evidence"), Mapping)
            else None
        ),
        canonical_step2_snapshot_sha256=(
            evidence.get("phase2_evidence", {}).get(
                "canonical_step2_snapshot_sha256"
            )
            if isinstance(evidence.get("phase2_evidence"), Mapping)
            else None
        ),
    ):
        return _failed(
            "promotion_candidate_step3_lineage_invalid",
            recommendation,
        )
    if not _recommendation_has_exact_publication_identity(manifest, request):
        return _failed(
            "promotion_recommendation_publication_mismatch",
            recommendation,
        )

    return _verdict(
        required=True,
        eligible=True,
        reason_code="qualified",
        recommendation=recommendation,
        qualified_round_id=request.evidence_bundle_ref,
        qualified_finished_at=finished_at.isoformat(),
        parameter_values_fingerprint=parameter_values_fingerprint,
    )


def _qualified_combo_stats(
    stats: Mapping[str, Any],
    request: _QualificationRequest,
) -> bool:
    total_experiments = stats.get("total_experiments")
    experiments_with_openings = stats.get("experiments_with_openings")
    max_opening_count = stats.get("max_opening_count")
    return (
        stats.get("available") is True
        and stats.get("family") == request.family
        and normalize_timeframe_value(stats.get("timeframe")) == request.timeframe
        and stats.get("combo_key") == make_combo_key(request.family, request.timeframe)
        and type(total_experiments) is int
        and type(experiments_with_openings) is int
        and type(max_opening_count) is int
        and total_experiments >= experiments_with_openings > 0
        and max_opening_count > 0
        and phase2_combo_meets_promotion_gate(stats)
    )


def _validated_target_ft_decision(
    ft_decisions: list[Any],
    request: _QualificationRequest,
) -> Mapping[str, Any] | None:
    seen: set[str] = set()
    target_combo_key = make_combo_key(request.family, request.timeframe)
    target: Mapping[str, Any] | None = None
    for item in ft_decisions:
        if not isinstance(item, Mapping):
            return None
        family = _exact_nonempty_string(item.get("family"))
        timeframe = normalize_timeframe_value(item.get("timeframe"))
        combo_key = _exact_nonempty_string(item.get("combo_key"))
        if (
            family is None
            or timeframe is None
            or combo_key is None
            or combo_key in seen
            or combo_key != make_combo_key(family, timeframe)
            or item.get("decision") not in _FT_DECISIONS
            or item.get("confidence") not in {"low", "medium", "high"}
        ):
            return None
        seen.add(combo_key)
        if combo_key == target_combo_key:
            target = item
    return target


def _validate_readiness_assessment(
    assessment: Any,
    *,
    manifest: Mapping[str, Any],
    evidence: Mapping[str, Any],
    candidates: list[Any],
    ft_decisions: list[Any],
    candidate: Mapping[str, Any],
    request: _QualificationRequest,
    round_started_at: datetime | None,
    round_finished_at: datetime,
) -> str | None:
    """Validate the exact Phase 6 readiness product and target binding.

    The readiness report has no round ID of its own.  Its identity therefore
    comes from the enclosing immutable snapshot, an exact manifest status, and
    a generated timestamp inside that round's start/finish interval.  Counts,
    the seven-check state machine, promoted candidates, and the target combo
    are recomputed instead of trusted.
    """

    if (
        type(assessment) is not dict
        or not assessment
        or frozenset(assessment) != _READINESS_FIELDS
        or not _json_value_is_finite(assessment)
    ):
        return "promotion_round_readiness_invalid"

    readiness = assessment.get("readiness")
    confidence = assessment.get("overall_confidence")
    if (
        type(readiness) is not str
        or readiness not in _READINESS_STATUSES
        or type(confidence) is not str
        or confidence not in {"medium", "high"}
        or manifest.get("readiness") != readiness
    ):
        return "promotion_round_readiness_invalid"

    generated_at = _parse_explicit_timestamp(
        assessment.get("generated_at"),
        context="promotion_qualification.readiness.generated_at",
    )
    if (
        round_started_at is None
        or generated_at is None
        or round_started_at > round_finished_at
        or generated_at < round_started_at
        or generated_at > round_finished_at
    ):
        return "promotion_round_readiness_invalid"

    counts = tuple(
        assessment.get(field)
        for field in ("checks_total", "checks_passed", "checks_failed")
    )
    if any(type(value) is not int or value < 0 for value in counts):
        return "promotion_round_readiness_invalid"
    checks_total, checks_passed, checks_failed = counts

    checks = assessment.get("checks")
    blockers = assessment.get("blockers")
    promoted = assessment.get("promoted_candidates")
    active = assessment.get("active_family_timeframes")
    if not all(type(value) is list for value in (checks, blockers, promoted, active)):
        return "promotion_round_readiness_invalid"
    if len(checks) != len(_READINESS_CHECKS) or checks_total != len(checks):
        return "promotion_round_readiness_invalid"

    for expected_name, check in zip(_READINESS_CHECKS, checks, strict=True):
        if (
            type(check) is not dict
            or frozenset(check) != {"check", "passed", "detail"}
            or check.get("check") != expected_name
            or type(check.get("passed")) is not bool
            or _exact_nonempty_string(check.get("detail")) is None
        ):
            return "promotion_round_readiness_invalid"

    actual_passed = sum(1 for check in checks if check["passed"])
    actual_failed = len(checks) - actual_passed
    if (
        checks_passed != actual_passed
        or checks_failed != actual_failed
        or checks_passed + checks_failed != checks_total
        or not all(_exact_nonempty_string(blocker) is not None for blocker in blockers)
        or len(blockers) != actual_failed
    ):
        return "promotion_round_readiness_invalid"

    derived_readiness = _derived_readiness(checks)
    expected_confidence = (
        "high" if actual_failed == 0 or len(blockers) > 2 else "medium"
    )
    if readiness != derived_readiness or confidence != expected_confidence:
        return "promotion_round_readiness_invalid"

    expected_promoted: list[dict[str, Any]] = []
    expected_ids: set[str] = set()
    for item in candidates:
        if item.get("decision") != "promote_candidate":
            continue
        parameter_set_id = _exact_nonempty_string(item.get("parameter_set_id"))
        score_ratio = _finite_number(item.get("score_ratio"))
        expected_candidate_confidence = (
            "high"
            if score_ratio is not None and score_ratio >= 0.85
            else "medium"
        )
        if (
            parameter_set_id is None
            or parameter_set_id in expected_ids
            or score_ratio is None
            or not _PROMOTE_SCORE_RATIO_MIN
            <= score_ratio
            <= _PROMOTE_SCORE_RATIO_MAX
            or item.get("confidence") != expected_candidate_confidence
        ):
            return "promotion_candidate_readiness_mismatch"
        expected_ids.add(parameter_set_id)
        expected_promoted.append(
            {
                "parameter_set_id": parameter_set_id,
                "score_ratio": item.get("score_ratio"),
            }
        )

    promoted_ids: set[str] = set()
    for item in promoted:
        if type(item) is not dict or frozenset(item) != {
            "parameter_set_id",
            "score_ratio",
        }:
            return "promotion_round_readiness_invalid"
        parameter_set_id = _exact_nonempty_string(item.get("parameter_set_id"))
        if (
            parameter_set_id is None
            or parameter_set_id in promoted_ids
            or _finite_number(item.get("score_ratio")) is None
        ):
            return "promotion_round_readiness_invalid"
        promoted_ids.add(parameter_set_id)
    if promoted != expected_promoted:
        return "promotion_candidate_readiness_mismatch"

    active_combos: set[str] = set()
    for item in active:
        if type(item) is not dict or frozenset(item) != {"combo_key", "confidence"}:
            return "promotion_round_readiness_invalid"
        combo_key = _exact_nonempty_string(item.get("combo_key"))
        if (
            combo_key is None
            or combo_key in active_combos
            or item.get("confidence") not in {"low", "medium", "high"}
        ):
            return "promotion_round_readiness_invalid"
        active_combos.add(combo_key)

    try:
        recomputed = evaluate_promotion_readiness(
            dict(evidence),
            [dict(item) for item in candidates],
            [dict(item) for item in ft_decisions],
        )
    except Exception:
        return "promotion_round_readiness_invalid"
    if (
        type(recomputed) is not dict
        or frozenset(recomputed) != _READINESS_FIELDS
        or any(
            assessment.get(field) != recomputed.get(field)
            for field in _READINESS_FIELDS - {"generated_at"}
        )
    ):
        return "promotion_round_readiness_invalid"

    checks_by_name = {check["check"]: check for check in checks}
    if (
        checks_by_name["has_promote_candidate"]["passed"] is not bool(promoted)
        or checks_by_name["has_keep_active_ft"]["passed"] is not bool(active)
    ):
        return "promotion_round_readiness_invalid"
    if readiness != _READY_STATUS:
        return "promotion_round_not_ready"

    target_score = _finite_number(candidate.get("score_ratio"))
    target_promoted = [
        item
        for item in promoted
        if item.get("parameter_set_id") == request.target_parameter_set_id
    ]
    target_combo_key = make_combo_key(request.family, request.timeframe)
    if (
        target_score is None
        or len(target_promoted) != 1
        or _finite_number(target_promoted[0].get("score_ratio")) != target_score
        or target_combo_key is None
        or target_combo_key not in active_combos
    ):
        return "promotion_candidate_readiness_mismatch"
    return None


def _derived_readiness(checks: list[dict[str, Any]]) -> str:
    if all(check["passed"] for check in checks):
        return _READY_STATUS
    if not checks[0]["passed"]:
        return "not_ready_more_research_needed"
    if not checks[1]["passed"]:
        return "not_ready_attribution_issue"
    if not checks[2]["passed"]:
        return "not_ready_execution_issue"
    if not checks[3]["passed"]:
        return "not_ready_governance_issue"
    return "not_ready_more_research_needed"


def _target_phase3_evidence_qualified(
    evidence: Mapping[str, Any],
    request: _QualificationRequest,
    *,
    phase6_started_at: datetime | None,
) -> bool:
    phase3 = evidence.get("phase3_evidence")
    governance_index_used = evidence.get("governance_index_used")
    if (
        not isinstance(phase3, Mapping)
        or phase3.get("source") != "phase3"
        or phase3.get("evidence_source") != "governance_index"
        or not isinstance(governance_index_used, Mapping)
        or governance_index_used.get("active_round_index") is not True
    ):
        return False
    round_count = phase3.get("round_count")
    trusted_round_count = phase3.get("trusted_round_count")
    latest = phase3.get("latest_round")
    latest_started_at = (
        _parse_explicit_timestamp(
            latest.get("started_at"),
            context="promotion_qualification.phase3.started_at",
        )
        if isinstance(latest, Mapping)
        else None
    )
    if (
        type(round_count) is not int
        or type(trusted_round_count) is not int
        or round_count <= 0
        or trusted_round_count <= 0
        or trusted_round_count > round_count
        or not isinstance(latest, Mapping)
        or phase6_started_at is None
        or latest_started_at is None
        or latest_started_at > phase6_started_at
        or phase6_started_at - latest_started_at > PROMOTION_MAX_EVIDENCE_AGE
        or _ROUND_ID.fullmatch(str(latest.get("round_id") or "")) is None
        or latest.get("status") != "succeeded"
        or latest.get("replay_only") is not False
        or latest.get("live_query_succeeded") is not True
    ):
        return False

    combos = latest.get("combos")
    combo_key = make_combo_key(request.family, request.timeframe)
    if not isinstance(combos, Mapping) or combo_key is None:
        return False
    combo = combos.get(combo_key)
    if (
        not isinstance(combo, Mapping)
        or combo.get("status") != "succeeded"
        or combo.get("live_query_succeeded") is not True
    ):
        return False
    alignment = combo.get("alignment_stats")
    if not isinstance(alignment, Mapping):
        return False
    aligned = alignment.get("aligned")
    unattributable = alignment.get("unattributable")
    return (
        type(aligned) is int
        and aligned > 0
        and type(unattributable) is int
        and unattributable == 0
    )


def _target_phase4_evidence_qualified(
    evidence: Mapping[str, Any],
    request: _QualificationRequest,
    *,
    phase6_started_at: datetime | None,
) -> bool:
    phase4 = evidence.get("phase4_evidence")
    governance_index_used = evidence.get("governance_index_used")
    if (
        not isinstance(phase4, Mapping)
        or phase4.get("source") != "phase4"
        or phase4.get("evidence_source") != "governance_index"
        or not isinstance(governance_index_used, Mapping)
        or governance_index_used.get("active_round_index") is not True
    ):
        return False
    round_count = phase4.get("round_count")
    trusted_round_count = phase4.get("trusted_round_count")
    latest = phase4.get("latest_round")
    latest_started_at = (
        _parse_explicit_timestamp(
            latest.get("started_at"),
            context="promotion_qualification.phase4.started_at",
        )
        if isinstance(latest, Mapping)
        else None
    )
    if (
        type(round_count) is not int
        or type(trusted_round_count) is not int
        or round_count <= 0
        or trusted_round_count <= 0
        or trusted_round_count > round_count
        or not isinstance(latest, Mapping)
        or phase6_started_at is None
        or latest_started_at is None
        or latest_started_at > phase6_started_at
        or phase6_started_at - latest_started_at > PROMOTION_MAX_EVIDENCE_AGE
        or _ROUND_ID.fullmatch(str(latest.get("round_id") or "")) is None
        or latest.get("status") != "succeeded"
    ):
        return False

    combos = latest.get("combos")
    combo_key = make_combo_key(request.family, request.timeframe)
    if not isinstance(combos, Mapping) or combo_key is None:
        return False
    combo = combos.get(combo_key)
    if (
        not isinstance(combo, Mapping)
        or combo.get("status") != "succeeded"
    ):
        return False
    cost_summary = combo.get("cost_summary")
    if not isinstance(cost_summary, Mapping):
        return False
    total_candidates = cost_summary.get("total_candidates")
    cost_adjusted_edge = _finite_number(
        cost_summary.get("cost_adjusted_edge_mean")
    )
    full_fill_ratio = _finite_number(cost_summary.get("full_fill_ratio"))
    return (
        type(total_candidates) is int
        and total_candidates > 0
        and cost_adjusted_edge is not None
        and cost_adjusted_edge >= 0.0
        and full_fill_ratio is not None
        and 0.3 <= full_fill_ratio <= 1.0
    )


def _target_parameter_evidence_lineage(
    evidence: Mapping[str, Any],
    *,
    phase_key: str,
    request: _QualificationRequest,
    parameter_values_fingerprint: str,
) -> tuple[str, str] | None:
    phase = evidence.get(phase_key)
    combo_key = make_combo_key(request.family, request.timeframe)
    if not isinstance(phase, Mapping) or combo_key is None:
        return None
    latest = phase.get("latest_round")
    if not isinstance(latest, Mapping):
        return None
    combos = latest.get("combos")
    if not isinstance(combos, Mapping):
        return None
    combo = combos.get(combo_key)
    if (
        not isinstance(combo, Mapping)
        or _ROUND_ID.fullmatch(request.source_round_id) is None
        or combo.get("source_step3_round_id") != request.source_round_id
        or combo.get("parameter_values_fingerprint")
        != parameter_values_fingerprint
    ):
        return None
    resolved_fingerprint = _exact_nonempty_string(
        combo.get("resolved_parameter_values_fingerprint")
    )
    candidate_sha256 = _exact_nonempty_string(
        combo.get("source_step3_candidate_sha256")
    )
    if (
        resolved_fingerprint is None
        or _PARAMETER_VALUES_FINGERPRINT.fullmatch(resolved_fingerprint) is None
        or candidate_sha256 is None
        or _PARAMETER_VALUES_FINGERPRINT.fullmatch(candidate_sha256) is None
    ):
        return None
    return resolved_fingerprint, candidate_sha256


def _target_formal_step3_candidate_qualified(
    project_root: Path,
    request: _QualificationRequest,
    *,
    expected_values_fingerprint: str,
    candidate_sha256: str,
    canonical_step2_round_id: Any,
    canonical_step2_snapshot_sha256: Any,
) -> bool:
    """Bind Phase 2/3/4 evidence to one managed Step 3 parent chain."""

    if (
        type(canonical_step2_round_id) is not str
        or _ROUND_ID.fullmatch(canonical_step2_round_id) is None
        or type(canonical_step2_snapshot_sha256) is not str
        or _PARAMETER_VALUES_FINGERPRINT.fullmatch(
            canonical_step2_snapshot_sha256
        )
        is None
    ):
        return False

    candidate_path = (
        project_root
        / "artifacts"
        / "research"
        / "step3_rounds"
        / request.source_round_id
        / "parameter_candidates_merged.json"
    )
    artifact = load_validated_formal_step3_candidate(
        project_root,
        candidate_path,
        expected_round_id=request.source_round_id,
        expected_candidate_sha256=candidate_sha256,
    )
    if (
        artifact is None
        or artifact.metadata.get("status") != "succeeded"
        or artifact.metadata.get("symbol") != request.symbol
    ):
        return False
    try:
        step3_snapshot = load_research_round_snapshot(
            round_id=request.source_round_id,
            project_root=project_root,
            require_managed_db_truth=True,
        )
    except Exception:
        return False
    step3_artifacts = (
        step3_snapshot.get("artifacts")
        if isinstance(step3_snapshot, Mapping)
        else None
    )
    if (
        not isinstance(step3_snapshot, Mapping)
        or step3_snapshot.get("data_source") != "db"
        or step3_snapshot.get("round_id") != request.source_round_id
        or step3_snapshot.get("phase") != ROUND_PHASE_STEP3
        or step3_snapshot.get("status") != "succeeded"
        or not isinstance(step3_artifacts, Mapping)
        or step3_artifacts.get("step2_round_id")
        != canonical_step2_round_id
        or step3_artifacts.get("step2_snapshot_sha256")
        != canonical_step2_snapshot_sha256
    ):
        return False
    try:
        parameter_sets = materialize_validated_step3_parameter_sets(
            artifact,
            initial_status="candidate",
        )
    except (KeyError, TypeError, ValueError):
        return False
    matches = [
        parameter_set
        for parameter_set in parameter_sets
        if parameter_set.get("parameter_set_id") == request.target_parameter_set_id
    ]
    if len(matches) != 1:
        return False
    parameter_set = matches[0]
    try:
        formal_values_fingerprint = parameter_values_fingerprint(
            parameter_set.get("values")
        )
    except ValueError:
        return False
    return bool(
        parameter_set.get("family") == request.family
        and normalize_timeframe_value(parameter_set.get("timeframe"))
        == request.timeframe
        and parameter_set.get("symbol") == request.symbol
        and parameter_set.get("source_round_id") == request.source_round_id
        and parameter_set.get("source_phase") == "step3_merged"
        and formal_values_fingerprint == expected_values_fingerprint
    )


def _finite_number(value: Any) -> float | None:
    if type(value) not in {int, float}:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _manifest_scope_matches(
    manifest: Mapping[str, Any],
    request: _QualificationRequest,
) -> bool:
    scope = manifest.get("scope")
    if not isinstance(scope, Mapping) or scope.get("symbol") != request.symbol:
        return False
    families = scope.get("families")
    timeframes = scope.get("timeframes")
    return (
        isinstance(families, list)
        and all(type(value) is str for value in families)
        and request.family in families
        and isinstance(timeframes, list)
        and all(type(value) is str for value in timeframes)
        and request.timeframe
        in {normalize_timeframe_value(value) for value in timeframes}
    )


def _validated_output_refs(
    manifest: Mapping[str, Any],
    *,
    round_dir: Path | None = None,
) -> dict[str, Path] | dict[str, str] | None:
    output_refs = manifest.get("output_refs")
    if not isinstance(output_refs, Mapping):
        return None
    resolved: dict[str, Path] | dict[str, str]
    resolved = {}
    for key, expected_name in _REQUIRED_OUTPUT_REFS.items():
        raw_ref = _exact_nonempty_string(output_refs.get(key))
        if raw_ref is None or not _safe_relative_ref(raw_ref, expected_name):
            return None
        if round_dir is None:
            resolved[key] = raw_ref
            continue
        try:
            supplied = (round_dir / raw_ref).absolute()
            if supplied.is_symlink():
                return None
            path = supplied.resolve(strict=True)
            if (
                path != supplied
                or not path.is_relative_to(round_dir)
                or not path.is_file()
            ):
                return None
        except (OSError, RuntimeError, ValueError):
            return None
        resolved[key] = path
    return resolved


def _safe_relative_ref(raw_ref: str, expected_name: str) -> bool:
    candidate = Path(raw_ref)
    return (
        not candidate.is_absolute()
        and not candidate.drive
        and all(part not in {"", ".", ".."} for part in candidate.parts)
        and candidate.name == expected_name
    )


def _strict_json(path: Path, *, expected: type[dict] | type[list]) -> Any | None:
    try:
        payload, _ = read_stable_json_artifact(
            path,
            parent=path.parent,
            expected_type=expected,
        )
    except ValueError:
        return None
    if not _json_value_is_finite(payload):
        return None
    return payload


def _json_value_is_finite(value: Any) -> bool:
    if value is None or isinstance(value, (bool, str, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_json_value_is_finite(item) for item in value)
    if isinstance(value, Mapping):
        return all(
            isinstance(key, str) and _json_value_is_finite(item)
            for key, item in value.items()
        )
    return False


def _exact_nonempty_string(value: Any) -> str | None:
    if type(value) is not str or not value or value != value.strip():
        return None
    return value


def _parse_explicit_timestamp(value: Any, *, context: str) -> datetime | None:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or _EXPLICIT_TIMEZONE_SUFFIX.search(value) is None
    ):
        return None
    try:
        return parse_iso_datetime_utc(value, context=context)
    except (TypeError, ValueError):
        return None


def _canonical_publication_created_at(value: Any) -> str | None:
    parsed = _parse_explicit_timestamp(
        value,
        context="promotion_qualification.recommendation.created_at",
    )
    return parsed.astimezone(timezone.utc).isoformat() if parsed else None


def _recommendation_has_exact_publication_identity(
    manifest: Mapping[str, Any],
    request: _QualificationRequest,
) -> bool:
    publication = manifest.get("control_plane_publication")
    if (
        type(publication) is not dict
        or frozenset(publication) != _CONTROL_PLANE_PUBLICATION_FIELDS
        or publication.get("schema_version")
        != "aats.phase6.control_plane_publication.v1"
        or type(publication.get("recommendations")) is not list
        or type(publication.get("active_decisions")) is not list
        or type(publication.get("evidence_bundle")) is not dict
        or not _json_value_is_finite(publication)
    ):
        return False

    expected_identity = {
        "recommendation_id": request.recommendation_id,
        "created_at": request.created_at,
        "family": request.family,
        "symbol": request.symbol,
        "timeframe": request.timeframe,
        "recommendation_type": request.recommendation_type,
        "target_parameter_set_id": request.target_parameter_set_id,
        "source_round_id": request.source_round_id,
        "confidence": request.confidence,
        "reason": request.reason,
        "evidence_bundle_ref": request.evidence_bundle_ref,
    }
    seen_ids: set[str] = set()
    producer_indexes: set[int] = set()
    matches = 0
    recommendations = publication["recommendations"]
    for entry in recommendations:
        if (
            type(entry) is not dict
            or frozenset(entry) != _PUBLICATION_RECOMMENDATION_FIELDS
        ):
            return False
        producer_index = entry.get("producer_index")
        recommendation_id = _exact_nonempty_string(
            entry.get("recommendation_id")
        )
        created_at = _canonical_publication_created_at(
            entry.get("created_at")
        )
        timeframe = normalize_timeframe_value(entry.get("timeframe"))
        if (
            type(producer_index) is not int
            or producer_index < 0
            or producer_index in producer_indexes
            or recommendation_id is None
            or recommendation_id in seen_ids
            or created_at is None
            or entry.get("created_at") != created_at
            or timeframe is None
            or entry.get("timeframe") != timeframe
            or _exact_nonempty_string(entry.get("family")) is None
            or _exact_nonempty_string(entry.get("symbol")) is None
            or entry.get("recommendation_type") not in VALID_REC_TYPES
            or entry.get("confidence") not in {"low", "medium", "high"}
            or type(entry.get("reason")) is not str
            or entry.get("evidence_bundle_ref") != manifest.get("round_id")
            or (
                entry.get("target_parameter_set_id") is not None
                and _exact_nonempty_string(
                    entry.get("target_parameter_set_id")
                )
                is None
            )
            or (
                entry.get("source_round_id") is not None
                and _exact_nonempty_string(entry.get("source_round_id"))
                is None
            )
        ):
            return False
        producer_indexes.add(producer_index)
        seen_ids.add(recommendation_id)
        actual_identity = {
            field: entry.get(field)
            for field in expected_identity
        }
        if actual_identity == expected_identity:
            matches += 1
    if producer_indexes != set(range(len(recommendations))):
        return False
    return matches == 1


def _canonical_round_started_at(
    snapshot: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> datetime | None:
    snapshot_time = _parse_explicit_timestamp(
        snapshot.get("started_at"),
        context="promotion_qualification.snapshot.started_at",
    )
    manifest_time = _parse_explicit_timestamp(
        manifest.get("started_at"),
        context="promotion_qualification.manifest.started_at",
    )
    if snapshot_time is None or manifest_time is None or snapshot_time != manifest_time:
        return None
    return snapshot_time


def _canonical_round_finished_at(
    snapshot: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> datetime | None:
    snapshot_time = _parse_explicit_timestamp(
        snapshot.get("finished_at"),
        context="promotion_qualification.snapshot.finished_at",
    )
    manifest_time = _parse_explicit_timestamp(
        manifest.get("finished_at"),
        context="promotion_qualification.manifest.finished_at",
    )
    if (
        snapshot_time is None
        or manifest_time is None
        or snapshot_time != manifest_time
        or snapshot_time > datetime.now(timezone.utc)
    ):
        return None
    return snapshot_time


def _failed(
    reason_code: str,
    recommendation: Mapping[str, Any],
) -> PromotionQualificationVerdict:
    return _verdict(
        required=True,
        eligible=False,
        reason_code=reason_code,
        recommendation=recommendation,
    )


def _verdict(
    *,
    required: bool,
    eligible: bool,
    reason_code: str,
    recommendation: Mapping[str, Any],
    qualified_round_id: str | None = None,
    qualified_finished_at: str | None = None,
    parameter_values_fingerprint: str | None = None,
) -> PromotionQualificationVerdict:
    evidence_bundle_ref = _exact_nonempty_string(
        recommendation.get("evidence_bundle_ref")
    )
    source_round_id = _exact_nonempty_string(recommendation.get("source_round_id"))
    return PromotionQualificationVerdict(
        required=required,
        eligible=eligible,
        reason_code=reason_code,
        evidence_bundle_ref=evidence_bundle_ref,
        source_round_id=source_round_id,
        qualified_round_id=qualified_round_id,
        detail=_DETAILS[reason_code],
        qualified_finished_at=qualified_finished_at,
        parameter_values_fingerprint=parameter_values_fingerprint,
    )
