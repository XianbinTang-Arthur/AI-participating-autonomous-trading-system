from __future__ import annotations

from aats.bootstrap.logging import configure_logging, get_logger
from aats.bootstrap.settings import AATSSettings


def main() -> None:
    settings = AATSSettings()
    configure_logging(settings.log_level)
    logger = get_logger("apps.reconciliation_service")
    logger.info("Reconciliation service app placeholder. Local reconciliation is triggered by portfolio snapshots.")


if __name__ == "__main__":
    main()

