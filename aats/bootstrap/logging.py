from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(logger: logging.Logger, event_name: str, **fields: Any) -> None:
    level_name = str(fields.pop("level", "info")).lower()
    rendered_fields = " ".join(
        f"{key}={_render_value(value)}"
        for key, value in sorted(fields.items())
    )
    message = event_name if not rendered_fields else f"{event_name} {rendered_fields}"
    log_method = getattr(logger, level_name, logger.info)
    log_method(message)


def _render_value(value: Any) -> str:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return str(value)
    if isinstance(value, Mapping):
        return json.dumps(dict(value), sort_keys=True, default=str)
    return json.dumps(value, default=str)
