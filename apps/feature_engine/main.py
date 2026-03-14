from __future__ import annotations

from aats.bootstrap.logging import configure_logging, get_logger
from aats.bootstrap.settings import AATSSettings


def main() -> None:
    settings = AATSSettings()
    configure_logging(settings.log_level)
    logger = get_logger("apps.feature_engine")
    logger.info("Feature engine app placeholder. Local MVP wiring is exercised via scripts/run_local.py.")


if __name__ == "__main__":
    main()

