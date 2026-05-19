"""Candidate verdict board for Research Factory workflows.

Verdicts are operator-facing research conclusions only. They do not authorize
runtime mutation, active parameter writes, runtime config writes, or OKX writes.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any

CANDIDATE_VERDICT_SCHEMA_VERSION = "research_candidate_verdict_v1"
CANDIDATE_VERDICT_BOARD_JSONL_REF = "candidate_verdict_board.jsonl"
CANDIDATE_VERDICT_BOARD_MD_REF = "candidate_verdict_board.md"
ALLOWED_CANDIDATE_VERDICTS = frozenset(
    {"reject", "keep_observing", "positive_executable_edge"}
)
ALLOWED_VERDICT_NEXT_ACTIONS = frozenset(
    {
        "archive",
        "request_more_observation",
        "review_preapply_evidence",
        "record_positive_executable_edge",
    }
)
FORBIDDEN_VERDICT_TERMS = (
    "active_parameter",
    "approved_for_apply",
    "auto_apply",
    "direct_apply",
    "live_order",
    "okx_write",
    "operator_write",
    "production_config",
    "runtime_config_write",
    "runtime_mutation",
)


@dataclass(frozen=True, slots=True)
class CandidateVerdict:
    """Research-only verdict for a candidate after workflow evidence review."""

    candidate_id: str
    experiment_id: str
    workflow_id: str
    symbol: str
    timeframe: str
    factor_expression: str
    research_profile: str
    net_annualized_return: float | None
    max_drawdown: float | None
    cost_adjusted_edge_bps_mean: float | None
    fillable_ratio: float | None
    partial_fill_ratio: float | None
    observation_gate_passed: bool
    reference_integrity_passed: bool
    risk_flags: Sequence[str]
    verdict: str
    reason: str
    next_action: str
    candidate_gate_passed: bool = False
    evidence_bundle_passed: bool = False
    blocking_failures: Sequence[str] = field(default_factory=tuple)
    workflow_status: str = "unknown"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    schema_version: str = CANDIDATE_VERDICT_SCHEMA_VERSION
    runtime_mutation_allowed: bool = False
    active_parameter_write_allowed: bool = False
    runtime_config_write_allowed: bool = False
    okx_write_allowed: bool = False

    def __post_init__(self) -> None:
        _require_safe_identifier(self.candidate_id, "candidate_id")
        _require_safe_identifier(self.experiment_id, "experiment_id")
        _require_safe_identifier(self.workflow_id, "workflow_id")
        object.__setattr__(self, "symbol", _require_control_text(self.symbol, "symbol"))
        object.__setattr__(self, "timeframe", _require_control_text(self.timeframe, "timeframe"))
        object.__setattr__(
            self,
            "factor_expression",
            _require_non_empty_text(self.factor_expression, "factor_expression"),
        )
        object.__setattr__(
            self,
            "research_profile",
            _require_control_text(self.research_profile, "research_profile"),
        )
        for field_name in (
            "net_annualized_return",
            "max_drawdown",
            "cost_adjusted_edge_bps_mean",
            "fillable_ratio",
            "partial_fill_ratio",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _require_finite_number(value, field_name))
        if not isinstance(self.observation_gate_passed, bool):
            raise ValueError("observation_gate_passed must be a bool")
        if not isinstance(self.reference_integrity_passed, bool):
            raise ValueError("reference_integrity_passed must be a bool")
        if not isinstance(self.candidate_gate_passed, bool):
            raise ValueError("candidate_gate_passed must be a bool")
        if not isinstance(self.evidence_bundle_passed, bool):
            raise ValueError("evidence_bundle_passed must be a bool")
        object.__setattr__(
            self,
            "risk_flags",
            _normalize_text_sequence(self.risk_flags, "risk_flags", allow_empty=True),
        )
        object.__setattr__(
            self,
            "blocking_failures",
            _normalize_text_sequence(
                self.blocking_failures,
                "blocking_failures",
                allow_empty=True,
            ),
        )
        if self.verdict not in ALLOWED_CANDIDATE_VERDICTS:
            allowed = ", ".join(sorted(ALLOWED_CANDIDATE_VERDICTS))
            raise ValueError(f"verdict must be one of: {allowed}")
        reason = _require_non_empty_text(self.reason, "reason")
        _reject_promotion_text(reason, "reason")
        object.__setattr__(self, "reason", reason)
        if self.next_action not in ALLOWED_VERDICT_NEXT_ACTIONS:
            allowed = ", ".join(sorted(ALLOWED_VERDICT_NEXT_ACTIONS))
            raise ValueError(f"next_action must be one of: {allowed}")
        _reject_promotion_text(self.next_action, "next_action")
        object.__setattr__(
            self,
            "workflow_status",
            _require_control_text(self.workflow_status, "workflow_status"),
        )
        _require_timezone_aware_datetime(self.created_at, "created_at")
        if self.schema_version != CANDIDATE_VERDICT_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {CANDIDATE_VERDICT_SCHEMA_VERSION!r}")
        _require_no_runtime_permissions(self)

    def to_dict(self) -> dict[str, Any]:
        return _to_jsonable(self)


def build_candidate_verdict_from_workflow(
    workflow_summary_path: str | Path,
    *,
    research_factory_root: str | Path | None = None,
    created_at: datetime | None = None,
) -> CandidateVerdict:
    """Build a verdict from a workflow summary and its referenced artifacts."""
    workflow_summary = _load_json_mapping(Path(workflow_summary_path), "workflow_summary")
    root = _resolve_research_factory_root(
        research_factory_root,
        workflow_summary_path=Path(workflow_summary_path),
    )
    artifact_refs = _require_mapping(workflow_summary.get("artifact_refs"), "artifact_refs")
    operator_checklist = _load_optional_json_ref(
        root,
        artifact_refs.get("operator_review_checklist"),
    )
    candidate = _load_optional_json_ref(root, artifact_refs.get("candidate_artifact"))
    metrics = _load_optional_json_ref(root, artifact_refs.get("metrics_snapshot"))
    observation_gate = _load_optional_json_ref(root, artifact_refs.get("observation_gate_result"))
    observation_result = _load_optional_json_ref(root, artifact_refs.get("observation_result"))
    evidence_bundle = _load_optional_json_ref(root, artifact_refs.get("evidence_bundle"))
    preapply_package = _load_optional_json_ref(root, artifact_refs.get("preapply_evidence_package"))
    experiment_spec = _load_optional_json_ref(
        root,
        _experiment_spec_ref(workflow_summary, artifact_refs),
    )
    return build_candidate_verdict_from_payloads(
        workflow_summary=workflow_summary,
        operator_checklist=operator_checklist,
        candidate_artifact=candidate,
        metrics_snapshot=metrics,
        observation_gate_result=observation_gate,
        observation_result=observation_result,
        evidence_bundle=evidence_bundle,
        preapply_evidence_package=preapply_package,
        experiment_spec=experiment_spec,
        created_at=created_at,
    )


def build_candidate_verdict_from_payloads(
    *,
    workflow_summary: Mapping[str, Any],
    operator_checklist: Mapping[str, Any] | None = None,
    candidate_artifact: Mapping[str, Any] | None = None,
    metrics_snapshot: Mapping[str, Any] | None = None,
    observation_gate_result: Mapping[str, Any] | None = None,
    observation_result: Mapping[str, Any] | None = None,
    evidence_bundle: Mapping[str, Any] | None = None,
    preapply_evidence_package: Mapping[str, Any] | None = None,
    experiment_spec: Mapping[str, Any] | None = None,
    created_at: datetime | None = None,
) -> CandidateVerdict:
    """Build a deterministic candidate verdict from workflow payloads."""
    if not isinstance(workflow_summary, Mapping):
        raise ValueError("workflow_summary must be a mapping")
    readiness = _require_mapping(operator_checklist.get("readiness"), "readiness") if operator_checklist else {}
    candidate_payload = (
        _require_mapping(candidate_artifact.get("payload"), "candidate.payload")
        if isinstance(candidate_artifact, Mapping)
        else {}
    )
    candidate_gate = (
        _require_mapping(candidate_artifact.get("gate"), "candidate.gate")
        if isinstance(candidate_artifact, Mapping)
        else {}
    )

    candidate_gate_passed = _bool_from_sources(
        candidate_gate.get("passed"),
        readiness.get("candidate_gate_passed"),
        default=False,
    )
    evidence_bundle_passed = _bool_from_sources(
        evidence_bundle.get("passed") if isinstance(evidence_bundle, Mapping) else None,
        readiness.get("evidence_bundle_passed"),
        default=False,
    )
    observation_gate_passed = _bool_from_sources(
        observation_gate_result.get("passed") if isinstance(observation_gate_result, Mapping) else None,
        workflow_summary.get("observation_gate_passed"),
        readiness.get("observation_gate_passed"),
        default=False,
    )
    reference_integrity_passed = _bool_from_sources(
        workflow_summary.get("reference_integrity_passed"),
        readiness.get("reference_integrity_passed"),
        default=False,
    )

    risk_flags = tuple(str(item) for item in workflow_summary.get("risk_flags", ()) or ())
    blocking_failures = tuple(
        str(item) for item in workflow_summary.get("blocking_failures", ()) or ()
    )
    observation_failures = tuple(
        str(item)
        for item in (
            observation_gate_result.get("failures", ())
            if isinstance(observation_gate_result, Mapping)
            else ()
        )
    )
    edge = _metric_value(
        observation_result,
        metrics_snapshot,
        field_name="cost_adjusted_edge_bps_mean",
    )

    verdict, reason, next_action = _verdict_decision(
        evidence_bundle_passed=evidence_bundle_passed,
        candidate_gate_passed=candidate_gate_passed,
        observation_gate_passed=observation_gate_passed,
        reference_integrity_passed=reference_integrity_passed,
        cost_adjusted_edge_bps_mean=edge,
        risk_flags=risk_flags,
        blocking_failures=blocking_failures,
        observation_failures=observation_failures,
    )

    return CandidateVerdict(
        candidate_id=_first_text(
            workflow_summary.get("candidate_id"),
            candidate_artifact.get("candidate_id") if isinstance(candidate_artifact, Mapping) else None,
            field_name="candidate_id",
        ),
        experiment_id=_first_text(
            workflow_summary.get("experiment_id"),
            candidate_artifact.get("experiment_id") if isinstance(candidate_artifact, Mapping) else None,
            field_name="experiment_id",
        ),
        workflow_id=_require_mapping_text(workflow_summary, "workflow_id"),
        symbol=_symbol_from_payloads(experiment_spec),
        timeframe=_timeframe_from_payloads(experiment_spec),
        factor_expression=str(candidate_payload.get("factor_expression") or "n/a"),
        research_profile=_require_mapping_text(workflow_summary, "profile"),
        net_annualized_return=_metric_value(
            metrics_snapshot,
            candidate_artifact,
            field_name="net_annualized_return",
        ),
        max_drawdown=_metric_value(
            observation_result,
            metrics_snapshot,
            field_name="max_drawdown",
        ),
        cost_adjusted_edge_bps_mean=edge,
        fillable_ratio=_metric_value(
            observation_result,
            metrics_snapshot,
            field_name="fillable_ratio",
        ),
        partial_fill_ratio=_metric_value(
            observation_result,
            metrics_snapshot,
            field_name="partial_fill_ratio",
        ),
        observation_gate_passed=observation_gate_passed,
        reference_integrity_passed=reference_integrity_passed,
        risk_flags=risk_flags,
        verdict=verdict,
        reason=reason,
        next_action=next_action,
        candidate_gate_passed=candidate_gate_passed,
        evidence_bundle_passed=evidence_bundle_passed,
        blocking_failures=blocking_failures,
        workflow_status=str(workflow_summary.get("status") or "unknown"),
        created_at=created_at or datetime.now(UTC),
    )


def update_candidate_verdict_board(
    verdicts: Sequence[CandidateVerdict],
    *,
    board_root: str | Path,
) -> tuple[Path, Path]:
    """Merge verdicts into the JSONL board and render a Markdown board."""
    if isinstance(verdicts, CandidateVerdict):
        raise ValueError("verdicts must be a sequence of CandidateVerdict")
    if not all(isinstance(verdict, CandidateVerdict) for verdict in verdicts):
        raise ValueError("all verdicts must be CandidateVerdict instances")
    board_dir = _require_research_artifact_directory(board_root)
    board_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = board_dir / CANDIDATE_VERDICT_BOARD_JSONL_REF
    md_path = board_dir / CANDIDATE_VERDICT_BOARD_MD_REF
    merged: dict[str, dict[str, Any]] = {}
    if jsonl_path.exists():
        for line in jsonl_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            if not isinstance(payload, Mapping):
                raise ValueError("existing verdict board line must contain a JSON object")
            merged[str(payload["workflow_id"])] = dict(payload)
    for verdict in verdicts:
        merged[verdict.workflow_id] = verdict.to_dict()
    ordered = [merged[key] for key in sorted(merged)]
    _write_text_atomic(
        jsonl_path,
        "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in ordered),
    )
    _write_text_atomic(md_path, _render_verdict_board_markdown(ordered))
    return jsonl_path, md_path


def _verdict_decision(
    *,
    evidence_bundle_passed: bool,
    candidate_gate_passed: bool,
    observation_gate_passed: bool,
    reference_integrity_passed: bool,
    cost_adjusted_edge_bps_mean: float | None,
    risk_flags: Sequence[str],
    blocking_failures: Sequence[str],
    observation_failures: Sequence[str],
) -> tuple[str, str, str]:
    if not evidence_bundle_passed:
        return "reject", "evidence bundle failed", "archive"
    if not candidate_gate_passed:
        return "reject", "candidate gate failed", "archive"
    if not reference_integrity_passed:
        return "reject", "reference integrity failed", "archive"
    if cost_adjusted_edge_bps_mean is not None and cost_adjusted_edge_bps_mean <= 0:
        return "reject", "cost-adjusted edge is not positive", "archive"
    if not observation_gate_passed:
        if _only_insufficient_observation(observation_failures):
            return "keep_observing", "observation sample is still insufficient", "request_more_observation"
        return "reject", "observation gate failed on executable evidence", "archive"
    if "execution_evidence_uses_dataset_compatibility" in risk_flags:
        return "keep_observing", "execution evidence uses dataset compatibility mode", "request_more_observation"
    if blocking_failures:
        return "keep_observing", "workflow still has blocking follow-ups", "request_more_observation"
    if cost_adjusted_edge_bps_mean is None:
        return "keep_observing", "cost-adjusted edge is missing", "request_more_observation"
    return (
        "positive_executable_edge",
        "all required gates passed with positive cost-adjusted edge",
        "review_preapply_evidence",
    )


def _only_insufficient_observation(failures: Sequence[str]) -> bool:
    return bool(failures) and all(
        failure.startswith(("observed_bars=", "observed_events=")) for failure in failures
    )


def _render_verdict_board_markdown(entries: Sequence[Mapping[str, Any]]) -> str:
    lines = [
        "# Research Factory Candidate Verdict Board",
        "",
        "This board is research-only and does not authorize runtime mutation, active parameter updates, runtime config writes, OKX writes, auto apply, or production deployment.",
        "",
        "| Workflow | Candidate | Symbol | Timeframe | Profile | Verdict | Reason | Next Action |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for entry in entries:
        lines.append(
            "| {workflow_id} | {candidate_id} | {symbol} | {timeframe} | {research_profile} | {verdict} | {reason} | {next_action} |".format(
                workflow_id=_md_cell(entry.get("workflow_id")),
                candidate_id=_md_cell(entry.get("candidate_id")),
                symbol=_md_cell(entry.get("symbol")),
                timeframe=_md_cell(entry.get("timeframe")),
                research_profile=_md_cell(entry.get("research_profile")),
                verdict=_md_cell(entry.get("verdict")),
                reason=_md_cell(entry.get("reason")),
                next_action=_md_cell(entry.get("next_action")),
            )
        )
    lines.append("")
    return "\n".join(lines)


def _experiment_spec_ref(
    workflow_summary: Mapping[str, Any],
    artifact_refs: Mapping[str, Any],
) -> str | None:
    if artifact_refs.get("experiment_spec"):
        return str(artifact_refs["experiment_spec"])
    experiment_id = workflow_summary.get("experiment_id")
    if isinstance(experiment_id, str) and experiment_id.strip():
        return f"experiments/{experiment_id}/experiment_spec.json"
    return None


def _resolve_research_factory_root(
    research_factory_root: str | Path | None,
    *,
    workflow_summary_path: Path,
) -> Path:
    if research_factory_root is not None:
        return _require_research_artifact_directory(research_factory_root)
    resolved = workflow_summary_path.resolve(strict=False)
    parts = resolved.parts
    for index in range(len(parts) - 2):
        if parts[index] == "artifacts" and parts[index + 1] == "research":
            candidate = Path(*parts[: index + 2]) / "research_factory"
            if candidate.name == "research_factory":
                return candidate
    raise ValueError("research_factory_root is required when workflow summary is not under artifacts/research")


def _load_optional_json_ref(root: Path, ref: Any) -> dict[str, Any] | None:
    if not isinstance(ref, str) or not ref.strip():
        return None
    path = _resolve_artifact_ref(root, ref)
    if not path.exists():
        return None
    return _load_json_mapping(path, ref)


def _resolve_artifact_ref(root: Path, ref: str) -> Path:
    ref = _require_relative_ref(ref, "artifact_ref")
    path = root / ref
    resolved = path.resolve(strict=False)
    if not _is_relative_to(resolved, root.resolve(strict=False)):
        raise ValueError("artifact ref must stay under research_factory_root")
    return path


def _load_json_mapping(path: Path, field_name: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} must contain valid JSON") from exc
    if not isinstance(payload, Mapping):
        raise ValueError(f"{field_name} must contain a JSON object")
    return dict(payload)


def _metric_value(*payloads: Mapping[str, Any] | None, field_name: str) -> float | None:
    for payload in payloads:
        if not isinstance(payload, Mapping):
            continue
        value: Any = payload.get(field_name)
        if value is None and isinstance(payload.get("metrics"), Mapping):
            value = payload["metrics"].get(field_name)
        if value is None:
            continue
        return _require_finite_number(value, field_name)
    return None


def _symbol_from_payloads(experiment_spec: Mapping[str, Any] | None) -> str:
    dataset = experiment_spec.get("dataset") if isinstance(experiment_spec, Mapping) else None
    if isinstance(dataset, Mapping):
        value = dataset.get("symbol")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "unknown"


def _timeframe_from_payloads(experiment_spec: Mapping[str, Any] | None) -> str:
    dataset = experiment_spec.get("dataset") if isinstance(experiment_spec, Mapping) else None
    if isinstance(dataset, Mapping):
        value = dataset.get("timeframe")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "unknown"


def _bool_from_sources(*values: Any, default: bool) -> bool:
    for value in values:
        if isinstance(value, bool):
            return value
    return default


def _first_text(*values: Any, field_name: str) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise ValueError(f"{field_name} must be available")


def _require_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a mapping")
    return value


def _require_mapping_text(payload: Mapping[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _require_safe_identifier(value: Any, field_name: str) -> str:
    value = _require_non_empty_text(value, field_name)
    if "/" in value or "\\" in value or value in {".", ".."} or ".." in value:
        raise ValueError(f"{field_name} must not contain path traversal or separators")
    _reject_promotion_text(value, field_name)
    return value


def _require_control_text(value: Any, field_name: str) -> str:
    text = _require_non_empty_text(value, field_name)
    if "/" in text or "\\" in text or text in {".", ".."} or ".." in text or text.startswith("~"):
        raise ValueError(f"{field_name} must not contain path traversal or separators")
    _reject_promotion_text(text, field_name)
    return text


def _require_non_empty_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _normalize_text_sequence(
    values: Sequence[str],
    field_name: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if isinstance(values, str | bytes | bytearray) or not isinstance(values, Sequence):
        raise ValueError(f"{field_name} must be a sequence of strings")
    normalized = tuple(_require_non_empty_text(value, field_name) for value in values)
    if not allow_empty and not normalized:
        raise ValueError(f"{field_name} must not be empty")
    for value in normalized:
        _reject_promotion_text(value, field_name)
    return tuple(dict.fromkeys(normalized))


def _require_relative_ref(value: Any, field_name: str) -> str:
    ref = _require_non_empty_text(value, field_name)
    posix_path = PurePosixPath(ref)
    windows_path = PureWindowsPath(ref)
    if posix_path.is_absolute() or windows_path.is_absolute() or windows_path.drive:
        raise ValueError(f"{field_name} must be a relative artifact ref")
    if ".." in posix_path.parts or ".." in windows_path.parts:
        raise ValueError(f"{field_name} must not contain path traversal")
    _reject_promotion_text(ref, field_name)
    return ref


def _require_finite_number(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{field_name} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field_name} must be finite")
    return result


def _require_timezone_aware_datetime(value: datetime, field_name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime")


def _require_no_runtime_permissions(value: CandidateVerdict) -> None:
    if value.runtime_mutation_allowed is not False:
        raise ValueError("candidate verdict must not allow runtime mutation")
    if value.active_parameter_write_allowed is not False:
        raise ValueError("candidate verdict must not allow active parameter writes")
    if value.runtime_config_write_allowed is not False:
        raise ValueError("candidate verdict must not allow runtime config writes")
    if value.okx_write_allowed is not False:
        raise ValueError("candidate verdict must not allow OKX writes")


def _require_research_artifact_directory(value: str | Path) -> Path:
    path = Path(value)
    if ".." in path.parts:
        raise ValueError("artifact directory must not contain path traversal")
    if not any(
        path.parts[index] == "artifacts" and path.parts[index + 1] == "research"
        for index in range(len(path.parts) - 1)
    ):
        raise ValueError("artifact directory must be under artifacts/research")
    return path


def _reject_promotion_text(value: str, field_name: str) -> None:
    lowered = value.lower()
    for term in FORBIDDEN_VERDICT_TERMS:
        if term in lowered:
            raise ValueError(f"{field_name} must not encode runtime promotion term: {term}")


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {item.name: _to_jsonable(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime values must be timezone-aware")
        return value.isoformat()
    if isinstance(value, PurePath):
        return value.as_posix()
    if isinstance(value, Mapping):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("float values must be finite")
        return value
    if isinstance(value, int | str | bool) or value is None:
        return value
    raise TypeError(f"unsupported JSON artifact value: {type(value).__name__}")


def _md_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")
