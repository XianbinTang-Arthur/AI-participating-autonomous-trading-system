from __future__ import annotations

from aats.bootstrap.logging import configure_logging, get_logger
from aats.bootstrap.settings import AATSSettings


def main() -> None:
    settings = AATSSettings()
    configure_logging(settings.log_level)
    logger = get_logger("apps.market_gateway")
    logger.info("Market gateway app placeholder. Use scripts/seed_demo_data.py for local snapshot seeding.")


if __name__ == "__main__":
    main()

