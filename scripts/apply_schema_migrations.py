#!/usr/bin/env python3
"""Apply and validate the root + RDP schema chains as one deployment job."""

from __future__ import annotations

import json


def main() -> int:
    from aats.bootstrap.config import load_settings
    from aats.data_platform.db import apply_rdp_migrations, reset_engine
    from aats.storage.session import (
        apply_current_migrations,
        create_database_runtime,
        create_schema,
        validate_current_migrations,
        validate_runtime_schema,
    )

    settings = load_settings()
    if settings.storage_mode != "postgres" or not settings.database_url:
        raise RuntimeError("schema_migration_job_requires_postgres_storage")

    runtime = create_database_runtime(settings.database_url)
    try:
        create_schema(runtime)
        root_applied = apply_current_migrations(runtime)
        validate_current_migrations(runtime)
        validate_runtime_schema(runtime)

        rdp_report = apply_rdp_migrations()
        rdp_applied = [stage.stage for stage in rdp_report.stages if stage.applied]
        rdp_existing = [stage.stage for stage in rdp_report.stages if not stage.applied]
        print(
            json.dumps(
                {
                    "status": "schema_migrations_current",
                    "root_applied": root_applied,
                    "rdp_applied": rdp_applied,
                    "rdp_already_applied": rdp_existing,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    finally:
        runtime.dispose()
        reset_engine()


if __name__ == "__main__":
    raise SystemExit(main())
