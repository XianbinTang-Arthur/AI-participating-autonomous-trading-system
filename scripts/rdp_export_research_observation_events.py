#!/usr/bin/env python3
"""Export read-only shadow/paper event JSONL into Research Factory observation events."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aats.data_platform.research_factory.observation_event_exporter import (  # noqa: E402
    OBSERVATION_EVENT_EXPORTER_SCHEMA_VERSION,
    load_source_events_jsonl,
    normalize_source_events,
    write_observation_events_jsonl,
)


class JsonErrorArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that emits JSON failures for operator tooling."""

    def error(self, message: str) -> None:
        _print_failure(message)
        raise SystemExit(2)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = JsonErrorArgumentParser(
        description=(
            "Normalize already-exported shadow/paper event JSONL into canonical "
            "Research Factory observation events. This does not read .env files, "
            "query live runtime tables, write runtime config, place orders, execute "
            "dry-runs, or mutate active parameters."
        )
    )
    parser.add_argument("--source-events", type=Path, required=True)
    parser.add_argument("--output-events", type=Path, required=True)
    parser.add_argument("--recommendation-id", required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--mode", choices=("shadow", "paper"), required=True)
    parser.add_argument(
        "--source-kind",
        choices=("shadow_decision", "paper_intent", "observation_event"),
        required=True,
    )
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        source_events = load_source_events_jsonl(args.source_events)
        observation_events = normalize_source_events(
            source_events,
            recommendation_id=args.recommendation_id,
            candidate_id=args.candidate_id,
            experiment_id=args.experiment_id,
            mode=args.mode,
            source_kind=args.source_kind,
        )
        output_path = write_observation_events_jsonl(
            observation_events,
            args.output_events,
            overwrite=args.overwrite,
        )
    except SystemExit:
        raise
    except Exception as exc:
        _print_failure(str(exc))
        return 2

    print(
        json.dumps(
            {
                "status": "succeeded",
                "schema_version": OBSERVATION_EVENT_EXPORTER_SCHEMA_VERSION,
                "events_path": str(output_path),
                "source_event_count": len(source_events),
                "observation_event_count": len(observation_events),
                "recommendation_id": args.recommendation_id,
                "candidate_id": args.candidate_id,
                "experiment_id": args.experiment_id,
                "mode": args.mode,
                "source_kind": args.source_kind,
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
    return 0


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
