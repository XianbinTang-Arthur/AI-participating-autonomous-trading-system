from __future__ import annotations

from aats.bootstrap.logging import configure_logging, get_logger
from aats.bootstrap.settings import AATSSettings


def main() -> None:
    settings = AATSSettings()
    configure_logging(settings.log_level)
    logger = get_logger("apps.portfolio_service")
    logger.info("Portfolio service app placeholder. Portfolio state mutates from fill events in the local MVP.")


if __name__ == "__main__":
    main()

