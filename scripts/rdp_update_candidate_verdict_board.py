#!/usr/bin/env python3
"""Update the Research Factory candidate verdict board from workflow summaries."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aats.data_platform.research_factory.verdicts import (  # noqa: E402
    build_candidate_verdict_from_workflow,
    update_candidate_verdict_board,
)


class JsonErrorArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        _print_failure(message)
        raise SystemExit(2)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = JsonErrorArgumentParser(
        description=(
            "Update a research-only candidate verdict board from workflow summaries. "
            "This does not authorize apply or runtime mutation."
        )
    )
    parser.add_argument("--workflow-summary", type=Path, action="append", required=True)
    parser.add_argument(
        "--research-factory-root",
        type=Path,
        default=Path("artifacts") / "research" / "research_factory",
    )
    parser.add_argument(
        "--board-root",
        type=Path,
        default=None,
        help="Defaults to <research-factory-root>/verdicts.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        board_root = args.board_root or args.research_factory_root / "verdicts"
        verdicts = [
            build_candidate_verdict_from_workflow(
                workflow_summary_path,
                research_factory_root=args.research_factory_root,
            )
            for workflow_summary_path in args.workflow_summary
        ]
        jsonl_path, md_path = update_candidate_verdict_board(
            verdicts,
            board_root=board_root,
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
                "verdict_count": len(verdicts),
                "candidate_verdict_board_jsonl": jsonl_path.as_posix(),
                "candidate_verdict_board_md": md_path.as_posix(),
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
