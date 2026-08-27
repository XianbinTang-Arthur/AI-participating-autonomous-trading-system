"""Exact-round promotion qualification for apply-capable recommendations.

The verifier is intentionally read-only and fail-closed.  A reachable
governance database is authoritative; project-local files are used only when
the database itself is unavailable.  No latest-round lookup is permitted.
"""

from __future__ import annotations

import json
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
from aats.data_platform.governance._db_util import (
    VALID_REC_TYPES,
    has_explicit_governance_db_configuration,
    try_governance_db,
)
from aats.data_platform.governance.decision_rounds_db import (
    db_load_decision_round_snapshots,
)
from aats.data_platform.governance._time_util import parse_iso_datetime_utc

_ROUND_ID = re.compile(r"^[0-9]{8}_[0-9]{6}_[0-9a-f]{8}$")
_DECISION_ROUNDS_ROOT = Path("artifacts") / "decision_rounds"
_REQUIRED_OUTPUT_REFS = {
    "evidence_summary": "evidence_summary.json",
    "upgrade_candidates": "parameter_upgrade_candidates.json",
}
PROMOTION_MAX_EVIDENCE_AGE = timedelta(hours=168)
_EXPLICIT_TIMEZONE_SUFFIX = re.compile(r"(?:Z|[+-][0-9]{2}:[0-9]{2})$")

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
    "promotion_round_finished_at_invalid": "精确 round 缺少可信且一致的完成时间。",
    "promotion_round_stale": "精确 round 已超过 168 小时资格有效期。",
    "promotion_round_scope_mismatch": "Manifest scope 与 recommendation 身份不一致。",
    "promotion_round_output_ref_invalid": "Manifest 的关键输出引用无效。",
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "required": self.required,
            "eligible": self.eligible,
            "reason_code": self.reason_code,
            "evidence_bundle_ref": self.evidence_bundle_ref,
            "source_round_id": self.source_round_id,
            "qualified_round_id": self.qualified_round_id,
            "qualified_finished_at": self.qualified_finished_at,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class _QualificationRequest:
    recommendation_id: str
    evidence_bundle_ref: str
    source_round_id: str
    target_parameter_set_id: str
    family: str
    timeframe: str
    symbol: str


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
                _evaluate_snapshot(item, recommendation, resolution.snapshot)
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
    return _QualificationRequest(
        recommendation_id=recommendation_id,
        evidence_bundle_ref=evidence_bundle_ref,
        source_round_id=source_round_id,
        target_parameter_set_id=target_parameter_set_id,
        family=family,
        timeframe=timeframe,
        symbol=symbol,
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
    return _SnapshotResolution(
        {
            "round_id": round_id,
            "started_at": manifest.get("started_at"),
            "finished_at": manifest.get("finished_at"),
            "manifest": manifest,
            "evidence_bundle_summary": evidence,
            "parameter_upgrade_candidates": candidates,
        }
    )


def _evaluate_snapshot(
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

    return _verdict(
        required=True,
        eligible=True,
        reason_code="qualified",
        recommendation=recommendation,
        qualified_round_id=request.evidence_bundle_ref,
        qualified_finished_at=finished_at.isoformat(),
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
    )


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
            path = (round_dir / raw_ref).resolve(strict=True)
            if not path.is_relative_to(round_dir) or not path.is_file():
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
        payload = json.loads(
            path.read_bytes().decode("utf-8"),
            parse_constant=lambda token: _raise_json_constant(token),
        )
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if type(payload) is not expected or not _json_value_is_finite(payload):
        return None
    return payload


def _raise_json_constant(token: str) -> None:
    raise ValueError(f"non-finite JSON constant: {token}")


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


def _canonical_round_finished_at(
    snapshot: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> datetime | None:
    raw_snapshot = snapshot.get("finished_at")
    raw_manifest = manifest.get("finished_at")
    for raw in (raw_snapshot, raw_manifest):
        if (
            not isinstance(raw, str)
            or raw != raw.strip()
            or _EXPLICIT_TIMEZONE_SUFFIX.search(raw) is None
        ):
            return None
    try:
        snapshot_time = parse_iso_datetime_utc(
            raw_snapshot,
            context="promotion_qualification.snapshot.finished_at",
        )
        manifest_time = parse_iso_datetime_utc(
            raw_manifest,
            context="promotion_qualification.manifest.finished_at",
        )
    except (TypeError, ValueError):
        return None
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
    )
