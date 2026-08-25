#!/usr/bin/env python3
"""Evaluate a complete v2 development candidate family without opening holdout.

The campaign derives p-values from immutable experiment return-series artifacts,
counts every planned trial, collapses exact hypothesis duplicates for selection,
and writes research-only statistical evidence.  It never accesses a database,
runtime parameters, exchange credentials, orders, or the sealed holdout series.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from aats.data_platform.governance._atomic_io import immutable_json_write  # noqa: E402
from aats.data_platform.research_factory.registry import (  # noqa: E402
    factor_signature_from_expression,
)
from aats.data_platform.research_factory.validation.statistics import (  # noqa: E402
    build_purged_walk_forward_splits,
    evaluate_statistical_evidence,
    evaluate_walk_forward,
    moving_block_bootstrap_mean,
)
from scripts.rdp_run_candidate_v2_batch import (  # noqa: E402
    experiment_id_for_plan,
    load_and_validate_plan,
)

RETURN_SERIES_SCHEMA = "research_development_return_series_v1"
SELECTION_PROTOCOL = "train_valid_selection_test_holdout_v2"
CAMPAIGN_SCHEMA = "research_candidate_campaign_v1"


def _load_mapping(path: Path, label: str) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"{label}_must_be_object")
    return dict(payload)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _series_fingerprint(values: Sequence[float]) -> str:
    encoded = json.dumps(
        list(values),
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _finite_returns(values: Any) -> tuple[float, ...]:
    if isinstance(values, str | bytes | bytearray) or not isinstance(values, Sequence):
        raise ValueError("valid_net_returns_must_be_array")
    returns = tuple(float(value) for value in values)
    if len(returns) < 20:
        raise ValueError("valid_returns_require_at_least_20_samples")
    if any(not math.isfinite(value) for value in returns):
        raise ValueError("valid_returns_must_be_finite")
    if any(value <= -1.0 for value in returns):
        raise ValueError("valid_returns_must_be_greater_than_minus_one")
    return returns


def _validate_return_series(
    payload: Mapping[str, Any],
    *,
    experiment_id: str,
) -> tuple[tuple[float, ...], str, float]:
    if payload.get("schema_version") != RETURN_SERIES_SCHEMA:
        raise ValueError("development_return_series_schema_mismatch")
    if payload.get("selection_protocol_version") != SELECTION_PROTOCOL:
        raise ValueError("development_return_series_protocol_mismatch")
    if payload.get("experiment_id") != experiment_id:
        raise ValueError("development_return_series_experiment_id_mismatch")
    if payload.get("benchmark_segment") != "valid":
        raise ValueError("development_return_series_benchmark_not_valid")
    dataset_fingerprint = str(payload.get("dataset_fingerprint", "")).strip()
    if not dataset_fingerprint:
        raise ValueError("development_return_series_dataset_fingerprint_missing")
    segments = payload.get("segments")
    if not isinstance(segments, Mapping) or set(segments) != {"train", "valid"}:
        raise ValueError("development_return_series_segments_must_be_train_valid_only")
    valid = segments.get("valid")
    if not isinstance(valid, Mapping):
        raise ValueError("development_return_series_valid_segment_missing")
    returns = _finite_returns(valid.get("net_returns"))
    if valid.get("sample_count") != len(returns):
        raise ValueError("development_return_series_sample_count_mismatch")
    if valid.get("series_fingerprint") != _series_fingerprint(returns):
        raise ValueError("development_return_series_fingerprint_mismatch")
    holdout = payload.get("holdout")
    expected_holdout_keys = {
        "segment",
        "status",
        "content_fingerprint",
        "values_exposed",
    }
    if not isinstance(holdout, Mapping) or set(holdout) != expected_holdout_keys:
        raise ValueError("development_return_series_holdout_shape_invalid")
    if (
        holdout.get("segment") != "test"
        or holdout.get("status") != "sealed_not_evaluated"
        or holdout.get("values_exposed") is not False
    ):
        raise ValueError("development_return_series_holdout_not_sealed")
    costs = payload.get("cost_assumptions")
    if not isinstance(costs, Mapping):
        raise ValueError("development_return_series_cost_assumptions_missing")
    periods_per_year = float(costs.get("periods_per_year", 0.0))
    if not math.isfinite(periods_per_year) or periods_per_year <= 0.0:
        raise ValueError("development_return_series_periods_per_year_invalid")
    return returns, dataset_fingerprint, periods_per_year


def _planned_hypothesis_fingerprint(plan: Mapping[str, Any]) -> str:
    payload = {
        "factor_signature": factor_signature_from_expression(str(plan["factor_expression"])),
        "dataset_version": str(plan["dataset_version"]),
        "symbol": str(plan["symbol"]).upper(),
        "timeframe": str(plan["timeframe"]),
        "start": str(plan["start"]),
        "end": str(plan["end"]),
        "label_horizon_bars": int(plan["label_horizon_bars"]),
        "train_ratio": float(plan["train_ratio"]),
        "valid_ratio": float(plan["valid_ratio"]),
        "test_ratio": float(plan["test_ratio"]),
        "fee_bps": float(plan["fee_bps"]),
        "slippage_bps": float(plan["slippage_bps"]),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _hypothesis_fingerprint(
    plan: Mapping[str, Any],
    *,
    dataset_fingerprint: str,
) -> str:
    payload = {
        "planned_hypothesis_fingerprint": _planned_hypothesis_fingerprint(plan),
        "dataset_fingerprint": dataset_fingerprint,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _validate_success_candidate(
    experiment_dir: Path,
    *,
    experiment_id: str,
    candidate_id: str,
    dataset_fingerprint: str,
) -> tuple[bool, str | None, str | None]:
    manifest_path = experiment_dir / "experiment_manifest.json"
    candidate_path = experiment_dir / "candidate_artifact.json"
    if not manifest_path.is_file():
        return False, "experiment_manifest_missing", None
    manifest = _load_mapping(manifest_path, "experiment_manifest")
    if manifest.get("status") != "succeeded":
        return False, "experiment_not_succeeded", None
    if not candidate_path.is_file():
        return False, "candidate_artifact_missing", None
    candidate = _load_mapping(candidate_path, "candidate_artifact")
    candidate_payload = candidate.get("payload")
    if candidate.get("candidate_id") != candidate_id:
        raise ValueError("candidate_artifact_id_mismatch")
    if candidate.get("experiment_id") != experiment_id:
        raise ValueError("candidate_artifact_experiment_id_mismatch")
    if not isinstance(candidate_payload, Mapping):
        raise ValueError("candidate_artifact_payload_missing")
    if candidate_payload.get("selection_protocol_version") != SELECTION_PROTOCOL:
        raise ValueError("candidate_artifact_protocol_mismatch")
    if candidate_payload.get("dataset_fingerprint") != dataset_fingerprint:
        raise ValueError("candidate_artifact_dataset_fingerprint_mismatch")
    if candidate_payload.get("benchmark_segment") != "valid":
        raise ValueError("candidate_artifact_benchmark_not_valid")
    if candidate_payload.get("holdout_status") != "sealed_not_evaluated":
        raise ValueError("candidate_artifact_holdout_not_sealed")
    return True, None, _sha256(candidate_path)


def evaluate_campaign(
    *,
    plan_root: Path,
    artifact_root: Path,
    experiment_root: Path,
    replications: int = 2_000,
    alpha: float = 0.05,
    min_deflated_sharpe_probability: float = 0.95,
    seed: int = 0,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    if replications < 100:
        raise ValueError("bootstrap_replications_must_be_at_least_100")
    if not 0.0 < alpha < 0.5:
        raise ValueError("campaign_alpha_out_of_range")
    if not 0.0 < min_deflated_sharpe_probability < 1.0:
        raise ValueError("min_deflated_sharpe_probability_out_of_range")
    resolved_artifact_root = artifact_root.resolve()
    resolved_plan_root = plan_root.resolve()
    resolved_experiment_root = experiment_root.resolve()
    for label, path in (
        ("plan", resolved_plan_root),
        ("experiment", resolved_experiment_root),
    ):
        try:
            path.relative_to(resolved_artifact_root)
        except ValueError as exc:
            raise ValueError(f"campaign_{label}_root_outside_artifact_root") from exc
    paths = sorted(plan_root.glob("*.json"))
    if not paths:
        raise ValueError("v2_replay_plans_required")
    plans = [load_and_validate_plan(path, artifact_root=artifact_root) for path in paths]
    entries: list[dict[str, Any]] = []
    returns_by_candidate: dict[str, tuple[float, ...]] = {}
    periods_by_candidate: dict[str, float] = {}
    raw_p_values: dict[str, float] = {}
    groups: dict[str, list[str]] = {}
    planned_groups: dict[str, list[str]] = {}

    for plan, plan_path in zip(plans, paths, strict=True):
        experiment_id = experiment_id_for_plan(plan, phase="development")
        candidate_id = f"cand_{experiment_id}"
        planned_hypothesis_fingerprint = _planned_hypothesis_fingerprint(plan)
        planned_groups.setdefault(planned_hypothesis_fingerprint, []).append(candidate_id)
        experiment_dir = (resolved_experiment_root / experiment_id).resolve()
        try:
            experiment_dir.relative_to(resolved_experiment_root)
        except ValueError as exc:
            raise ValueError("campaign_experiment_path_outside_root") from exc
        return_path = experiment_dir / "development_return_series.json"
        entry: dict[str, Any] = {
            "plan_id": plan["plan_id"],
            "plan_ref": plan_path.resolve().relative_to(resolved_artifact_root).as_posix(),
            "plan_sha256": _sha256(plan_path),
            "source_experiment_id": plan["source_experiment_id"],
            "experiment_id": experiment_id,
            "candidate_id": candidate_id,
            "planned_hypothesis_fingerprint": planned_hypothesis_fingerprint,
            "capital_eligible": False,
        }
        if not return_path.is_file():
            entry.update(
                {
                    "status": "experiment_unavailable",
                    "reason_codes": ["development_return_series_missing"],
                    "raw_p_value": 1.0,
                }
            )
            raw_p_values[candidate_id] = 1.0
            entries.append(entry)
            continue
        return_payload = _load_mapping(return_path, "development_return_series")
        returns, dataset_fingerprint, periods_per_year = _validate_return_series(
            return_payload,
            experiment_id=experiment_id,
        )
        block_size = max(2, min(20, len(returns) // 10))
        bootstrap = moving_block_bootstrap_mean(
            returns,
            block_size=block_size,
            replications=replications,
            confidence_level=1.0 - alpha,
            seed=seed,
        )
        hypothesis_fingerprint = _hypothesis_fingerprint(
            plan,
            dataset_fingerprint=dataset_fingerprint,
        )
        candidate_ready, candidate_reason, candidate_sha = _validate_success_candidate(
            experiment_dir,
            experiment_id=experiment_id,
            candidate_id=candidate_id,
            dataset_fingerprint=dataset_fingerprint,
        )
        entry.update(
            {
                "status": "evidence_validated",
                "reason_codes": ([candidate_reason] if candidate_reason is not None else []),
                "candidate_ready": candidate_ready,
                "dataset_fingerprint": dataset_fingerprint,
                "hypothesis_fingerprint": hypothesis_fingerprint,
                "return_series_ref": return_path.relative_to(resolved_experiment_root).as_posix(),
                "return_series_sha256": _sha256(return_path),
                "candidate_artifact_sha256": candidate_sha,
                "valid_sample_count": len(returns),
                "block_size": block_size,
                "raw_p_value": bootstrap.one_sided_p_value,
            }
        )
        returns_by_candidate[candidate_id] = returns
        periods_by_candidate[candidate_id] = periods_per_year
        raw_p_values[candidate_id] = bootstrap.one_sided_p_value
        groups.setdefault(hypothesis_fingerprint, []).append(candidate_id)
        entries.append(entry)

    representative_by_group = {
        fingerprint: sorted(candidate_ids)[0]
        for fingerprint, candidate_ids in groups.items()
    }
    evidence_by_candidate: dict[str, dict[str, Any]] = {}
    for entry in entries:
        candidate_id = str(entry["candidate_id"])
        fingerprint = entry.get("hypothesis_fingerprint")
        if fingerprint is None:
            continue
        representative = representative_by_group[str(fingerprint)]
        entry["hypothesis_group_size"] = len(groups[str(fingerprint)])
        entry["representative_candidate_id"] = representative
        if candidate_id != representative:
            entry["status"] = "duplicate_hypothesis"
            entry["reason_codes"].append("duplicate_hypothesis_not_independent_trial")
            continue
        returns = returns_by_candidate[candidate_id]
        initial_train_size = max(20, len(returns) // 2)
        test_size = max(10, len(returns) // 10)
        splits = build_purged_walk_forward_splits(
            len(returns),
            initial_train_size=initial_train_size,
            test_size=test_size,
            purge_size=1,
            embargo_size=1,
        )
        walk_forward = evaluate_walk_forward(returns, splits)
        statistical = evaluate_statistical_evidence(
            returns,
            candidate_id=candidate_id,
            candidate_p_values=raw_p_values,
            trial_count=len(plans),
            periods_per_year=periods_by_candidate[candidate_id],
            block_size=int(entry["block_size"]),
            replications=replications,
            alpha=alpha,
            min_deflated_sharpe_probability=min_deflated_sharpe_probability,
            seed=seed,
        )
        passed = bool(entry["candidate_ready"] and walk_forward.passed and statistical.passed)
        evidence = {
            "schema_version": "research_candidate_campaign_member_v1",
            "candidate_id": candidate_id,
            "experiment_id": entry["experiment_id"],
            "dataset_fingerprint": entry["dataset_fingerprint"],
            "hypothesis_fingerprint": fingerprint,
            "selection_protocol_version": SELECTION_PROTOCOL,
            "benchmark_segment": "valid",
            "trial_count": len(plans),
            "unique_hypothesis_count": len(groups),
            "walk_forward": walk_forward.to_dict(),
            "statistics": statistical.to_dict(),
            "passed": passed,
            "capital_eligible": False,
            "source_return_series_ref": entry["return_series_ref"],
            "source_return_series_sha256": entry["return_series_sha256"],
            "authorization_boundary": (
                "development statistics only; holdout sealed; execution realism, "
                "calibration and forward paper evidence still required"
            ),
        }
        evidence_by_candidate[candidate_id] = evidence
        entry["statistics_passed"] = statistical.passed
        entry["walk_forward_passed"] = walk_forward.passed
        entry["status"] = "representative_statistics_pass" if passed else "representative_statistics_fail"
        if not entry["candidate_ready"]:
            entry["reason_codes"].append("development_candidate_not_ready")
        entry["reason_codes"].extend(walk_forward.reason_codes)
        entry["reason_codes"].extend(statistical.reason_codes)
        entry["reason_codes"] = sorted(set(entry["reason_codes"]))

    representative_entries = [
        entry
        for entry in entries
        if entry["status"].startswith("representative_statistics_")
    ]
    summary = {
        "schema_version": CAMPAIGN_SCHEMA,
        "state": "complete",
        "selection_protocol_version": SELECTION_PROTOCOL,
        "benchmark_segment": "valid",
        "holdout_status": "sealed_not_evaluated",
        "plan_count": len(plans),
        "trial_count": len(plans),
        "return_series_available_count": len(returns_by_candidate),
        "unique_hypothesis_count": len(groups),
        "planned_unique_hypothesis_count": len(planned_groups),
        "duplicate_trial_count": sum(max(0, len(items) - 1) for items in groups.values()),
        "planned_duplicate_trial_count": sum(
            max(0, len(items) - 1) for items in planned_groups.values()
        ),
        "representative_count": len(representative_entries),
        "representative_pass_count": sum(
            entry["status"] == "representative_statistics_pass"
            for entry in representative_entries
        ),
        "raw_p_values": raw_p_values,
        "hypothesis_groups": {
            fingerprint: {
                "representative_candidate_id": representative_by_group[fingerprint],
                "candidate_ids": sorted(candidate_ids),
            }
            for fingerprint, candidate_ids in sorted(groups.items())
        },
        "planned_hypothesis_groups": {
            fingerprint: {"candidate_ids": sorted(candidate_ids)}
            for fingerprint, candidate_ids in sorted(planned_groups.items())
        },
        "entries": entries,
        "statistical_config": {
            "replications": replications,
            "alpha": alpha,
            "min_deflated_sharpe_probability": min_deflated_sharpe_probability,
            "seed": seed,
        },
        "capital_eligible": False,
        "evaluated_at": datetime.now(UTC).isoformat(),
        "authorization_boundary": (
            "development-only campaign; no holdout access, runtime parameter write, "
            "order submission or live-trading authorization"
        ),
    }
    return summary, evidence_by_candidate


def write_campaign(
    summary: Mapping[str, Any],
    evidence_by_candidate: Mapping[str, Mapping[str, Any]],
    *,
    output_root: Path,
) -> str:
    targets = [
        output_root / "candidates" / f"{candidate_id}.json"
        for candidate_id in sorted(evidence_by_candidate)
    ]
    summary_path = output_root / "campaign_evidence.json"
    targets.append(summary_path)
    existing = [path for path in targets if path.exists()]
    if existing:
        raise FileExistsError(f"campaign_output_exists:{existing[0].as_posix()}")
    for candidate_id, evidence in sorted(evidence_by_candidate.items()):
        immutable_json_write(
            evidence,
            output_root / "candidates" / f"{candidate_id}.json",
        )
    return immutable_json_write(summary, summary_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--plan-root",
        type=Path,
        default=Path("artifacts/research/research_factory/replay_plans"),
    )
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=Path("artifacts/research/research_factory"),
    )
    parser.add_argument(
        "--experiment-root",
        type=Path,
        default=Path("artifacts/research/research_factory/experiments"),
    )
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--replications", type=int, default=2_000)
    parser.add_argument("--alpha", type=float, default=0.05)
    parser.add_argument("--min-deflated-sharpe-probability", type=float, default=0.95)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        args.output_root.resolve().relative_to(args.artifact_root.resolve())
    except ValueError as exc:
        parser.error(f"--output-root must be inside --artifact-root: {exc}")
    summary, evidence = evaluate_campaign(
        plan_root=args.plan_root,
        artifact_root=args.artifact_root,
        experiment_root=args.experiment_root,
        replications=args.replications,
        alpha=args.alpha,
        min_deflated_sharpe_probability=args.min_deflated_sharpe_probability,
        seed=args.seed,
    )
    digest = write_campaign(summary, evidence, output_root=args.output_root)
    print(
        json.dumps(
            {
                "output": (args.output_root / "campaign_evidence.json").as_posix(),
                "sha256": digest,
                "plan_count": summary["plan_count"],
                "unique_hypothesis_count": summary["unique_hypothesis_count"],
                "representative_pass_count": summary["representative_pass_count"],
                "capital_eligible": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
