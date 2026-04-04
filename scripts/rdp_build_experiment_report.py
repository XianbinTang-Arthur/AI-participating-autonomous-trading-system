#!/usr/bin/env python3
"""Build report for an existing experiment.

Usage:
    python scripts/rdp_build_experiment_report.py --experiment-id <UUID>
"""

from __future__ import annotations

import argparse
import json
import logging
import pathlib
import sys
from uuid import UUID

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rdp_build_report")

_ARTIFACT_ROOT = pathlib.Path("artifacts/research/experiments")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build experiment report")
    parser.add_argument("--experiment-id", required=True, help="Experiment UUID")
    parser.add_argument("--ensure-schema", action="store_true",
                        help="Run DB migrations before building report")
    args = parser.parse_args()

    from aats.data_platform.config import get_settings
    from aats.data_platform.db import get_session, run_migrations
    from aats.data_platform.replay.registry.experiment_registry import (
        get_experiment,
        mark_experiment_succeeded,
    )
    from aats.data_platform.replay.reports.markdown_report_builder import build_experiment_report

    settings = get_settings()
    if args.ensure_schema:
        log.info("Running migrations (--ensure-schema)...")
        run_migrations(settings)

    exp_id = UUID(args.experiment_id)

    with get_session(settings) as session:
        exp = get_experiment(session, exp_id)
        if exp is None:
            print(f"Experiment {exp_id} not found")
            sys.exit(1)

        # 尝试读取已有的 diagnostics
        summary_path = exp.get("summary_path")
        if summary_path and pathlib.Path(summary_path).exists():
            diag = json.loads(pathlib.Path(summary_path).read_text(encoding="utf-8"))
        else:
            print(f"No diagnostics found for experiment {exp_id}")
            sys.exit(1)

        exp_dir = _ARTIFACT_ROOT / str(exp_id)
        report_path = build_experiment_report(
            experiment_info=exp,
            diagnostics=diag,
            output_path=exp_dir / "report.md",
        )

        # 更新 registry
        mark_experiment_succeeded(session, exp_id, report_path=str(report_path))
        session.commit()

        print(f"Report generated: {report_path}")


if __name__ == "__main__":
    main()
