#!/usr/bin/env python3
"""Generate immutable walk-forward and statistical evidence from net returns."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from aats.data_platform.governance._atomic_io import immutable_json_write
from aats.data_platform.research_factory.validation.statistics import (
    build_purged_walk_forward_splits,
    evaluate_statistical_evidence,
    evaluate_walk_forward,
)


def _load_input(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("statistics_input_must_be_object")
    forbidden = {"password", "secret", "token", "database_url", "api_key"}
    if any(any(marker in str(key).lower() for marker in forbidden) for key in payload):
        raise ValueError("statistics_input_contains_forbidden_secret_key")
    return dict(payload)


def evaluate_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    candidate_id = str(payload["candidate_id"])
    dataset_fingerprint = str(payload["dataset_fingerprint"])
    returns = [float(value) for value in payload["net_returns"]]
    split_config = payload.get("walk_forward") or {}
    stats_config = payload.get("statistics") or {}
    if not isinstance(split_config, Mapping) or not isinstance(stats_config, Mapping):
        raise ValueError("walk_forward_and_statistics_configs_must_be_objects")
    splits = build_purged_walk_forward_splits(
        len(returns),
        initial_train_size=int(split_config.get("initial_train_size", max(20, len(returns) // 2))),
        test_size=int(split_config.get("test_size", max(10, len(returns) // 10))),
        purge_size=int(split_config.get("purge_size", 1)),
        embargo_size=int(split_config.get("embargo_size", 1)),
    )
    walk_forward = evaluate_walk_forward(
        returns,
        splits,
        min_positive_fold_ratio=float(split_config.get("min_positive_fold_ratio", 0.6)),
        max_fold_drawdown=float(split_config.get("max_fold_drawdown", 0.2)),
    )
    raw_p_values = payload.get("candidate_p_values")
    if not isinstance(raw_p_values, Mapping):
        raise ValueError("candidate_p_values_must_be_object")
    statistical = evaluate_statistical_evidence(
        returns,
        candidate_id=candidate_id,
        candidate_p_values={str(key): float(value) for key, value in raw_p_values.items()},
        trial_count=int(payload["trial_count"]),
        periods_per_year=float(payload["periods_per_year"]),
        block_size=int(stats_config.get("block_size", max(2, min(20, len(returns) // 10)))),
        replications=int(stats_config.get("replications", 2_000)),
        alpha=float(stats_config.get("alpha", 0.05)),
        min_deflated_sharpe_probability=float(
            stats_config.get("min_deflated_sharpe_probability", 0.95)
        ),
        seed=int(stats_config.get("seed", 0)),
    )
    return {
        "format_version": 1,
        "candidate_id": candidate_id,
        "dataset_fingerprint": dataset_fingerprint,
        "selection_protocol_version": "train_valid_selection_test_holdout_v2",
        "benchmark_segment": "valid",
        "walk_forward": walk_forward.to_dict(),
        "statistics": statistical.to_dict(),
        "passed": walk_forward.passed and statistical.passed,
        "evaluated_at": datetime.now(UTC).isoformat(),
        "authorization_boundary": (
            "development statistics only; no holdout access or live-trading authorization"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate_payload(_load_input(args.input))
    digest = immutable_json_write(result, args.output)
    print(
        json.dumps(
            {
                "output": args.output.as_posix(),
                "sha256": digest,
                "passed": result["passed"],
                "candidate_id": result["candidate_id"],
            },
            ensure_ascii=False,
        )
    )
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
