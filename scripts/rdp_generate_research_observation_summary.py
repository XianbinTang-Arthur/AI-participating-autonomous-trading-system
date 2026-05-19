#!/usr/bin/env python3
"""Generate a Research Factory observation summary from read-only event JSONL."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aats.data_platform.research_factory.observation_summary_generator import (  # noqa: E402
    build_observation_summary_from_events,
    load_observation_events_jsonl,
    write_observation_summary,
)


class JsonErrorArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that emits JSON failures for operator tooling."""

    def error(self, message: str) -> None:
        _print_failure(message)
        raise SystemExit(2)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = JsonErrorArgumentParser(
        description=(
            "Generate a research-only observation summary from shadow/paper "
            "event JSONL. This does not read .env files, write runtime config, "
            "place orders, or mutate active parameters."
        )
    )
    parser.add_argument("--events-jsonl", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--recommendation-id", required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--mode", choices=("shadow", "paper"), required=True)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        events = load_observation_events_jsonl(args.events_jsonl)
        summary = build_observation_summary_from_events(
            events,
            recommendation_id=args.recommendation_id,
            candidate_id=args.candidate_id,
            experiment_id=args.experiment_id,
            mode=args.mode,
            source_artifact_ref=_research_relative_ref(args.events_jsonl),
        )
        output_path = write_observation_summary(
            summary,
            args.output,
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
                "summary_path": str(output_path),
                "schema_version": summary.schema_version,
                "mode": summary.mode,
                "recommendation_id": summary.recommendation_id,
                "candidate_id": summary.candidate_id,
                "experiment_id": summary.experiment_id,
                "observed_bars": summary.observed_bars,
                "observed_events": summary.observed_events,
                "runtime_mutation_allowed": False,
                "active_parameter_write_allowed": False,
                "runtime_config_write_allowed": False,
                "okx_write_allowed": False,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _research_relative_ref(path: Path) -> str:
    resolved = Path(path).resolve(strict=False)
    parts = resolved.parts
    for index in range(len(parts) - 1):
        if parts[index] == "artifacts" and parts[index + 1] == "research":
            return Path(*parts[index + 2 :]).as_posix()
    return resolved.name


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
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
