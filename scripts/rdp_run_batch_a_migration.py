#!/usr/bin/env python3
"""Execute an RDP Batch A DB hardening migration stage.

USAGE:
  # Stage 4.4.1 — orphan report (read-only, safe on prod):
  .venv\\Scripts\\python.exe scripts/rdp_run_batch_a_migration.py --stage orphan_report

  # Stage 4.4.2 / 4.4.3 / 4.4.4 dry-run (parse SQL, don't execute):
  .venv\\Scripts\\python.exe scripts/rdp_run_batch_a_migration.py --stage fks --dry-run

  # Stage execution (requires --confirm-prod):
  .venv\\Scripts\\python.exe scripts/rdp_run_batch_a_migration.py --stage fks --confirm-prod

  # Emergency rollback:
  .venv\\Scripts\\python.exe scripts/rdp_run_batch_a_migration.py --stage rollback --confirm-prod

See: docs/task/rdp_hardening_batch_a_detailed_design.md §4 for the full migration plan.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rdp_batch_a_migration")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="RDP Batch A DB hardening migration runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--stage",
        required=True,
        choices=["orphan_report", "fks", "uqs", "checks", "rollback"],
        help="Which migration stage to run",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="For DDL stages: parse SQL but do not execute. No effect on orphan_report (already read-only).",
    )
    parser.add_argument(
        "--confirm-prod",
        action="store_true",
        help="Required flag to actually execute DDL stages (fks/uqs/checks/rollback). "
             "Without it the script refuses to mutate the database.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output structured JSON instead of human text.",
    )
    args = parser.parse_args()

    ddl_stages = {"fks", "uqs", "checks", "rollback"}
    if args.stage in ddl_stages and not args.dry_run and not args.confirm_prod:
        log.error(
            "stage %r mutates the database; pass --dry-run to parse SQL only, "
            "or --confirm-prod to execute it.",
            args.stage,
        )
        return 2

    from aats.data_platform.config import get_settings
    from aats.data_platform.db import apply_batch_a_migrations

    settings = get_settings()
    target = settings.database_url.split("@")[-1]
    log.info("running stage=%s dry_run=%s against %s", args.stage, args.dry_run, target)

    result = apply_batch_a_migrations(settings, stage=args.stage, dry_run=args.dry_run)

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        if args.stage == "orphan_report":
            print(result["text"])
        else:
            for k, v in result.items():
                print(f"{k}: {v}")

    if args.stage == "orphan_report" and not result.get("is_clean", False):
        log.error("orphan_report DIRTY — do NOT proceed to stage 4.4.2 until triaged")
        return 1

    log.info("stage %s completed.", args.stage)
    return 0


if __name__ == "__main__":
    sys.exit(main())
