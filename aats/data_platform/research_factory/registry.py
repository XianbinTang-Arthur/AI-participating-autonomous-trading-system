"""Research Factory memory registry for attempted candidates and failures."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any

from aats.data_platform.research_factory.features.expressions import parse_factor_expression
from aats.data_platform.research_factory.metrics.gates import (
    CandidateArtifact,
    CandidateGateResult,
)
from aats.data_platform.research_factory.metrics.snapshots import metric_snapshot_to_dict
from aats.data_platform.research_factory.observations import (
    ObservationGateResult,
    ObservationResult,
    ReviewOutcome,
)
from aats.data_platform.research_factory.specs import MetricsSnapshot

RESEARCH_MEMORY_SCHEMA_VERSION = "research_memory_entry_v1"
NOVELTY_GATE_SCHEMA_VERSION = "research_novelty_gate_v1"
DEFAULT_RESEARCH_MEMORY_PATH = (
    Path("artifacts") / "research" / "research_factory" / "registry" / "research_memory.jsonl"
)
ALLOWED_RESEARCH_MEMORY_STATUSES = frozenset(
    {
        "recommendation_ready",
        "gate_failed",
        "failed",
        "rejected",
        "duplicate",
        "observation_keep_reviewing",
        "observation_rejected",
        "observation_eligible_for_preapply",
    }
)
ALLOWED_NOVELTY_GATE_DECISIONS = frozenset({"allow", "duplicate", "retest", "warn", "suppress"})
NOVELTY_GATE_FAILURE_STATUSES = frozenset(
    {"gate_failed", "failed", "rejected", "observation_rejected"}
)
NOVELTY_GATE_SOFT_FAILURE_STATUSES = frozenset(
    NOVELTY_GATE_FAILURE_STATUSES | {"observation_keep_reviewing"}
)
_SECRET_MARKERS = (
    "api_key",
    "apikey",
    "access_key",
    "private_key",
    "okx",
    "password",
    "passwd",
    "secret",
    "token",
)


@dataclass(frozen=True, slots=True)
class ResearchMemorySimilarity:
    """Similarity evidence against a previous registry entry."""

    entry_id: str
    experiment_id: str
    status: str
    score: float
    reason: str
    created_at: datetime
    candidate_id: str | None = None

    def __post_init__(self) -> None:
        _require_safe_identifier(self.entry_id, "similarity.entry_id")
        _require_safe_identifier(self.experiment_id, "similarity.experiment_id")
        if self.candidate_id is not None:
            _require_safe_identifier(self.candidate_id, "similarity.candidate_id")
        if self.status not in ALLOWED_RESEARCH_MEMORY_STATUSES:
            allowed = ", ".join(sorted(ALLOWED_RESEARCH_MEMORY_STATUSES))
            raise ValueError(f"similarity status must be one of: {allowed}")
        if not isinstance(self.score, int | float) or not math.isfinite(float(self.score)):
            raise ValueError("similarity score must be finite")
        if self.score < 0 or self.score > 1:
            raise ValueError("similarity score must be between 0 and 1")
        object.__setattr__(self, "score", float(self.score))
        object.__setattr__(self, "reason", _require_non_empty_text(self.reason, "similarity.reason"))
        _require_timezone_aware_datetime(self.created_at, "similarity.created_at")

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ResearchMemorySimilarity":
        if not isinstance(payload, Mapping):
            raise ValueError("similarity payload must be a mapping")
        return cls(
            entry_id=str(payload.get("entry_id", "")),
            experiment_id=str(payload.get("experiment_id", "")),
            status=str(payload.get("status", "")),
            score=payload.get("score"),
            reason=str(payload.get("reason", "")),
            created_at=_parse_datetime(payload.get("created_at"), "similarity.created_at"),
            candidate_id=payload.get("candidate_id"),
        )


@dataclass(frozen=True, slots=True)
class NoveltyGateResult:
    """Research-only novelty decision from prior registry memory."""

    factor_signature: str
    dataset_fingerprint: str
    decision: str
    should_run: bool
    reasons: tuple[str, ...]
    matched_entries: tuple[ResearchMemorySimilarity, ...] = field(default_factory=tuple)
    failure_match_count: int = 0
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    schema_version: str = NOVELTY_GATE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "factor_signature",
            _require_non_empty_text(self.factor_signature, "factor_signature"),
        )
        object.__setattr__(
            self,
            "dataset_fingerprint",
            _require_non_empty_text(self.dataset_fingerprint, "dataset_fingerprint"),
        )
        if self.decision not in ALLOWED_NOVELTY_GATE_DECISIONS:
            allowed = ", ".join(sorted(ALLOWED_NOVELTY_GATE_DECISIONS))
            raise ValueError(f"novelty gate decision must be one of: {allowed}")
        expected_should_run = self.decision not in {"duplicate", "suppress"}
        if not isinstance(self.should_run, bool):
            raise ValueError("novelty gate should_run must be a bool")
        if self.should_run != expected_should_run:
            raise ValueError("novelty gate should_run must match decision")
        if not self.reasons:
            raise ValueError("novelty gate reasons must not be empty")
        object.__setattr__(self, "reasons", _normalize_text_sequence(self.reasons, "novelty_gate.reasons"))
        matches = tuple(
            item if isinstance(item, ResearchMemorySimilarity) else ResearchMemorySimilarity.from_dict(item)
            for item in self.matched_entries
        )
        object.__setattr__(self, "matched_entries", matches)
        if isinstance(self.failure_match_count, bool) or not isinstance(self.failure_match_count, int):
            raise ValueError("failure_match_count must be an integer")
        if self.failure_match_count < 0:
            raise ValueError("failure_match_count must be non-negative")
        _require_timezone_aware_datetime(self.evaluated_at, "novelty_gate.evaluated_at")
        if self.schema_version != NOVELTY_GATE_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {NOVELTY_GATE_SCHEMA_VERSION!r}")

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "NoveltyGateResult":
        if not isinstance(payload, Mapping):
            raise ValueError("novelty gate payload must be a mapping")
        reasons = payload.get("reasons", ())
        if isinstance(reasons, str | bytes | bytearray) or not isinstance(reasons, Sequence):
            raise ValueError("novelty gate reasons must be a sequence")
        matched_entries = payload.get("matched_entries", ())
        if isinstance(matched_entries, str | bytes | bytearray) or not isinstance(matched_entries, Sequence):
            raise ValueError("novelty gate matched_entries must be a sequence")
        return cls(
            factor_signature=str(payload.get("factor_signature", "")),
            dataset_fingerprint=str(payload.get("dataset_fingerprint", "")),
            decision=str(payload.get("decision", "")),
            should_run=payload.get("should_run"),
            reasons=tuple(reasons),
            matched_entries=tuple(matched_entries),
            failure_match_count=payload.get("failure_match_count", 0),
            evaluated_at=_parse_datetime(payload.get("evaluated_at"), "novelty_gate.evaluated_at"),
            schema_version=str(payload.get("schema_version", "")),
        )


@dataclass(frozen=True, slots=True)
class ResearchMemoryEntry:
    """One research attempt retained for duplicate detection and audit."""

    entry_id: str
    experiment_id: str
    status: str
    created_at: datetime
    created_by: str
    factor_signature: str | None = None
    dataset_fingerprint: str | None = None
    candidate_id: str | None = None
    candidate_type: str | None = None
    recommendation_id: str | None = None
    observation_id: str | None = None
    review_decision: str | None = None
    factor_expression: str | None = None
    benchmark_segment: str | None = None
    metric_snapshot: Mapping[str, Any] = field(default_factory=dict)
    gate_result: Mapping[str, Any] | None = None
    observation_metrics: Mapping[str, Any] = field(default_factory=dict)
    observation_gate_result: Mapping[str, Any] | None = None
    observation_failure_reasons: Sequence[str] = field(default_factory=tuple)
    failure_reason: str | None = None
    artifact_refs: Mapping[str, str] = field(default_factory=dict)
    similarity_to_existing: Sequence[ResearchMemorySimilarity] = field(default_factory=tuple)
    schema_version: str = RESEARCH_MEMORY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_safe_identifier(self.entry_id, "entry_id")
        _require_safe_identifier(self.experiment_id, "experiment_id")
        if self.status not in ALLOWED_RESEARCH_MEMORY_STATUSES:
            allowed = ", ".join(sorted(ALLOWED_RESEARCH_MEMORY_STATUSES))
            raise ValueError(f"research memory status must be one of: {allowed}")
        _require_timezone_aware_datetime(self.created_at, "created_at")
        object.__setattr__(self, "created_by", _require_non_empty_text(self.created_by, "created_by"))
        if self.factor_signature is not None:
            object.__setattr__(
                self,
                "factor_signature",
                _require_non_empty_text(self.factor_signature, "factor_signature"),
            )
        if self.dataset_fingerprint is not None:
            object.__setattr__(
                self,
                "dataset_fingerprint",
                _require_non_empty_text(self.dataset_fingerprint, "dataset_fingerprint"),
            )
        if self.candidate_id is not None:
            _require_safe_identifier(self.candidate_id, "candidate_id")
        if self.candidate_type is not None:
            object.__setattr__(
                self,
                "candidate_type",
                _require_non_empty_text(self.candidate_type, "candidate_type"),
            )
        if self.recommendation_id is not None:
            _require_safe_identifier(self.recommendation_id, "recommendation_id")
        if self.observation_id is not None:
            _require_safe_identifier(self.observation_id, "observation_id")
        if self.review_decision is not None:
            object.__setattr__(
                self,
                "review_decision",
                _require_non_empty_text(self.review_decision, "review_decision"),
            )
        if self.factor_expression is not None:
            object.__setattr__(
                self,
                "factor_expression",
                _require_non_empty_text(self.factor_expression, "factor_expression"),
            )
        if self.benchmark_segment is not None:
            object.__setattr__(
                self,
                "benchmark_segment",
                _require_non_empty_text(self.benchmark_segment, "benchmark_segment"),
            )
        object.__setattr__(self, "metric_snapshot", _normalize_json_mapping(self.metric_snapshot, "metric_snapshot"))
        if self.gate_result is not None:
            object.__setattr__(self, "gate_result", _normalize_json_mapping(self.gate_result, "gate_result"))
        object.__setattr__(
            self,
            "observation_metrics",
            _normalize_json_mapping(self.observation_metrics, "observation_metrics"),
        )
        if self.observation_gate_result is not None:
            object.__setattr__(
                self,
                "observation_gate_result",
                _normalize_json_mapping(self.observation_gate_result, "observation_gate_result"),
            )
        object.__setattr__(
            self,
            "observation_failure_reasons",
            _normalize_text_sequence(self.observation_failure_reasons, "observation_failure_reasons"),
        )
        if self.failure_reason is not None:
            object.__setattr__(
                self,
                "failure_reason",
                _redact_sensitive_text(_require_non_empty_text(self.failure_reason, "failure_reason")),
            )
        object.__setattr__(self, "artifact_refs", _normalize_artifact_refs(self.artifact_refs))
        similarities = tuple(
            item if isinstance(item, ResearchMemorySimilarity) else ResearchMemorySimilarity.from_dict(item)
            for item in self.similarity_to_existing
        )
        object.__setattr__(self, "similarity_to_existing", similarities)
        if self.schema_version != RESEARCH_MEMORY_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {RESEARCH_MEMORY_SCHEMA_VERSION!r}")

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ResearchMemoryEntry":
        if not isinstance(payload, Mapping):
            raise ValueError("research memory entry payload must be a mapping")
        return cls(
            entry_id=str(payload.get("entry_id", "")),
            experiment_id=str(payload.get("experiment_id", "")),
            status=str(payload.get("status", "")),
            created_at=_parse_datetime(payload.get("created_at"), "created_at"),
            created_by=str(payload.get("created_by", "")),
            factor_signature=payload.get("factor_signature"),
            dataset_fingerprint=payload.get("dataset_fingerprint"),
            candidate_id=payload.get("candidate_id"),
            candidate_type=payload.get("candidate_type"),
            recommendation_id=payload.get("recommendation_id"),
            observation_id=payload.get("observation_id"),
            review_decision=payload.get("review_decision"),
            factor_expression=payload.get("factor_expression"),
            benchmark_segment=payload.get("benchmark_segment"),
            metric_snapshot=payload.get("metric_snapshot", {}),
            gate_result=payload.get("gate_result"),
            observation_metrics=payload.get("observation_metrics", {}),
            observation_gate_result=payload.get("observation_gate_result"),
            observation_failure_reasons=payload.get("observation_failure_reasons", ()),
            failure_reason=payload.get("failure_reason"),
            artifact_refs=payload.get("artifact_refs", {}),
            similarity_to_existing=payload.get("similarity_to_existing", ()),
            schema_version=str(payload.get("schema_version", "")),
        )


class ResearchMemoryRegistry:
    """Atomic JSONL registry for Research Factory memory entries."""

    def __init__(self, path: str | Path = DEFAULT_RESEARCH_MEMORY_PATH) -> None:
        self.path = _require_research_registry_path(path)

    def load_entries(self) -> tuple[ResearchMemoryEntry, ...]:
        if not self.path.exists():
            return ()
        if not self.path.is_file():
            raise ValueError("research memory registry path is not a file")

        entries: list[ResearchMemoryEntry] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid JSONL registry line {line_number}") from exc
                entries.append(ResearchMemoryEntry.from_dict(payload))
        return tuple(entries)

    def find_similar(
        self,
        entry: ResearchMemoryEntry,
        *,
        limit: int = 5,
    ) -> tuple[ResearchMemorySimilarity, ...]:
        if not isinstance(entry, ResearchMemoryEntry):
            raise ValueError("entry must be a ResearchMemoryEntry")
        return _find_similar(entry, self.load_entries(), limit=limit)

    def evaluate_novelty(
        self,
        *,
        factor_expression: str,
        dataset_fingerprint: str,
        suppress_after_failures: int = 3,
        limit: int = 5,
        evaluated_at: datetime | None = None,
    ) -> NoveltyGateResult:
        """Evaluate whether a new factor proposal is novel enough to run."""
        return evaluate_novelty_gate(
            factor_expression=factor_expression,
            dataset_fingerprint=dataset_fingerprint,
            entries=self.load_entries(),
            suppress_after_failures=suppress_after_failures,
            limit=limit,
            evaluated_at=evaluated_at,
        )

    def upsert(self, entry: ResearchMemoryEntry) -> ResearchMemoryEntry:
        if not isinstance(entry, ResearchMemoryEntry):
            raise ValueError("entry must be a ResearchMemoryEntry")

        existing_entries = self.load_entries()
        enriched_entry = replace(
            entry,
            similarity_to_existing=_find_similar(entry, existing_entries, limit=5),
        )
        replaced = False
        merged_entries: list[ResearchMemoryEntry] = []
        for existing in existing_entries:
            if existing.entry_id == enriched_entry.entry_id:
                merged_entries.append(enriched_entry)
                replaced = True
            else:
                merged_entries.append(existing)
        if not replaced:
            merged_entries.append(enriched_entry)

        _write_registry_atomic(self.path, merged_entries)
        return enriched_entry


def default_research_memory_path_for_artifact_root(artifact_root: str | Path) -> Path:
    """Return the sibling registry path for a Research Factory experiment root."""
    root = Path(artifact_root)
    if root.name == "experiments":
        return root.parent / "registry" / "research_memory.jsonl"
    return root / "registry" / "research_memory.jsonl"


def factor_signature_from_expression(expression: str) -> str:
    """Build a deterministic signature for a factor DSL expression."""
    expression_text = _require_non_empty_text(expression, "factor_expression")
    try:
        parsed = parse_factor_expression(expression_text)
        payload = {
            "type": "factor_dsl",
            "normalized_ast": parsed.normalized_ast,
            "fields": parsed.fields,
            "functions": parsed.functions,
        }
    except ValueError:
        payload = {
            "type": "invalid_factor_dsl",
            "expression": expression_text.strip(),
        }
    return f"factor_signature_sha256:{_stable_hash(payload)}"


def evaluate_novelty_gate(
    *,
    factor_expression: str,
    dataset_fingerprint: str,
    entries: Sequence[ResearchMemoryEntry],
    suppress_after_failures: int = 3,
    limit: int = 5,
    evaluated_at: datetime | None = None,
) -> NoveltyGateResult:
    """Evaluate a factor proposal against prior Research Factory memory."""
    factor_signature = factor_signature_from_expression(factor_expression)
    dataset_fingerprint = _require_non_empty_text(dataset_fingerprint, "dataset_fingerprint")
    if isinstance(entries, str | bytes | bytearray) or not isinstance(entries, Sequence):
        raise ValueError("entries must be a sequence")
    normalized_entries = tuple(
        entry if isinstance(entry, ResearchMemoryEntry) else ResearchMemoryEntry.from_dict(entry)
        for entry in entries
    )
    _require_positive_integer(suppress_after_failures, "suppress_after_failures")
    _require_positive_integer(limit, "novelty gate limit")

    exact_matches = _novelty_matches(
        factor_signature=factor_signature,
        dataset_fingerprint=dataset_fingerprint,
        entries=normalized_entries,
        same_factor=True,
        same_dataset=True,
        limit=limit,
    )
    if exact_matches:
        return _build_novelty_gate_result(
            factor_signature=factor_signature,
            dataset_fingerprint=dataset_fingerprint,
            decision="duplicate",
            reasons=("same factor_signature and dataset_fingerprint already exists",),
            matched_entries=exact_matches,
            failure_match_count=_count_failure_matches(exact_matches),
            evaluated_at=evaluated_at,
        )

    same_factor_matches = _novelty_matches(
        factor_signature=factor_signature,
        dataset_fingerprint=dataset_fingerprint,
        entries=normalized_entries,
        same_factor=True,
        same_dataset=False,
        limit=limit,
    )
    same_factor_failures = tuple(
        match for match in same_factor_matches if match.status in NOVELTY_GATE_FAILURE_STATUSES
    )
    if len(same_factor_failures) >= suppress_after_failures:
        return _build_novelty_gate_result(
            factor_signature=factor_signature,
            dataset_fingerprint=dataset_fingerprint,
            decision="suppress",
            reasons=(f"same factor family has {len(same_factor_failures)} prior failure outcomes",),
            matched_entries=same_factor_failures[:limit],
            failure_match_count=len(same_factor_failures),
            evaluated_at=evaluated_at,
        )
    if same_factor_matches:
        return _build_novelty_gate_result(
            factor_signature=factor_signature,
            dataset_fingerprint=dataset_fingerprint,
            decision="retest",
            reasons=("same factor_signature exists on a different dataset_fingerprint",),
            matched_entries=same_factor_matches,
            failure_match_count=_count_failure_matches(same_factor_matches),
            evaluated_at=evaluated_at,
        )

    same_dataset_matches = _novelty_matches(
        factor_signature=factor_signature,
        dataset_fingerprint=dataset_fingerprint,
        entries=normalized_entries,
        same_factor=False,
        same_dataset=True,
        limit=limit,
    )
    same_dataset_soft_failures = tuple(
        match for match in same_dataset_matches if match.status in NOVELTY_GATE_SOFT_FAILURE_STATUSES
    )
    if same_dataset_soft_failures:
        return _build_novelty_gate_result(
            factor_signature=factor_signature,
            dataset_fingerprint=dataset_fingerprint,
            decision="warn",
            reasons=("same dataset_fingerprint has prior failed or unresolved research memory",),
            matched_entries=same_dataset_soft_failures[:limit],
            failure_match_count=_count_failure_matches(same_dataset_soft_failures),
            evaluated_at=evaluated_at,
        )

    return _build_novelty_gate_result(
        factor_signature=factor_signature,
        dataset_fingerprint=dataset_fingerprint,
        decision="allow",
        reasons=("no matching factor_signature or failed dataset memory found",),
        matched_entries=(),
        failure_match_count=0,
        evaluated_at=evaluated_at,
    )


def build_research_memory_entry(
    *,
    experiment_id: str,
    status: str,
    created_by: str,
    created_at: datetime,
    candidate: CandidateArtifact | None = None,
    metrics: MetricsSnapshot | None = None,
    gate: CandidateGateResult | None = None,
    factor_expression: str | None = None,
    dataset_fingerprint: str | None = None,
    failure_reason: str | None = None,
    artifact_refs: Mapping[str, str] | None = None,
) -> ResearchMemoryEntry:
    """Build a normalized registry entry from research-only artifacts."""
    _require_safe_identifier(experiment_id, "experiment_id")
    if candidate is not None:
        if not isinstance(candidate, CandidateArtifact):
            raise ValueError("candidate must be a CandidateArtifact")
        if candidate.experiment_id != experiment_id:
            raise ValueError("candidate experiment_id must match experiment_id")
        metrics = metrics or candidate.metrics
        gate = gate or candidate.gate
        factor_expression = factor_expression or _optional_text(candidate.payload.get("factor_expression"))
        dataset_fingerprint = dataset_fingerprint or _optional_text(candidate.payload.get("dataset_fingerprint"))
        candidate_id = candidate.candidate_id
        candidate_type = candidate.candidate_type
        benchmark_segment = _optional_text(candidate.payload.get("benchmark_segment"))
    else:
        candidate_id = None
        candidate_type = None
        benchmark_segment = None

    factor_signature = (
        factor_signature_from_expression(factor_expression)
        if factor_expression is not None
        else None
    )
    metric_snapshot = metric_snapshot_to_dict(metrics) if metrics is not None else {}
    gate_result = _gate_result_to_dict(gate) if gate is not None else None
    entry_id = _build_entry_id(
        experiment_id=experiment_id,
        candidate_id=candidate_id,
        factor_signature=factor_signature,
        dataset_fingerprint=dataset_fingerprint,
        status=status,
    )
    return ResearchMemoryEntry(
        entry_id=entry_id,
        experiment_id=experiment_id,
        status=status,
        created_at=created_at,
        created_by=created_by,
        factor_signature=factor_signature,
        dataset_fingerprint=dataset_fingerprint,
        candidate_id=candidate_id,
        candidate_type=candidate_type,
        factor_expression=factor_expression,
        benchmark_segment=benchmark_segment,
        metric_snapshot=metric_snapshot,
        gate_result=gate_result,
        failure_reason=failure_reason,
        artifact_refs=artifact_refs or {},
    )


def build_observation_memory_entry(
    *,
    candidate: CandidateArtifact,
    observation_result: ObservationResult,
    observation_gate: ObservationGateResult,
    review_outcome: ReviewOutcome,
    created_by: str,
    created_at: datetime,
    artifact_refs: Mapping[str, str] | None = None,
) -> ResearchMemoryEntry:
    """Build a registry entry from a completed observation review outcome."""
    if not isinstance(candidate, CandidateArtifact):
        raise ValueError("candidate must be a CandidateArtifact")
    if not isinstance(observation_result, ObservationResult):
        raise ValueError("observation_result must be an ObservationResult")
    if not isinstance(observation_gate, ObservationGateResult):
        raise ValueError("observation_gate must be an ObservationGateResult")
    if not isinstance(review_outcome, ReviewOutcome):
        raise ValueError("review_outcome must be a ReviewOutcome")
    _require_matching_observation_memory_inputs(candidate, observation_result, observation_gate, review_outcome)

    status = _observation_memory_status(review_outcome.decision)
    failure_reasons = _observation_failure_reasons(
        review_decision=review_outcome.decision,
        observation_result=observation_result,
        observation_gate=observation_gate,
    )
    factor_expression = _optional_text(candidate.payload.get("factor_expression"))
    dataset_fingerprint = _optional_text(candidate.payload.get("dataset_fingerprint"))
    factor_signature = (
        factor_signature_from_expression(factor_expression)
        if factor_expression is not None
        else None
    )
    failure_reason = "; ".join(failure_reasons) if failure_reasons else None
    entry_id = _build_observation_entry_id(
        observation_id=observation_result.observation_id,
        candidate_id=candidate.candidate_id,
        status=status,
    )
    return ResearchMemoryEntry(
        entry_id=entry_id,
        experiment_id=candidate.experiment_id,
        status=status,
        created_at=created_at,
        created_by=created_by,
        factor_signature=factor_signature,
        dataset_fingerprint=dataset_fingerprint,
        candidate_id=candidate.candidate_id,
        candidate_type=candidate.candidate_type,
        recommendation_id=review_outcome.recommendation_id,
        observation_id=observation_result.observation_id,
        review_decision=review_outcome.decision,
        factor_expression=factor_expression,
        benchmark_segment=_optional_text(candidate.payload.get("benchmark_segment")),
        metric_snapshot=metric_snapshot_to_dict(candidate.metrics),
        gate_result=_gate_result_to_dict(candidate.gate),
        observation_metrics=_observation_metrics_to_dict(observation_result),
        observation_gate_result=_observation_gate_result_to_dict(observation_gate),
        observation_failure_reasons=failure_reasons,
        failure_reason=failure_reason,
        artifact_refs=artifact_refs or {},
    )


def _find_similar(
    target: ResearchMemoryEntry,
    entries: Sequence[ResearchMemoryEntry],
    *,
    limit: int,
) -> tuple[ResearchMemorySimilarity, ...]:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("similarity limit must be a positive integer")
    matches: list[ResearchMemorySimilarity] = []
    for entry in entries:
        if entry.entry_id == target.entry_id:
            continue
        score, reason = _similarity_score(target, entry)
        if score <= 0:
            continue
        matches.append(
            ResearchMemorySimilarity(
                entry_id=entry.entry_id,
                experiment_id=entry.experiment_id,
                status=entry.status,
                score=score,
                reason=reason,
                created_at=entry.created_at,
                candidate_id=entry.candidate_id,
            )
        )
    matches.sort(key=lambda item: (-item.score, item.created_at.isoformat(), item.entry_id))
    return tuple(matches[:limit])


def _novelty_matches(
    *,
    factor_signature: str,
    dataset_fingerprint: str,
    entries: Sequence[ResearchMemoryEntry],
    same_factor: bool,
    same_dataset: bool,
    limit: int,
) -> tuple[ResearchMemorySimilarity, ...]:
    matches: list[ResearchMemorySimilarity] = []
    for entry in entries:
        factor_matches = entry.factor_signature == factor_signature
        dataset_matches = entry.dataset_fingerprint == dataset_fingerprint
        if same_factor != factor_matches or same_dataset != dataset_matches:
            continue
        score, reason = _novelty_match_score_and_reason(
            same_factor=same_factor,
            same_dataset=same_dataset,
        )
        matches.append(
            ResearchMemorySimilarity(
                entry_id=entry.entry_id,
                experiment_id=entry.experiment_id,
                status=entry.status,
                score=score,
                reason=reason,
                created_at=entry.created_at,
                candidate_id=entry.candidate_id,
            )
        )
    matches.sort(key=lambda item: (-item.score, item.created_at.isoformat(), item.entry_id))
    return tuple(matches[:limit])


def _novelty_match_score_and_reason(*, same_factor: bool, same_dataset: bool) -> tuple[float, str]:
    if same_factor and same_dataset:
        return 1.0, "same factor_signature and dataset_fingerprint"
    if same_factor:
        return 0.8, "same factor_signature on different dataset_fingerprint"
    return 0.35, "same dataset_fingerprint with different factor_signature"


def _count_failure_matches(matches: Sequence[ResearchMemorySimilarity]) -> int:
    return sum(1 for match in matches if match.status in NOVELTY_GATE_FAILURE_STATUSES)


def _build_novelty_gate_result(
    *,
    factor_signature: str,
    dataset_fingerprint: str,
    decision: str,
    reasons: Sequence[str],
    matched_entries: Sequence[ResearchMemorySimilarity],
    failure_match_count: int,
    evaluated_at: datetime | None,
) -> NoveltyGateResult:
    return NoveltyGateResult(
        factor_signature=factor_signature,
        dataset_fingerprint=dataset_fingerprint,
        decision=decision,
        should_run=decision not in {"duplicate", "suppress"},
        reasons=tuple(reasons),
        matched_entries=tuple(matched_entries),
        failure_match_count=failure_match_count,
        evaluated_at=evaluated_at or datetime.now(UTC),
    )


def _similarity_score(target: ResearchMemoryEntry, existing: ResearchMemoryEntry) -> tuple[float, str]:
    same_factor = (
        target.factor_signature is not None
        and existing.factor_signature is not None
        and target.factor_signature == existing.factor_signature
    )
    same_dataset = (
        target.dataset_fingerprint is not None
        and existing.dataset_fingerprint is not None
        and target.dataset_fingerprint == existing.dataset_fingerprint
    )
    if same_factor and same_dataset:
        return 1.0, "same factor_signature and dataset_fingerprint"
    if same_factor:
        return 0.8, "same factor_signature"
    if same_dataset:
        return 0.35, "same dataset_fingerprint"
    return 0.0, ""


def _gate_result_to_dict(gate: CandidateGateResult) -> dict[str, Any]:
    if not isinstance(gate, CandidateGateResult):
        raise ValueError("gate must be a CandidateGateResult")
    return {
        "passed": gate.passed,
        "failures": list(gate.failures),
        "thresholds": _to_jsonable(gate.thresholds),
        "critical_metrics": list(gate.critical_metrics),
        "evaluated_at": gate.evaluated_at.isoformat(),
    }


def _observation_metrics_to_dict(result: ObservationResult) -> dict[str, Any]:
    if not isinstance(result, ObservationResult):
        raise ValueError("observation_result must be an ObservationResult")
    return {
        "mode": result.mode,
        "observation_start": result.observation_start.isoformat(),
        "observation_end": result.observation_end.isoformat(),
        "observed_bars": result.observed_bars,
        "observed_events": result.observed_events,
        "signal_count": result.signal_count,
        "paper_intent_count": result.paper_intent_count,
        "fillable_ratio": result.fillable_ratio,
        "partial_fill_ratio": result.partial_fill_ratio,
        "fee_bps_mean": result.fee_bps_mean,
        "slippage_bps_mean": result.slippage_bps_mean,
        "funding_bps_mean": result.funding_bps_mean,
        "cost_adjusted_edge_bps_mean": result.cost_adjusted_edge_bps_mean,
        "drawdown": result.drawdown,
        "metric_drift": result.metric_drift,
        "abort_triggered": result.abort_triggered,
        "abort_reason": result.abort_reason,
    }


def _observation_gate_result_to_dict(gate: ObservationGateResult) -> dict[str, Any]:
    if not isinstance(gate, ObservationGateResult):
        raise ValueError("observation_gate must be an ObservationGateResult")
    return {
        "passed": gate.passed,
        "failures": list(gate.failures),
        "thresholds": _to_jsonable(gate.thresholds),
        "critical_metrics": list(gate.critical_metrics),
        "evaluated_at": gate.evaluated_at.isoformat(),
    }


def _require_matching_observation_memory_inputs(
    candidate: CandidateArtifact,
    observation_result: ObservationResult,
    observation_gate: ObservationGateResult,
    review_outcome: ReviewOutcome,
) -> None:
    if observation_result.candidate_id != candidate.candidate_id:
        raise ValueError("observation_result candidate_id must match candidate")
    if observation_result.experiment_id != candidate.experiment_id:
        raise ValueError("observation_result experiment_id must match candidate")
    if observation_gate.observation_id != observation_result.observation_id:
        raise ValueError("observation_gate observation_id must match observation_result")
    if observation_gate.recommendation_id != observation_result.recommendation_id:
        raise ValueError("observation_gate recommendation_id must match observation_result")
    if observation_gate.candidate_id != candidate.candidate_id:
        raise ValueError("observation_gate candidate_id must match candidate")
    if observation_gate.experiment_id != candidate.experiment_id:
        raise ValueError("observation_gate experiment_id must match candidate")
    if review_outcome.observation_id != observation_result.observation_id:
        raise ValueError("review_outcome observation_id must match observation_result")
    if review_outcome.recommendation_id != observation_result.recommendation_id:
        raise ValueError("review_outcome recommendation_id must match observation_result")
    if review_outcome.candidate_id != candidate.candidate_id:
        raise ValueError("review_outcome candidate_id must match candidate")
    if review_outcome.experiment_id != candidate.experiment_id:
        raise ValueError("review_outcome experiment_id must match candidate")
    if (
        review_outcome.observation_gate_passed is not None
        and review_outcome.observation_gate_passed != observation_gate.passed
    ):
        raise ValueError("review_outcome observation_gate_passed must match observation_gate")


def _observation_memory_status(review_decision: str) -> str:
    if review_decision == "eligible_for_preapply":
        return "observation_eligible_for_preapply"
    if review_decision == "keep_reviewing":
        return "observation_keep_reviewing"
    if review_decision == "reject":
        return "observation_rejected"
    raise ValueError("review_decision must be keep_reviewing, reject, or eligible_for_preapply")


def _observation_failure_reasons(
    *,
    review_decision: str,
    observation_result: ObservationResult,
    observation_gate: ObservationGateResult,
) -> tuple[str, ...]:
    if review_decision == "eligible_for_preapply" and observation_gate.passed:
        return ()
    reasons: list[str] = [f"review_decision={review_decision}"]
    if observation_result.abort_triggered:
        reason = observation_result.abort_reason or "abort_triggered"
        reasons.append(f"observation_abort: {reason}")
    if not observation_gate.passed:
        reasons.extend(f"observation_gate: {failure}" for failure in observation_gate.failures)
    return tuple(reasons)


def _build_entry_id(
    *,
    experiment_id: str,
    candidate_id: str | None,
    factor_signature: str | None,
    dataset_fingerprint: str | None,
    status: str,
) -> str:
    payload = {
        "candidate_id": candidate_id,
        "dataset_fingerprint": dataset_fingerprint,
        "experiment_id": experiment_id,
        "factor_signature": factor_signature,
        "status": status,
    }
    return f"mem_{_stable_hash(payload)[:20]}"


def _build_observation_entry_id(*, observation_id: str, candidate_id: str, status: str) -> str:
    payload = {
        "candidate_id": candidate_id,
        "observation_id": observation_id,
        "status": status,
    }
    return f"obs_mem_{_stable_hash(payload)[:20]}"


def _write_registry_atomic(path: Path, entries: Sequence[ResearchMemoryEntry]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = "".join(
        json.dumps(_to_jsonable(entry), ensure_ascii=False, sort_keys=True) + "\n"
        for entry in entries
    )

    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            delete=False,
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as handle:
            temp_path = handle.name
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)


def _normalize_artifact_refs(refs: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(refs, Mapping):
        raise ValueError("artifact_refs must be a mapping")
    normalized: dict[str, str] = {}
    for key, value in refs.items():
        ref_name = _require_non_empty_text(str(key), "artifact ref name")
        normalized[ref_name] = _require_relative_ref(value, f"artifact_refs.{ref_name}")
    return dict(sorted(normalized.items()))


def _normalize_json_mapping(value: Mapping[str, Any], field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a mapping")
    normalized = _to_jsonable(value)
    if not isinstance(normalized, dict):
        raise ValueError(f"{field_name} must be a mapping")
    return normalized


def _normalize_text_sequence(values: Sequence[str], field_name: str) -> tuple[str, ...]:
    if isinstance(values, str | bytes | bytearray) or not isinstance(values, Sequence):
        raise ValueError(f"{field_name} must be a sequence")
    return tuple(_redact_sensitive_text(_require_non_empty_text(str(value), field_name)) for value in values)


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {field.name: _to_jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, datetime):
        _require_timezone_aware_datetime(value, "datetime")
        return value.isoformat()
    if isinstance(value, PurePath):
        return value.as_posix()
    if isinstance(value, Mapping):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON values must be finite")
        return value
    if isinstance(value, int | str | bool) or value is None:
        return value
    raise TypeError(f"unsupported JSON registry value: {type(value).__name__}")


def _stable_hash(payload: Mapping[str, Any]) -> str:
    rendered = json.dumps(_to_jsonable(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _parse_datetime(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be an ISO datetime string")
    parsed = datetime.fromisoformat(value)
    _require_timezone_aware_datetime(parsed, field_name)
    return parsed


def _require_research_registry_path(value: str | Path) -> Path:
    path = Path(value)
    parts = path.parts
    if ".." in parts:
        raise ValueError("research memory registry path must not contain path traversal")
    has_research_artifact_root = any(
        parts[index] == "artifacts" and parts[index + 1] == "research"
        for index in range(len(parts) - 1)
    )
    if not has_research_artifact_root:
        raise ValueError("research memory registry path must be under artifacts/research")
    if path.suffix != ".jsonl":
        raise ValueError("research memory registry path must be a .jsonl file")
    return path


def _require_relative_ref(value: Any, field_name: str) -> str:
    ref = _require_non_empty_text(value, field_name)
    if ref.startswith("~"):
        raise ValueError(f"{field_name} must be a relative artifact ref")
    posix_path = PurePosixPath(ref)
    windows_path = PureWindowsPath(ref)
    if posix_path.is_absolute() or windows_path.is_absolute() or windows_path.drive:
        raise ValueError(f"{field_name} must be a relative artifact ref")
    if ".." in posix_path.parts or ".." in windows_path.parts:
        raise ValueError(f"{field_name} must not contain path traversal")
    return ref


def _require_safe_identifier(value: str, field_name: str) -> str:
    value = _require_non_empty_text(value, field_name)
    if "/" in value or "\\" in value or value in {".", ".."} or ".." in value:
        raise ValueError(f"{field_name} must not contain path traversal or separators")
    return value


def _require_non_empty_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _require_positive_integer(value: Any, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    return _require_non_empty_text(value, "optional text")


def _require_timezone_aware_datetime(value: datetime, field_name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime")


def _redact_sensitive_text(value: str) -> str:
    lowered = value.lower()
    if any(marker in lowered for marker in _SECRET_MARKERS):
        return "[REDACTED]"
    return value
