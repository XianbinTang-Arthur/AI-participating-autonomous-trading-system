"""Fail-closed control-plane guard for promotion-capable recommendations."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from aats.data_platform.governance._db_util import VALID_REC_TYPES
from aats.data_platform.governance._time_util import parse_iso_datetime_utc


_BLOCK_CODE = "promotion_qualification_blocked"
_APPLY_TYPE = "parameter_upgrade"
_AUTHORIZATION_ISSUER = object()
_AUTHORIZATION_TTL = timedelta(minutes=5)
_EXPLICIT_TIMEZONE_SUFFIX = re.compile(r"(?:Z|[+-][0-9]{2}:[0-9]{2})$")
_AUTHORIZATION_IDENTITY_FIELDS = (
    "recommendation_id",
    "family",
    "symbol",
    "timeframe",
    "recommendation_type",
    "target_parameter_set_id",
    "source_round_id",
    "evidence_bundle_ref",
)
log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _InvalidQualificationVerdict:
    """Stable fail-closed verdict for evaluator exceptions or contract drift."""

    required: bool = True
    eligible: bool = False
    reason_code: str = "promotion_qualification_invalid"
    detail: str = "精确证据资格判定结构无效或执行失败，已失败关闭。"

    def to_dict(self) -> dict[str, Any]:
        return {
            "required": self.required,
            "eligible": self.eligible,
            "reason_code": self.reason_code,
            "evidence_bundle_ref": None,
            "source_round_id": None,
            "qualified_round_id": None,
            "qualified_finished_at": None,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class PromotionAuthorization:
    """Opaque, process-local capability for one qualified recommendation identity.

    The object is intentionally not serializable and its issuer marker is
    validated by identity.  It exists only to carry a pre-write qualification
    across the synchronous approve-and-release call without querying mutable
    evidence again after approval has already been persisted.
    """

    project_root: str
    recommendation_identity: tuple[tuple[str, Any], ...]
    issued_at_utc: datetime
    expires_at_utc: datetime
    _verdict: Any = field(repr=False, compare=False)
    _issuer: object = field(repr=False, compare=False)


def _verdict_contract_is_valid(
    verdict: Any,
    recommendation: dict[str, Any],
) -> bool:
    """Require a self-consistent verdict bound to the recommendation semantics."""

    required = getattr(verdict, "required", None)
    eligible = getattr(verdict, "eligible", None)
    reason_code = getattr(verdict, "reason_code", None)
    detail = getattr(verdict, "detail", None)
    serializer = getattr(verdict, "to_dict", None)
    if (
        type(required) is not bool
        or type(eligible) is not bool
        or not isinstance(reason_code, str)
        or not reason_code
        or not isinstance(detail, str)
        or not detail
        or not callable(serializer)
    ):
        return False
    try:
        payload = serializer()
    except Exception:
        return False
    contract_valid = (
        isinstance(payload, dict)
        and payload.get("required") is required
        and payload.get("eligible") is eligible
        and payload.get("reason_code") == reason_code
        and payload.get("detail") == detail
    )
    if not contract_valid:
        return False

    recommendation_type = recommendation.get("recommendation_type")
    target = recommendation.get("target_parameter_set_id")
    if recommendation_type is not None and (
        not isinstance(recommendation_type, str)
        or not recommendation_type.strip()
        or recommendation_type not in VALID_REC_TYPES
    ):
        return False
    if target is not None and (
        not isinstance(target, str) or not target.strip()
    ):
        return False
    if recommendation_type not in {None, _APPLY_TYPE} and target is not None:
        return False

    apply_capable = recommendation_type == _APPLY_TYPE or target is not None
    if apply_capable:
        if required is not True:
            return False
        # Only an exactly bound, qualified verdict may authorize a forward
        # mutation.  Merely returning ``eligible=True`` with a plausible shape
        # is insufficient: every control-plane entry point must bind the
        # verdict to this recommendation's immutable evidence references.
        if eligible is True:
            evidence_bundle_ref = recommendation.get("evidence_bundle_ref")
            source_round_id = recommendation.get("source_round_id")
            return bool(
                reason_code == "qualified"
                and isinstance(evidence_bundle_ref, str)
                and bool(evidence_bundle_ref.strip())
                and isinstance(source_round_id, str)
                and bool(source_round_id.strip())
                and payload.get("evidence_bundle_ref") == evidence_bundle_ref
                and payload.get("source_round_id") == source_round_id
                and payload.get("qualified_round_id") == evidence_bundle_ref
                and isinstance(payload.get("qualified_finished_at"), str)
                and bool(payload["qualified_finished_at"].strip())
            )
        return True
    return required is False and eligible is True and reason_code == "not_required"


def validate_promotion_qualification_verdict(
    verdict: Any,
    recommendation: dict[str, Any],
) -> Any:
    """Return a guard-valid verdict or the shared fail-closed verdict.

    Gate, approval, release and apply must use this same semantic contract so a
    structurally plausible ``required=False`` value cannot be interpreted
    differently by different control-plane layers.
    """

    if not isinstance(recommendation, dict):
        return _InvalidQualificationVerdict()
    if not _verdict_contract_is_valid(verdict, recommendation):
        return _InvalidQualificationVerdict()
    return verdict


def _canonical_project_root(project_root: Path) -> str | None:
    try:
        return str(project_root.resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical_aware_datetime(value: Any) -> datetime | None:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(timezone.utc)


def _qualified_finished_at_utc(value: Any) -> datetime | None:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or _EXPLICIT_TIMEZONE_SUFFIX.search(value) is None
    ):
        return None
    try:
        return parse_iso_datetime_utc(
            value,
            context="promotion_guard.qualified_finished_at",
        )
    except (TypeError, ValueError):
        return None


def _recommendation_identity(
    recommendation: dict[str, Any],
) -> tuple[tuple[str, Any], ...]:
    return tuple(
        (field_name, recommendation.get(field_name))
        for field_name in _AUTHORIZATION_IDENTITY_FIELDS
    )


def _authorization_invalid_verdict() -> _InvalidQualificationVerdict:
    return _InvalidQualificationVerdict(
        reason_code="promotion_authorization_invalid",
        detail="组合审批发布授权无效、已漂移或不属于当前 recommendation，已失败关闭。",
    )


def _not_apply_capable_verdict() -> _InvalidQualificationVerdict:
    return _InvalidQualificationVerdict(
        reason_code="recommendation_type_not_apply_capable",
        detail="只有 recommendation_type=parameter_upgrade 的建议可以创建发布或应用参数。",
    )


class PromotionQualificationBlockedError(RuntimeError):
    """Raised before a recommendation can advance without exact evidence."""

    def __init__(self, recommendation_id: str, verdict: Any) -> None:
        self.recommendation_id = recommendation_id
        self.verdict = verdict
        reason_code = str(getattr(verdict, "reason_code", "qualification_failed"))
        detail = str(getattr(verdict, "detail", "promotion qualification failed"))
        super().__init__(f"{reason_code}: {detail}")

    def to_dict(self) -> dict[str, Any]:
        qualification = self.verdict.to_dict()
        return {
            "ok": False,
            "code": _BLOCK_CODE,
            "message": "recommendation 不具备前向发布资格",
            "recommendation_id": self.recommendation_id,
            "promotion_qualification": qualification,
        }


def require_promotion_qualification(
    project_root: Path,
    recommendation: dict[str, Any],
) -> Any:
    """Return the exact-reference verdict or raise before any forward mutation."""
    from aats.data_platform.decision_system.promotion_qualification import (
        evaluate_promotion_qualification,
    )

    try:
        verdict = evaluate_promotion_qualification(
            project_root=project_root,
            recommendation=recommendation,
        )
    except Exception as exc:
        log.warning(
            "promotion qualification evaluator failed closed for %s (%s)",
            recommendation.get("recommendation_id"),
            type(exc).__name__,
        )
        verdict = _InvalidQualificationVerdict()
    verdict = validate_promotion_qualification_verdict(verdict, recommendation)
    if verdict.required and not verdict.eligible:
        raise PromotionQualificationBlockedError(
            str(recommendation.get("recommendation_id") or ""),
            verdict,
        )
    return verdict


def issue_promotion_authorization(
    project_root: Path,
    recommendation: dict[str, Any],
) -> PromotionAuthorization:
    """Issue an opaque authorization before any composite-flow write."""

    if recommendation.get("recommendation_type") != _APPLY_TYPE:
        raise PromotionQualificationBlockedError(
            str(recommendation.get("recommendation_id") or ""),
            _not_apply_capable_verdict(),
        )
    verdict = validate_promotion_qualification_verdict(
        require_promotion_qualification(project_root, recommendation),
        recommendation,
    )
    canonical_root = _canonical_project_root(project_root)
    try:
        payload = verdict.to_dict()
    except Exception:
        payload = {}
    qualified_finished_at = _qualified_finished_at_utc(
        payload.get("qualified_finished_at")
    )
    issued_at = _utc_now()
    from aats.data_platform.decision_system.promotion_qualification import (
        PROMOTION_MAX_EVIDENCE_AGE,
    )

    expires_at = (
        min(
            issued_at + _AUTHORIZATION_TTL,
            qualified_finished_at + PROMOTION_MAX_EVIDENCE_AGE,
        )
        if qualified_finished_at is not None
        else issued_at
    )
    if (
        canonical_root is None
        or verdict.required is not True
        or verdict.eligible is not True
        or verdict.reason_code != "qualified"
        or payload.get("evidence_bundle_ref")
        != recommendation.get("evidence_bundle_ref")
        or payload.get("source_round_id") != recommendation.get("source_round_id")
        or payload.get("qualified_round_id")
        != recommendation.get("evidence_bundle_ref")
        or qualified_finished_at is None
        or qualified_finished_at > issued_at
        or expires_at <= issued_at
    ):
        raise PromotionQualificationBlockedError(
            str(recommendation.get("recommendation_id") or ""),
            _authorization_invalid_verdict(),
        )
    return PromotionAuthorization(
        project_root=canonical_root,
        recommendation_identity=_recommendation_identity(recommendation),
        issued_at_utc=issued_at,
        expires_at_utc=expires_at,
        _verdict=verdict,
        _issuer=_AUTHORIZATION_ISSUER,
    )


def require_promotion_authorization(
    project_root: Path,
    recommendation: dict[str, Any],
    authorization: PromotionAuthorization,
) -> Any:
    """Validate a process-local authorization against the complete identity."""

    canonical_root = _canonical_project_root(project_root)
    issued_at = _canonical_aware_datetime(
        getattr(authorization, "issued_at_utc", None)
    )
    expires_at = _canonical_aware_datetime(
        getattr(authorization, "expires_at_utc", None)
    )
    now = _utc_now()
    valid = (
        type(authorization) is PromotionAuthorization
        and authorization._issuer is _AUTHORIZATION_ISSUER
        and canonical_root is not None
        and authorization.project_root == canonical_root
        and authorization.recommendation_identity
        == _recommendation_identity(recommendation)
        and issued_at is not None
        and expires_at is not None
        and issued_at <= now < expires_at
        and expires_at <= issued_at + _AUTHORIZATION_TTL
    )
    verdict = (
        validate_promotion_qualification_verdict(
            authorization._verdict,
            recommendation,
        )
        if valid
        else _authorization_invalid_verdict()
    )
    payload = verdict.to_dict()
    qualified_finished_at = _qualified_finished_at_utc(
        payload.get("qualified_finished_at")
    )
    from aats.data_platform.decision_system.promotion_qualification import (
        PROMOTION_MAX_EVIDENCE_AGE,
    )

    valid = bool(
        valid
        and verdict.required is True
        and verdict.eligible is True
        and verdict.reason_code == "qualified"
        and payload.get("evidence_bundle_ref")
        == recommendation.get("evidence_bundle_ref")
        and payload.get("source_round_id") == recommendation.get("source_round_id")
        and payload.get("qualified_round_id")
        == recommendation.get("evidence_bundle_ref")
        and qualified_finished_at is not None
        and qualified_finished_at <= issued_at
        and expires_at <= qualified_finished_at + PROMOTION_MAX_EVIDENCE_AGE
    )
    if not valid:
        raise PromotionQualificationBlockedError(
            str(recommendation.get("recommendation_id") or ""),
            _authorization_invalid_verdict(),
        )
    return verdict


def promotion_authorization_failure(
    project_root: Path,
    recommendation: dict[str, Any],
    authorization: PromotionAuthorization,
) -> dict[str, Any] | None:
    """Return a structured failure for invalid composite-flow authorization."""

    try:
        require_promotion_authorization(
            project_root,
            recommendation,
            authorization,
        )
    except PromotionQualificationBlockedError as exc:
        return exc.to_dict()
    return None


def promotion_qualification_failure(
    project_root: Path,
    recommendation: dict[str, Any],
) -> dict[str, Any] | None:
    """Return a structured failure for dict-returning release/apply helpers.

    Approval may record non-apply governance recommendations, but release/apply
    must never interpret ``keep_active``/``pause``/legacy unknown types merely
    because a malformed row happens to carry ``target_parameter_set_id``.
    """
    if recommendation.get("recommendation_type") != _APPLY_TYPE:
        return PromotionQualificationBlockedError(
            str(recommendation.get("recommendation_id") or ""),
            _not_apply_capable_verdict(),
        ).to_dict()
    try:
        require_promotion_qualification(project_root, recommendation)
    except PromotionQualificationBlockedError as exc:
        return exc.to_dict()
    return None
