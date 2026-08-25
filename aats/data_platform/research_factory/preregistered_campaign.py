"""Immutable preregistration contracts for new Research Factory campaigns."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aats.data_platform.governance._atomic_io import immutable_json_write
from aats.data_platform.research_factory.proposals import FactorDSLProposal
from aats.data_platform.research_factory.registry import factor_signature_from_expression

PREREGISTERED_CAMPAIGN_SCHEMA = "research_preregistered_campaign_v1"
PREREGISTERED_HYPOTHESIS_CARD_SCHEMA = "research_hypothesis_card_v1"
PREREGISTERED_PLAN_TYPE = "preregistered_hypothesis"
PREREGISTERED_PLAN_FORMAT_VERSION = 2
SELECTION_PROTOCOL = "train_valid_selection_test_holdout_v2"

_CAMPAIGN_KEYS = frozenset(
    {
        "schema_version",
        "campaign_id",
        "registered_at",
        "symbol",
        "timeframe",
        "start",
        "end",
        "dataset_version",
        "research_profile",
        "train_ratio",
        "valid_ratio",
        "test_ratio",
        "fee_bps",
        "slippage_bps",
        "funding_bps",
        "hypotheses",
    }
)
_CAMPAIGN_OPTIONAL_KEYS = frozenset({"max_factor_input_missing_ratio"})
_HYPOTHESIS_KEYS = frozenset(
    {
        "hypothesis_id",
        "mechanism",
        "hypothesis",
        "rationale",
        "falsification_condition",
        "capacity_assumption",
        "holding_period_bars",
        "factor_expression",
    }
)
_AUTHORIZATION_BOUNDARY = (
    "development research only; holdout sealed; no runtime parameter write, "
    "order submission, live deployment or funds authorization"
)
_SENSITIVE_MARKERS = (
    "api_key",
    "apikey",
    "connection_string",
    "database_url",
    "password",
    "private_key",
    "secret",
    "token",
)
_SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


@dataclass(frozen=True, slots=True)
class PreregisteredHypothesisSpec:
    """One economic hypothesis fixed before development results are observed."""

    hypothesis_id: str
    mechanism: str
    hypothesis: str
    rationale: str
    falsification_condition: str
    capacity_assumption: str
    holding_period_bars: int
    factor_expression: str
    factor_signature: str

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, Any],
        *,
        registered_at: datetime,
    ) -> "PreregisteredHypothesisSpec":
        _require_exact_keys(payload, _HYPOTHESIS_KEYS, "hypothesis")
        proposal = FactorDSLProposal.from_mapping(
            {
                "hypothesis": payload["hypothesis"],
                "factor_expression": payload["factor_expression"],
                "rationale": payload["rationale"],
            },
            created_by="preregistered_campaign",
            created_at=registered_at,
        )
        holding_period_bars = _positive_int(
            payload["holding_period_bars"],
            "holding_period_bars",
        )
        if holding_period_bars != 1:
            raise ValueError(
                "holding_period_bars_must_be_one_until_non_overlapping_returns_are_supported"
            )
        return cls(
            hypothesis_id=_safe_identifier(payload["hypothesis_id"], "hypothesis_id"),
            mechanism=_safe_research_text(payload["mechanism"], "mechanism"),
            hypothesis=proposal.hypothesis,
            rationale=proposal.rationale,
            falsification_condition=_safe_research_text(
                payload["falsification_condition"],
                "falsification_condition",
            ),
            capacity_assumption=_safe_research_text(
                payload["capacity_assumption"],
                "capacity_assumption",
            ),
            holding_period_bars=holding_period_bars,
            factor_expression=proposal.factor_expression,
            factor_signature=factor_signature_from_expression(
                proposal.factor_expression
            ),
        )

    def proposal_payload(self) -> dict[str, Any]:
        return {
            "hypothesis": self.hypothesis,
            "factor_expression": self.factor_expression,
            "rationale": self.rationale,
        }

    def card_payload(self, *, campaign_id: str) -> dict[str, Any]:
        return {
            "schema_version": PREREGISTERED_HYPOTHESIS_CARD_SCHEMA,
            "campaign_id": campaign_id,
            "hypothesis_id": self.hypothesis_id,
            "mechanism": self.mechanism,
            "hypothesis": self.hypothesis,
            "rationale": self.rationale,
            "falsification_condition": self.falsification_condition,
            "capacity_assumption": self.capacity_assumption,
            "holding_period_bars": self.holding_period_bars,
            "factor_expression": self.factor_expression,
            "factor_signature": self.factor_signature,
            "authorization_boundary": _AUTHORIZATION_BOUNDARY,
        }


@dataclass(frozen=True, slots=True)
class PreregisteredCampaignSpec:
    """Strict shared context plus the complete preregistered trial family."""

    campaign_id: str
    registered_at: datetime
    symbol: str
    timeframe: str
    start: datetime
    end: datetime
    dataset_version: str
    research_profile: str
    train_ratio: float
    valid_ratio: float
    test_ratio: float
    fee_bps: float
    slippage_bps: float
    funding_bps: float
    max_factor_input_missing_ratio: float | None
    hypotheses: tuple[PreregisteredHypothesisSpec, ...]
    source_sha256: str

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, Any],
        *,
        source_sha256: str,
    ) -> "PreregisteredCampaignSpec":
        _require_required_and_optional_keys(
            payload,
            _CAMPAIGN_KEYS,
            _CAMPAIGN_OPTIONAL_KEYS,
            "campaign",
        )
        if payload["schema_version"] != PREREGISTERED_CAMPAIGN_SCHEMA:
            raise ValueError("preregistered_campaign_schema_mismatch")
        registered_at = _datetime(payload["registered_at"], "registered_at")
        start = _datetime(payload["start"], "start")
        end = _datetime(payload["end"], "end")
        if end <= start:
            raise ValueError("campaign_end_must_be_after_start")
        hypotheses_payload = payload["hypotheses"]
        if (
            isinstance(hypotheses_payload, str | bytes | bytearray)
            or not isinstance(hypotheses_payload, Sequence)
            or len(hypotheses_payload) < 3
        ):
            raise ValueError("campaign_requires_at_least_three_hypotheses")
        hypotheses = tuple(
            PreregisteredHypothesisSpec.from_mapping(
                item,
                registered_at=registered_at,
            )
            for item in hypotheses_payload
        )
        _require_unique(
            [item.hypothesis_id for item in hypotheses],
            "duplicate_hypothesis_id",
        )
        _require_unique(
            [item.factor_signature for item in hypotheses],
            "duplicate_factor_signature",
        )
        train_ratio = _ratio(payload["train_ratio"], "train_ratio")
        valid_ratio = _ratio(payload["valid_ratio"], "valid_ratio")
        test_ratio = _ratio(payload["test_ratio"], "test_ratio")
        if not math.isclose(
            train_ratio + valid_ratio + test_ratio,
            1.0,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError("campaign_segment_ratios_must_sum_to_one")
        research_profile = _text(payload["research_profile"], "research_profile")
        if research_profile != "real_factor_research":
            raise ValueError("campaign_research_profile_must_be_real_factor_research")
        return cls(
            campaign_id=_safe_identifier(payload["campaign_id"], "campaign_id"),
            registered_at=registered_at,
            symbol=_text(payload["symbol"], "symbol").upper(),
            timeframe=_text(payload["timeframe"], "timeframe"),
            start=start,
            end=end,
            dataset_version=_text(payload["dataset_version"], "dataset_version"),
            research_profile=research_profile,
            train_ratio=train_ratio,
            valid_ratio=valid_ratio,
            test_ratio=test_ratio,
            fee_bps=_non_negative_float(payload["fee_bps"], "fee_bps"),
            slippage_bps=_non_negative_float(
                payload["slippage_bps"],
                "slippage_bps",
            ),
            funding_bps=_finite_float(payload["funding_bps"], "funding_bps"),
            max_factor_input_missing_ratio=(
                _inclusive_ratio(
                    payload["max_factor_input_missing_ratio"],
                    "max_factor_input_missing_ratio",
                )
                if "max_factor_input_missing_ratio" in payload
                else None
            ),
            hypotheses=hypotheses,
            source_sha256=_sha256_text(source_sha256, "source_sha256"),
        )

    def manifest_payload(self) -> dict[str, Any]:
        payload = {
            "schema_version": PREREGISTERED_CAMPAIGN_SCHEMA,
            "campaign_id": self.campaign_id,
            "registered_at": self.registered_at.isoformat(),
            "source_config_sha256": self.source_sha256,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "dataset_version": self.dataset_version,
            "research_profile": self.research_profile,
            "train_ratio": self.train_ratio,
            "valid_ratio": self.valid_ratio,
            "test_ratio": self.test_ratio,
            "fee_bps": self.fee_bps,
            "slippage_bps": self.slippage_bps,
            "funding_bps": self.funding_bps,
            "hypothesis_ids": [item.hypothesis_id for item in self.hypotheses],
            "factor_signatures": [item.factor_signature for item in self.hypotheses],
            "holdout_status": "sealed_not_evaluated",
            "capital_eligible": False,
            "authorization_boundary": _AUTHORIZATION_BOUNDARY,
        }
        if self.max_factor_input_missing_ratio is not None:
            payload["max_factor_input_missing_ratio"] = self.max_factor_input_missing_ratio
        return payload


def load_preregistered_campaign(path: str | Path) -> PreregisteredCampaignSpec:
    """Load a strict, version-controlled preregistration config."""
    config_path = Path(path)
    raw = config_path.read_bytes()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("preregistered_campaign_must_be_valid_json") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("preregistered_campaign_must_be_object")
    return PreregisteredCampaignSpec.from_mapping(
        payload,
        source_sha256=hashlib.sha256(raw).hexdigest(),
    )


def register_preregistered_campaign(
    spec: PreregisteredCampaignSpec,
    *,
    artifact_root: str | Path,
) -> dict[str, Any]:
    """Write deterministic preregistration artifacts without database access."""
    if not isinstance(spec, PreregisteredCampaignSpec):
        raise ValueError("spec_must_be_preregistered_campaign")
    root = _research_artifact_root(artifact_root)
    campaign_root = root / "preregistered_campaigns" / spec.campaign_id
    manifest_path = campaign_root / "campaign_manifest.json"
    manifest = spec.manifest_payload()
    manifest_sha = _encoded_digest(manifest)

    writes: list[tuple[Path, Mapping[str, Any]]] = [(manifest_path, manifest)]
    plan_records: list[dict[str, Any]] = []
    for hypothesis in spec.hypotheses:
        proposal_path = campaign_root / "proposals" / f"{hypothesis.hypothesis_id}.json"
        card_path = campaign_root / "hypothesis_cards" / f"{hypothesis.hypothesis_id}.json"
        proposal = hypothesis.proposal_payload()
        card = hypothesis.card_payload(campaign_id=spec.campaign_id)
        proposal_sha = _encoded_digest(proposal)
        card_sha = _encoded_digest(card)
        identity = {
            "campaign_manifest_sha256": manifest_sha,
            "proposal_sha256": proposal_sha,
            "hypothesis_card_sha256": card_sha,
            "selection_protocol_version": SELECTION_PROTOCOL,
        }
        plan_id = f"v2hyp_{_stable_hash(identity)[:24]}"
        plan_path = campaign_root / "plans" / f"{plan_id}.json"
        plan = {
            "format_version": PREREGISTERED_PLAN_FORMAT_VERSION,
            "plan_type": PREREGISTERED_PLAN_TYPE,
            "plan_id": plan_id,
            "created_at": spec.registered_at.isoformat(),
            "source_experiment_id": (
                f"prereg_{spec.campaign_id}_{hypothesis.hypothesis_id}"
            ),
            "campaign_manifest_ref": manifest_path.relative_to(root).as_posix(),
            "campaign_manifest_sha256": manifest_sha,
            "proposal_ref": proposal_path.relative_to(root).as_posix(),
            "proposal_sha256": proposal_sha,
            "hypothesis_card_ref": card_path.relative_to(root).as_posix(),
            "hypothesis_card_sha256": card_sha,
            "symbol": spec.symbol,
            "timeframe": spec.timeframe,
            "start": spec.start.isoformat(),
            "end": spec.end.isoformat(),
            "dataset_version": spec.dataset_version,
            "factor_expression": hypothesis.factor_expression,
            "label_horizon_bars": hypothesis.holding_period_bars,
            "fee_bps": spec.fee_bps,
            "slippage_bps": spec.slippage_bps,
            "funding_bps": spec.funding_bps,
            "research_profile": spec.research_profile,
            "selection_protocol_version": SELECTION_PROTOCOL,
            "train_ratio": spec.train_ratio,
            "valid_ratio": spec.valid_ratio,
            "test_ratio": spec.test_ratio,
            "status": "planned_not_run",
            "reason_codes": ["preregistered_new_hypothesis"],
            "authorization_boundary": _AUTHORIZATION_BOUNDARY,
        }
        if spec.max_factor_input_missing_ratio is not None:
            plan["max_factor_input_missing_ratio"] = spec.max_factor_input_missing_ratio
        writes.extend(((proposal_path, proposal), (card_path, card), (plan_path, plan)))
        plan_records.append(
            {
                "hypothesis_id": hypothesis.hypothesis_id,
                "plan_id": plan_id,
                "plan_ref": plan_path.relative_to(root).as_posix(),
                "plan_sha256": _encoded_digest(plan),
            }
        )

    evidence_path = campaign_root / "registration_evidence.json"
    evidence = {
        "schema_version": "research_preregistered_campaign_evidence_v1",
        "campaign_id": spec.campaign_id,
        "registered_at": spec.registered_at.isoformat(),
        "campaign_manifest_ref": manifest_path.relative_to(root).as_posix(),
        "campaign_manifest_sha256": manifest_sha,
        "plan_count": len(plan_records),
        "plans": plan_records,
        "holdout_status": "sealed_not_evaluated",
        "database_accessed": False,
        "runtime_parameters_written": False,
        "orders_submitted": False,
        "capital_eligible": False,
        "authorization_boundary": _AUTHORIZATION_BOUNDARY,
    }
    writes.append((evidence_path, evidence))
    _preflight_writes(writes)
    digests = {path: _write_once_or_verify(path, payload) for path, payload in writes}
    return {
        "campaign_id": spec.campaign_id,
        "campaign_root": campaign_root.as_posix(),
        "plan_root": (campaign_root / "plans").as_posix(),
        "plan_count": len(plan_records),
        "registration_evidence": evidence_path.as_posix(),
        "registration_evidence_sha256": digests[evidence_path],
        "holdout_accessed": False,
        "database_accessed": False,
        "runtime_parameters_written": False,
    }


def _preflight_writes(writes: Sequence[tuple[Path, Mapping[str, Any]]]) -> None:
    for path, payload in writes:
        if path.exists() and path.read_bytes() != _encoded(payload):
            raise FileExistsError(f"preregistered_artifact_content_mismatch:{path.as_posix()}")


def _write_once_or_verify(path: Path, payload: Mapping[str, Any]) -> str:
    encoded = _encoded(payload)
    if path.exists():
        if path.read_bytes() != encoded:
            raise FileExistsError(f"preregistered_artifact_content_mismatch:{path.as_posix()}")
        return hashlib.sha256(encoded).hexdigest()
    return immutable_json_write(payload, path)


def _encoded(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _encoded_digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_encoded(payload)).hexdigest()


def _stable_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _research_artifact_root(value: str | Path) -> Path:
    path = Path(value)
    if ".." in path.parts:
        raise ValueError("artifact_root_must_not_contain_path_traversal")
    parts = path.parts
    if not any(
        parts[index] == "artifacts" and parts[index + 1] == "research"
        for index in range(len(parts) - 1)
    ):
        raise ValueError("artifact_root_must_be_under_artifacts_research")
    return path


def _require_exact_keys(
    payload: Mapping[str, Any],
    expected: frozenset[str],
    label: str,
) -> None:
    if not isinstance(payload, Mapping):
        raise ValueError(f"{label}_must_be_object")
    keys = set(payload)
    if keys != expected:
        raise ValueError(
            f"{label}_keys_mismatch:missing={sorted(expected - keys)}:"
            f"extra={sorted(keys - expected)}"
        )


def _require_required_and_optional_keys(
    payload: Mapping[str, Any],
    required: frozenset[str],
    optional: frozenset[str],
    label: str,
) -> None:
    if not isinstance(payload, Mapping):
        raise ValueError(f"{label}_must_be_object")
    keys = set(payload)
    missing = required - keys
    extra = keys - required - optional
    if missing or extra:
        raise ValueError(
            f"{label}_keys_mismatch:missing={sorted(missing)}:extra={sorted(extra)}"
        )


def _require_unique(values: Sequence[str], reason: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(reason)


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name}_must_be_non_empty_text")
    return value.strip()


def _safe_identifier(value: Any, field_name: str) -> str:
    normalized = _text(value, field_name)
    if _SAFE_IDENTIFIER.fullmatch(normalized) is None:
        raise ValueError(f"{field_name}_must_be_safe_identifier")
    return normalized


def _safe_research_text(value: Any, field_name: str) -> str:
    normalized = _text(value, field_name)
    lowered = normalized.lower()
    if any(marker in lowered for marker in _SENSITIVE_MARKERS):
        raise ValueError(f"{field_name}_must_not_contain_sensitive_material")
    return normalized


def _positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name}_must_be_positive_integer")
    return value


def _finite_float(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{field_name}_must_be_number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field_name}_must_be_finite")
    return result


def _non_negative_float(value: Any, field_name: str) -> float:
    result = _finite_float(value, field_name)
    if result < 0:
        raise ValueError(f"{field_name}_must_be_non_negative")
    return result


def _ratio(value: Any, field_name: str) -> float:
    result = _finite_float(value, field_name)
    if not 0.0 < result < 1.0:
        raise ValueError(f"{field_name}_must_be_between_zero_and_one")
    return result


def _inclusive_ratio(value: Any, field_name: str) -> float:
    result = _finite_float(value, field_name)
    if not 0.0 <= result <= 1.0:
        raise ValueError(f"{field_name}_must_be_between_zero_and_one_inclusive")
    return result


def _datetime(value: Any, field_name: str) -> datetime:
    raw = _text(value, field_name).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name}_must_be_timezone_aware")
    return parsed.astimezone(UTC)


def _sha256_text(value: Any, field_name: str) -> str:
    normalized = _text(value, field_name)
    if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
        raise ValueError(f"{field_name}_must_be_sha256")
    return normalized
