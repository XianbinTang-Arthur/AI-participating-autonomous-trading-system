#!/usr/bin/env python3
"""Initialize the Research Data Platform database.

Creates the aats_research database (if needed) and runs all migrations.

Usage:
    python scripts/rdp_init_db.py
"""

from __future__ import annotations

import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("rdp_init_db")


def main() -> None:
    from aats.data_platform.config import get_settings
    from aats.data_platform.db import run_migrations

    settings = get_settings()
    log.info("Running research DB migrations against: %s", settings.database_url.split("@")[-1])
    run_migrations(settings)
    log.info("Research database initialized successfully.")


if __name__ == "__main__":
    main()
