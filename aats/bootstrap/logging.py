from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Lock
from collections.abc import Mapping
from typing import Any

CORRELATION_FIELD_ORDER = ("decision_id", "intent_id", "order_id", "fill_id")
LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
RUNTIME_LOG_NAME = "aats.log"
LEVEL_FILE_NAMES = {
    logging.DEBUG: ("debug", "debug.log"),
    logging.INFO: ("info", "info.log"),
    logging.WARNING: ("warning", "warning.log"),
    logging.ERROR: ("error", "error.log"),
}
PROPAGATING_LOGGERS = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "httpx",
    "websockets",
)
_CONFIGURE_LOCK = Lock()


class ExactLevelFilter(logging.Filter):
    def __init__(self, level: int) -> None:
        super().__init__()
        self.level = level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno == self.level


class MinimumLevelFilter(logging.Filter):
    def __init__(self, minimum_level: int) -> None:
        super().__init__()
        self.minimum_level = minimum_level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= self.minimum_level


def configure_logging(
    level: str = "INFO",
    *,
    log_dir: str = "logs",
    rotate_max_bytes: int = 5_242_880,
    backup_count: int = 7,
) -> None:
    resolved_level = getattr(logging, level.upper(), logging.INFO)
    base_path = Path(log_dir)

    with _CONFIGURE_LOCK:
        _ensure_log_directories(base_path)
        formatter = logging.Formatter(LOG_FORMAT)

        root_logger = logging.getLogger()
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)
            handler.close()

        root_logger.setLevel(resolved_level)
        root_logger.addHandler(_build_console_handler(resolved_level, formatter))
        root_logger.addHandler(
            _build_rotating_handler(
                base_path / "runtime" / RUNTIME_LOG_NAME,
                formatter=formatter,
                level=resolved_level,
                rotate_max_bytes=rotate_max_bytes,
                backup_count=backup_count,
            )
        )
        for log_level, (directory_name, file_name) in LEVEL_FILE_NAMES.items():
            root_logger.addHandler(
                _build_rotating_handler(
                    base_path / directory_name / file_name,
                    formatter=formatter,
                    level=resolved_level,
                    rotate_max_bytes=rotate_max_bytes,
                    backup_count=backup_count,
                    level_filter=(
                        MinimumLevelFilter(logging.ERROR)
                        if log_level == logging.ERROR
                        else ExactLevelFilter(log_level)
                    ),
                )
            )

        for logger_name in PROPAGATING_LOGGERS:
            logger = logging.getLogger(logger_name)
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()
            logger.propagate = True
            logger.setLevel(resolved_level)

        logging.captureWarnings(True)
        bootstrap_logger = get_logger("aats.bootstrap")
        bootstrap_logger.info(
            "logging_configured level=%s log_dir=%s rotate_max_bytes=%s backup_count=%s",
            logging.getLevelName(resolved_level),
            str(base_path),
            rotate_max_bytes,
            backup_count,
        )


def configure_logging_for_settings(settings: Any) -> None:
    configure_logging(
        level=getattr(settings, "log_level", "INFO"),
        log_dir=getattr(settings, "log_dir", "logs"),
        rotate_max_bytes=getattr(settings, "log_rotate_max_bytes", 5_242_880),
        backup_count=getattr(settings, "log_backup_count", 7),
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


def _ensure_log_directories(base_path: Path) -> None:
    (base_path / "runtime").mkdir(parents=True, exist_ok=True)
    for directory_name, _ in LEVEL_FILE_NAMES.values():
        (base_path / directory_name).mkdir(parents=True, exist_ok=True)


def _build_console_handler(level: int, formatter: logging.Formatter) -> logging.Handler:
    handler = logging.StreamHandler()
    handler.setLevel(level)
    handler.setFormatter(formatter)
    return handler


def _build_rotating_handler(
    path: Path,
    *,
    formatter: logging.Formatter,
    level: int,
    rotate_max_bytes: int,
    backup_count: int,
    level_filter: logging.Filter | None = None,
) -> logging.Handler:
    handler = RotatingFileHandler(
        path,
        maxBytes=rotate_max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setLevel(level)
    handler.setFormatter(formatter)
    if level_filter is not None:
        handler.addFilter(level_filter)
    return handler
