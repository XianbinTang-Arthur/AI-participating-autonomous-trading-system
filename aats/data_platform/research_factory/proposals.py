"""Proposal-only Factor DSL contracts for Research Factory experiments."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aats.data_platform.research_factory.features.expressions import parse_factor_expression
from aats.data_platform.research_factory.paths import require_research_artifact_json_file

FACTOR_DSL_PROPOSAL_SCHEMA_VERSION = "research_factor_dsl_proposal_v1"
PROPOSAL_ONLY_PAYLOAD_KEYS = frozenset({"hypothesis", "factor_expression", "rationale"})
FORBIDDEN_PROPOSAL_TERMS = (
    "active_parameter",
    "active_parameters",
    "auto_apply",
    "ccxt",
    "compile(",
    "credential",
    "direct_apply",
    "eval(",
    "exec(",
    "httpx",
    "import ",
    "live_order",
    "okx_write",
    "open(",
    "operator_write",
    "pathlib",
    "pickle",
    "place_order",
    "production_config",
    "requests",
    "runtime_config",
    "runtime_mutation",
    "secret",
    "shutil",
    "socket",
    "subprocess",
    "submit_order",
    "token",
    "__import__",
    "```",
)


@dataclass(frozen=True, slots=True)
class FactorDSLProposal:
    """A research-only proposal that can only carry a hypothesis, Factor DSL, and rationale."""

    hypothesis: str
    factor_expression: str
    rationale: str
    proposal_id: str | None = None
    created_by: str = "proposal_only_factor_dsl"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    schema_version: str = FACTOR_DSL_PROPOSAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        hypothesis = _require_proposal_text(self.hypothesis, "hypothesis")
        rationale = _require_proposal_text(self.rationale, "rationale")
        parsed = parse_factor_expression(_require_proposal_text(self.factor_expression, "factor_expression"))
        _reject_forbidden_proposal_text(parsed.expression, "factor_expression")
        proposal_id = self.proposal_id or _default_proposal_id(
            hypothesis=hypothesis,
            factor_expression=parsed.expression,
            rationale=rationale,
        )
        object.__setattr__(self, "proposal_id", _require_safe_identifier(proposal_id, "proposal_id"))
        object.__setattr__(self, "hypothesis", hypothesis)
        object.__setattr__(self, "factor_expression", parsed.expression)
        object.__setattr__(self, "rationale", rationale)
        object.__setattr__(self, "created_by", _require_safe_identifier(self.created_by, "created_by"))
        _require_timezone_aware_datetime(self.created_at, "created_at")
        if self.schema_version != FACTOR_DSL_PROPOSAL_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {FACTOR_DSL_PROPOSAL_SCHEMA_VERSION!r}")

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, Any],
        *,
        created_by: str = "proposal_only_factor_dsl",
        created_at: datetime | None = None,
    ) -> "FactorDSLProposal":
        """Build a proposal from the strict external proposal-only JSON shape."""
        if not isinstance(payload, Mapping):
            raise ValueError("factor DSL proposal payload must be a JSON object")
        keys = set(payload)
        if keys != PROPOSAL_ONLY_PAYLOAD_KEYS:
            missing = sorted(PROPOSAL_ONLY_PAYLOAD_KEYS - keys)
            extra = sorted(keys - PROPOSAL_ONLY_PAYLOAD_KEYS)
            details: list[str] = []
            if missing:
                details.append(f"missing: {', '.join(missing)}")
            if extra:
                details.append(f"extra: {', '.join(extra)}")
            rendered = "; ".join(details)
            raise ValueError(f"factor DSL proposal must contain only hypothesis, factor_expression, rationale ({rendered})")
        return cls(
            hypothesis=payload["hypothesis"],
            factor_expression=payload["factor_expression"],
            rationale=payload["rationale"],
            created_by=created_by,
            created_at=created_at or datetime.now(UTC),
        )


def load_factor_dsl_proposal(
    path: str | Path,
    *,
    research_root: str | Path | None = None,
    created_by: str = "proposal_only_factor_dsl",
    created_at: datetime | None = None,
) -> FactorDSLProposal:
    """Load and validate a strict proposal-only JSON artifact."""
    proposal_path = require_research_artifact_json_file(
        path,
        "factor_proposal_path",
        research_root=research_root,
    )
    try:
        payload = json.loads(proposal_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("factor DSL proposal must be valid JSON") from exc
    return FactorDSLProposal.from_mapping(payload, created_by=created_by, created_at=created_at)


def _default_proposal_id(
    *,
    hypothesis: str,
    factor_expression: str,
    rationale: str,
) -> str:
    payload = json.dumps(
        {
            "factor_expression": factor_expression,
            "hypothesis": hypothesis,
            "rationale": rationale,
        },
        ensure_ascii=True,
        sort_keys=True,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
    return f"factor_proposal_{digest}"


def _require_proposal_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    normalized = value.strip()
    _reject_forbidden_proposal_text(normalized, field_name)
    return normalized


def _reject_forbidden_proposal_text(value: str, field_name: str) -> None:
    lowered = value.lower()
    for forbidden in FORBIDDEN_PROPOSAL_TERMS:
        if forbidden in lowered:
            raise ValueError(f"{field_name} must remain proposal-only; forbidden term: {forbidden}")


def _require_safe_identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    normalized = value.strip()
    if "/" in normalized or "\\" in normalized or normalized in {".", ".."} or ".." in normalized:
        raise ValueError(f"{field_name} must not contain path traversal or separators")
    return normalized


def _require_timezone_aware_datetime(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value
