from __future__ import annotations

import json
import logging
import os
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

# ── JSON 格式化 ─────────────────────────────────────────────────────
# AATS_LOG_FORMAT=json 时启用；输出单行 JSON，字段对齐 Grafana Loki
# pipeline 的 json stage 解析。字段清单：
#   timestamp  — ISO 格式时间戳
#   level      — 日志级别（大写）
#   logger     — logger 名称
#   message    — 日志正文
#   trace_id   — OTel trace ID（32 位 hex，无 span 时为 "0"×32）
#   span_id    — OTel span ID（16 位 hex，无 span 时为 "0"×16）
#   process_role — AATS 进程角色（gateway/market/decision/execution）
#   event_name — log_event() 输出时的事件名（嵌在 message 首段）
# ────────────────────────────────────────────────────────────────────

_ZERO_TRACE_ID = "0" * 32
_ZERO_SPAN_ID = "0" * 16


class _OTelTraceFilter(logging.Filter):
    """给每条 LogRecord 注入 otelTraceID / otelSpanID。

    从 opentelemetry.trace.get_current_span() 动态读取当前 trace context。
    OTel 未安装时降级为全零（与 telemetry.py 的 _NoopTracer 策略一致）。
    不引入额外依赖，不做 auto-instrumentation。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            from opentelemetry import trace

            span = trace.get_current_span()
            ctx = span.get_span_context()
            if ctx and ctx.trace_id:
                record.otelTraceID = format(ctx.trace_id, "032x")  # type: ignore[attr-defined]
                record.otelSpanID = format(ctx.span_id, "016x")  # type: ignore[attr-defined]
            else:
                record.otelTraceID = _ZERO_TRACE_ID  # type: ignore[attr-defined]
                record.otelSpanID = _ZERO_SPAN_ID  # type: ignore[attr-defined]
        except Exception:
            record.otelTraceID = _ZERO_TRACE_ID  # type: ignore[attr-defined]
            record.otelSpanID = _ZERO_SPAN_ID  # type: ignore[attr-defined]
        return True


class _JSONFormatter(logging.Formatter):
    """单行 JSON 日志格式化器。

    输出字段与 Promtail 的 json pipeline stage 对齐：
    Promtail 从 Docker json-file 日志中提取 AATS 的 JSON log line，
    然后按 level / process_role 作为 Loki label 索引，
    trace_id / event_name 走正文搜索或 Grafana derived field。
    """

    def __init__(self) -> None:
        super().__init__()
        self._process_role = os.environ.get("AATS_PROCESS_ROLE", "monolith")

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S.%f"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "process_role": self._process_role,
            "trace_id": getattr(record, "otelTraceID", _ZERO_TRACE_ID),
            "span_id": getattr(record, "otelSpanID", _ZERO_SPAN_ID),
        }
        # event_name: 仅 log_event() 调用时存在，便于 Loki 用 | json 精确筛选
        event_name = getattr(record, "event_name", None)
        if event_name is not None:
            entry["event_name"] = event_name
        if record.exc_info and record.exc_info[1] is not None:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # noqa: N802
        """ISO 8601 毫秒精度时间戳。"""
        from datetime import datetime, timezone

        dt = datetime.fromtimestamp(record.created, tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(record.msecs):03d}Z"


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

    # AATS_LOG_FORMAT: "json" → 结构化 JSON（Loki/Promtail 友好）
    #                  "text" / 其它 → 传统纯文本
    log_format = os.environ.get("AATS_LOG_FORMAT", "text").lower().strip()
    use_json = log_format == "json"

    with _CONFIGURE_LOCK:
        _ensure_log_directories(base_path)

        if use_json:
            formatter: logging.Formatter = _JSONFormatter()
        else:
            formatter = logging.Formatter(LOG_FORMAT)

        root_logger = logging.getLogger()
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)
            handler.close()

        root_logger.setLevel(resolved_level)

        # trace_id / span_id 注入（JSON/text 都加，text 格式不显示但不影响）
        root_logger.addFilter(_OTelTraceFilter())

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
            "logging_configured level=%s log_dir=%s log_format=%s rotate_max_bytes=%s backup_count=%s",
            logging.getLevelName(resolved_level),
            str(base_path),
            log_format,
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
    log_method(message, extra={"event_name": event_name})


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
