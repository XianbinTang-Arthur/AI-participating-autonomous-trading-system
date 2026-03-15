from __future__ import annotations

from aats.bootstrap.logging import configure_logging_for_settings, get_logger
from aats.bootstrap.settings import AATSSettings


def main() -> None:
    settings = AATSSettings()
    configure_logging_for_settings(settings)
    logger = get_logger("apps.governance_engine")
    logger.info("Governance engine app placeholder. Policy and risk checks run inside the local MVP runtime.")


if __name__ == "__main__":
    main()
