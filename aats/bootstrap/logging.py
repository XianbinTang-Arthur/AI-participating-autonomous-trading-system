from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

CORRELATION_FIELD_ORDER = ("decision_id", "intent_id", "order_id", "fill_id")


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def correlation_fields(
    *,
    decision_id: str | None = None,
    intent_id: str | None = None,
    order_id: str | None = None,
    fill_id: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    rendered: dict[str, Any] = dict(fields)
    for key, value in (
        ("decision_id", decision_id),
        ("intent_id", intent_id),
        ("order_id", order_id),
        ("fill_id", fill_id),
    ):
        if value is not None:
            rendered[key] = value
    return rendered


def log_event(logger: logging.Logger, event_name: str, **fields: Any) -> None:
    level_name = str(fields.pop("level", "info")).lower()
    ordered_fields: list[tuple[str, Any]] = []
    seen: set[str] = set()
    for key in CORRELATION_FIELD_ORDER:
        if key in fields:
            ordered_fields.append((key, fields[key]))
            seen.add(key)
    for key in sorted(fields):
        if key in seen:
            continue
        ordered_fields.append((key, fields[key]))
    rendered_fields = " ".join(
        f"{key}={_render_value(value)}"
        for key, value in ordered_fields
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
