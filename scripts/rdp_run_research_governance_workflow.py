#!/usr/bin/env python3
"""Run the Research Factory governance workflow from Gold replay artifacts.

The CLI is research-only: it does not read .env files, does not write runtime
configuration, does not mutate active parameters, and does not call OKX writes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aats.data_platform.research_factory.real_data import (  # noqa: E402
    DEFAULT_EXPERIMENT_ARTIFACT_ROOT,
    GoldReplayDataSource,
    ResearchFactoryExperimentConfig,
)
from aats.data_platform.research_factory.workflow import (  # noqa: E402
    ResearchGovernanceWorkflowConfig,
    run_research_governance_workflow,
)


class JsonErrorArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that reports validation failures as JSON on stdout."""

    def error(self, message: str) -> None:
        _print_failure(message)
        raise SystemExit(2)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = JsonErrorArgumentParser(
        description=(
            "Run a research-only governance workflow. This does not execute "
            "dry-runs or apply runtime changes."
        )
    )
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--timeframe", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--factor-expression", required=True)
    parser.add_argument("--research-profile", required=True)
    parser.add_argument("--execution-cost-summary", type=Path, required=True)
    parser.add_argument("--observation-summary", type=Path, required=True)
    parser.add_argument("--observation-source-type", choices=("shadow", "paper"), required=True)
    parser.add_argument("--experiment-id", default=None)
    parser.add_argument("--workflow-id", default=None)
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=DEFAULT_EXPERIMENT_ARTIFACT_ROOT,
        help="Experiment artifact root under artifacts/research.",
    )
    parser.add_argument("--workflow-root", type=Path, default=None)
    parser.add_argument("--registry-path", type=Path, default=None)
    parser.add_argument("--label-horizon-bars", type=int, default=1)
    parser.add_argument("--dataset-version", default=None)
    parser.add_argument("--fee-bps", type=float, default=5.0)
    parser.add_argument("--slippage-bps", type=float, default=2.0)
    parser.add_argument("--funding-bps", type=float, default=0.5)
    parser.add_argument("--periods-per-year", type=float, default=None)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--allow-smoke-profile", action="store_true")
    parser.add_argument(
        "--database-url-env",
        default="RDP_DATABASE_URL",
        help="Environment variable containing the research DB URL. .env files are not read.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        database_url = os.environ.get(args.database_url_env, "").strip()
        if not database_url:
            raise ValueError(
                f"database URL environment variable {args.database_url_env!r} is required; "
                ".env files are not read"
            )
        run_timestamp = datetime.now(UTC)
        engine = create_engine(database_url, pool_pre_ping=True)
        session_factory = sessionmaker(bind=engine, expire_on_commit=False)
        try:
            with session_factory() as session:
                result = run_research_governance_workflow(
                    ResearchGovernanceWorkflowConfig(
                        experiment_config=ResearchFactoryExperimentConfig(
                            symbol=args.symbol,
                            timeframe=args.timeframe,
                            start=_parse_utc_datetime(args.start),
                            end=_parse_utc_datetime(args.end),
                            factor_expression=args.factor_expression,
                            research_profile=args.research_profile,
                            artifact_root=args.artifact_root,
                            experiment_id=args.experiment_id,
                            label_horizon_bars=args.label_horizon_bars,
                            dataset_version=args.dataset_version,
                            fee_bps=args.fee_bps,
                            slippage_bps=args.slippage_bps,
                            funding_bps=args.funding_bps,
                            periods_per_year=args.periods_per_year,
                            execution_cost_summary_path=args.execution_cost_summary,
                            require_execution_realism=True,
                            registry_path=args.registry_path,
                            overwrite=args.overwrite,
                            timestamp=run_timestamp,
                        ),
                        observation_summary_path=args.observation_summary,
                        workflow_id=args.workflow_id,
                        observation_source_type=args.observation_source_type,
                        workflow_root=args.workflow_root,
                        registry_path=args.registry_path,
                        allow_smoke_profile=args.allow_smoke_profile,
                        timestamp=run_timestamp,
                    ),
                    data_source=GoldReplayDataSource(session),
                )
        finally:
            engine.dispose()
    except SystemExit:
        raise
    except Exception as exc:
        _print_failure(str(exc))
        return 2

    payload = _result_payload(result)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", end="")
    return _workflow_exit_code(result.status)


def _parse_utc_datetime(value: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("datetime value must be a non-empty string")
    raw = value.strip()
    if len(raw) == 10:
        raw = f"{raw}T00:00:00+00:00"
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _workflow_exit_code(status: str) -> int:
    """Return process status without treating candidate verdicts as program errors."""
    if status in {"failed", "reference_integrity_failed"}:
        return 1
    return 0


def _result_payload(result) -> dict:
    raw = json.loads(result.to_json())
    if not isinstance(raw, dict):
        raise ValueError("workflow result JSON must be an object")
    workflow_dir = str(raw.get("workflow_dir") or "").strip()
    summary_ref = str(raw.get("workflow_summary_ref") or "workflow_summary.json").strip()
    workflow_summary = f"{workflow_dir}/{summary_ref}" if workflow_dir else summary_ref
    raw.setdefault("runtime_mutation_allowed", False)
    raw.setdefault("active_parameter_write_allowed", False)
    raw.setdefault("runtime_config_write_allowed", False)
    raw.setdefault("okx_write_allowed", False)
    raw.setdefault("dry_run_execution_allowed", False)
    raw.setdefault(
        "verdict_board_next_command",
        (
            "python scripts/rdp_update_candidate_verdict_board.py "
            f"--workflow-summary {workflow_summary}"
        ),
    )
    return raw


def _print_failure(error: str) -> None:
    print(
        json.dumps(
            {
                "status": "failed",
                "error": error,
                "runtime_mutation_allowed": False,
                "active_parameter_write_allowed": False,
                "runtime_config_write_allowed": False,
                "okx_write_allowed": False,
                "dry_run_execution_allowed": False,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
