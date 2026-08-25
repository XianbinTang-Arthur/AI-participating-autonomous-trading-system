#!/usr/bin/env python3
"""Run deterministic v2 candidate replay plans against research Gold bars.

The ``development`` phase intentionally produces non-capital-eligible evidence
without using the sealed holdout.  The ``evidence-complete`` phase additionally
requires a per-plan L2 execution-cost summary.  Neither phase applies runtime
parameters or submits orders.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from aats.data_platform.research_factory.real_data import (
    GoldReplayDataSource,
    ResearchFactoryExperimentConfig,
    run_research_factory_experiment,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("plan_datetime_must_be_timezone_aware")
    return parsed.astimezone(UTC)


def load_and_validate_plan(path: Path, *, artifact_root: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("replay_plan_must_be_object")
    plan = dict(payload)
    if plan.get("selection_protocol_version") != "train_valid_selection_test_holdout_v2":
        raise ValueError("replay_plan_protocol_not_v2")
    if plan.get("status") != "planned_not_run":
        raise ValueError("replay_plan_not_planned")
    root = artifact_root.resolve()
    source_candidate = (root / str(plan["source_candidate_ref"])).resolve()
    source_spec = (root / str(plan["source_experiment_spec_ref"])).resolve()
    for source in (source_candidate, source_spec):
        try:
            source.relative_to(root)
        except ValueError as exc:
            raise ValueError("replay_plan_source_outside_artifact_root") from exc
        if not source.is_file():
            raise ValueError(f"replay_plan_source_missing:{source.name}")
    if _sha256(source_candidate) != plan.get("source_candidate_sha256"):
        raise ValueError("replay_plan_candidate_sha256_mismatch")
    if _sha256(source_spec) != plan.get("source_experiment_spec_sha256"):
        raise ValueError("replay_plan_spec_sha256_mismatch")
    if not str(plan.get("symbol", "")).strip() or not str(plan.get("timeframe", "")).strip():
        raise ValueError("replay_plan_market_scope_missing")
    _parse_datetime(str(plan["start"]))
    _parse_datetime(str(plan["end"]))
    return plan


def experiment_id_for_plan(plan: Mapping[str, Any], *, phase: str) -> str:
    if phase not in {"development", "evidence-complete"}:
        raise ValueError("invalid_v2_batch_phase")
    phase_tag = "dev" if phase == "development" else "evidence"
    source = str(plan["source_experiment_id"])
    suffix = str(plan["plan_id"]).removeprefix("v2replay_")
    return f"{source}_v2_{phase_tag}_{suffix}"


def _summary_path(root: Path | None, plan_id: str) -> Path | None:
    return root / f"{plan_id}.json" if root is not None else None


def _validate_execution_summary(path: Path, *, plan: Mapping[str, Any]) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("execution_summary_must_be_object")
    if payload.get("model_version") != "l2_event_replay_v1":
        raise ValueError("execution_summary_not_l2_event_replay_v1")
    if payload.get("plan_id") != plan.get("plan_id"):
        raise ValueError("execution_summary_plan_id_mismatch")
    if str(payload.get("symbol", "")).upper() != str(plan["symbol"]).upper():
        raise ValueError("execution_summary_symbol_mismatch")
    if payload.get("benchmark_segment") != "valid":
        raise ValueError("execution_summary_must_use_valid_segment")
    if str(payload.get("timeframe")) != str(plan["timeframe"]):
        raise ValueError("execution_summary_timeframe_mismatch")


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
    parser.add_argument(
        "--phase",
        choices=("development", "evidence-complete"),
        default="development",
    )
    parser.add_argument("--execution-summary-root", type=Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.phase == "evidence-complete" and args.execution_summary_root is None:
        parser.error("--execution-summary-root is required for evidence-complete")

    paths = sorted(args.plan_root.glob("*.json"))
    if args.limit is not None:
        if args.limit <= 0:
            parser.error("--limit must be positive")
        paths = paths[: args.limit]
    if not paths:
        raise ValueError("v2_replay_plans_required")
    plans = [load_and_validate_plan(path, artifact_root=args.artifact_root) for path in paths]
    prepared: list[tuple[dict[str, Any], Path | None]] = []
    for plan in plans:
        summary = _summary_path(args.execution_summary_root, str(plan["plan_id"]))
        if args.phase == "evidence-complete":
            assert summary is not None
            if not summary.is_file():
                raise ValueError(f"execution_summary_missing:{plan['plan_id']}")
            _validate_execution_summary(summary, plan=plan)
        prepared.append((plan, summary))

    if args.dry_run:
        print(
            json.dumps(
                {
                    "phase": args.phase,
                    "validated_plan_count": len(prepared),
                    "database_accessed": False,
                    "holdout_accessed": False,
                    "runtime_parameters_written": False,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0

    database_url = os.environ.get("RDP_DATABASE_URL", "").strip()
    if not database_url:
        raise ValueError("RDP_DATABASE_URL_required_dotenv_not_read")
    engine = create_engine(database_url, pool_pre_ping=True)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    results: list[dict[str, Any]] = []
    timestamp = datetime.now(UTC)
    try:
        for plan, summary in prepared:
            experiment_id = experiment_id_for_plan(plan, phase=args.phase)
            try:
                with session_factory() as session:
                    result = run_research_factory_experiment(
                        ResearchFactoryExperimentConfig(
                            symbol=str(plan["symbol"]),
                            timeframe=str(plan["timeframe"]),
                            start=_parse_datetime(str(plan["start"])),
                            end=_parse_datetime(str(plan["end"])),
                            factor_expression=str(plan["factor_expression"]),
                            research_profile=(
                                "real_factor_development"
                                if args.phase == "development"
                                else str(plan["research_profile"])
                            ),
                            artifact_root=args.experiment_root,
                            experiment_id=experiment_id,
                            label_horizon_bars=int(plan["label_horizon_bars"]),
                            dataset_version=str(plan["dataset_version"]),
                            train_ratio=float(plan["train_ratio"]),
                            valid_ratio=float(plan["valid_ratio"]),
                            test_ratio=float(plan["test_ratio"]),
                            fee_bps=float(plan["fee_bps"]),
                            slippage_bps=float(plan["slippage_bps"]),
                            execution_cost_summary_path=summary,
                            require_execution_realism=args.phase == "evidence-complete",
                            timestamp=timestamp,
                        ),
                        data_source=GoldReplayDataSource(session),
                    )
                results.append(
                    {
                        "plan_id": plan["plan_id"],
                        "experiment_id": experiment_id,
                        "status": result.status,
                        "candidate_generated": result.candidate_generated,
                        "dataset_fingerprint": result.dataset_fingerprint,
                        "error": result.error,
                    }
                )
            except Exception as exc:
                results.append(
                    {
                        "plan_id": plan["plan_id"],
                        "experiment_id": experiment_id,
                        "status": "failed",
                        "candidate_generated": False,
                        "error_type": type(exc).__name__,
                    }
                )
    finally:
        engine.dispose()
    failed = sum(item["status"] != "succeeded" for item in results)
    print(
        json.dumps(
            {
                "phase": args.phase,
                "plan_count": len(results),
                "failed_count": failed,
                "holdout_accessed": False,
                "runtime_parameters_written": False,
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
