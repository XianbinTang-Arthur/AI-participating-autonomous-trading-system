#!/usr/bin/env python3
"""Run a research-only governance workflow from Gold replay to pre-apply review."""

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
from aats.data_platform.research_factory.proposals import (  # noqa: E402
    FactorDSLProposal,
    load_factor_dsl_proposal,
)
from aats.data_platform.research_factory.workflow import (  # noqa: E402
    ResearchGovernanceWorkflowConfig,
    run_research_governance_workflow,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a research-only governance review workflow."
    )
    parser.add_argument("--symbol", required=True, help="Gold replay symbol, e.g. BTC-USDT-SWAP.")
    parser.add_argument("--timeframe", required=True, help="Gold replay timeframe, e.g. 15m or 1h.")
    parser.add_argument("--start", required=True, help="UTC start timestamp or date.")
    parser.add_argument("--end", required=True, help="UTC end timestamp or date.")
    parser.add_argument("--research-profile", required=True, help="Required ResearchProfile policy name.")
    parser.add_argument(
        "--allow-smoke-profile",
        action="store_true",
        help="Explicitly allow the smoke ResearchProfile for smoke/test workflows.",
    )
    parser.add_argument(
        "--observation-summary",
        type=Path,
        required=True,
        help="Read-only shadow/paper observation summary JSON under artifacts/research.",
    )
    parser.add_argument("--observation-source-type", choices=("shadow", "paper"), default=None)
    parser.add_argument("--observation-id", default=None)
    parser.add_argument("--workflow-id", default=None)
    parser.add_argument("--workflow-root", type=Path, default=None)
    parser.add_argument(
        "--factor-expression",
        default=None,
        help="Safe factor DSL expression. Optional when --factor-proposal is provided.",
    )
    parser.add_argument(
        "--factor-proposal",
        type=Path,
        default=None,
        help="Proposal-only JSON artifact with hypothesis, factor_expression, and rationale.",
    )
    parser.add_argument("--label-horizon-bars", type=int, default=1)
    parser.add_argument("--dataset-version", default=None)
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_EXPERIMENT_ARTIFACT_ROOT)
    parser.add_argument("--experiment-id", default=None)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--fee-bps", type=float, default=5.0)
    parser.add_argument("--slippage-bps", type=float, default=2.0)
    parser.add_argument("--funding-bps", type=float, default=0.5)
    parser.add_argument("--periods-per-year", type=float, default=None)
    parser.add_argument(
        "--execution-cost-summary",
        type=Path,
        required=True,
        help="Execution evidence summary JSON under artifacts/research.",
    )
    parser.add_argument(
        "--registry-path",
        type=Path,
        default=None,
        help="Optional research memory JSONL path under artifacts/research.",
    )
    parser.add_argument(
        "--database-url-env",
        default="RDP_DATABASE_URL",
        help="Environment variable containing the research DB URL. .env files are not read.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    database_url = os.environ.get(args.database_url_env, "").strip()
    if not database_url:
        _print_failure(
            f"database URL environment variable {args.database_url_env!r} is required; .env files are not read"
        )
        return 2
    run_timestamp = datetime.now(UTC)
    try:
        proposal, factor_expression = _resolve_factor_input(args, created_at=run_timestamp)
    except Exception as exc:
        _print_failure(str(exc))
        return 2

    engine = create_engine(database_url, pool_pre_ping=True)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with session_factory() as session:
            experiment_config = ResearchFactoryExperimentConfig(
                symbol=args.symbol,
                timeframe=args.timeframe,
                start=_parse_utc_datetime(args.start),
                end=_parse_utc_datetime(args.end),
                factor_expression=factor_expression,
                proposal=proposal,
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
            )
            result = run_research_governance_workflow(
                ResearchGovernanceWorkflowConfig(
                    experiment_config=experiment_config,
                    observation_summary_path=args.observation_summary,
                    workflow_id=args.workflow_id,
                    observation_source_type=args.observation_source_type,
                    observation_id=args.observation_id,
                    workflow_root=args.workflow_root,
                    registry_path=args.registry_path,
                    allow_smoke_profile=args.allow_smoke_profile,
                    timestamp=run_timestamp,
                ),
                data_source=GoldReplayDataSource(session),
            )
    except Exception as exc:
        _print_failure(str(exc))
        return 2
    finally:
        engine.dispose()

    print(result.to_json(), end="")
    return 0 if result.status == "preapply_review_pending" else 1


def _resolve_factor_input(args: argparse.Namespace, *, created_at: datetime) -> tuple[FactorDSLProposal | None, str]:
    proposal = None
    factor_expression = args.factor_expression.strip() if isinstance(args.factor_expression, str) else None
    if args.factor_proposal is not None:
        proposal = load_factor_dsl_proposal(
            args.factor_proposal,
            research_root=args.artifact_root,
            created_at=created_at,
        )
        if factor_expression is None:
            factor_expression = proposal.factor_expression
        elif factor_expression != proposal.factor_expression:
            raise ValueError("--factor-expression must match --factor-proposal factor_expression")
    if not factor_expression:
        raise ValueError("either --factor-expression or --factor-proposal is required")
    return proposal, factor_expression


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


def _print_failure(error: str) -> None:
    print(
        json.dumps(
            {"status": "failed", "error": error},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
