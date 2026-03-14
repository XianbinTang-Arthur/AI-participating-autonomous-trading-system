from __future__ import annotations

from aats.bootstrap.logging import configure_logging, get_logger
from aats.bootstrap.settings import AATSSettings


def main() -> None:
    settings = AATSSettings()
    configure_logging(settings.log_level)
    logger = get_logger("apps.execution_engine")
    logger.info("Execution engine app placeholder. Paper execution runs inside the integrated local runtime.")


if __name__ == "__main__":
    main()

