from __future__ import annotations

from aats.bootstrap.logging import configure_logging, get_logger
from aats.bootstrap.settings import AATSSettings


def main() -> None:
    settings = AATSSettings()
    configure_logging(settings.log_level)
    logger = get_logger("apps.ai_service")
    logger.info("AI service app placeholder. Stub inference is called from the decision engine in this MVP.")


if __name__ == "__main__":
    main()

