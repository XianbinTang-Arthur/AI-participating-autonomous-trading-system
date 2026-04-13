from __future__ import annotations

import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path

from aats.bootstrap.logging import (
    _JSONFormatter,
    _OTelTraceFilter,
    _ZERO_SPAN_ID,
    _ZERO_TRACE_ID,
    configure_logging,
    get_logger,
    log_event,
)


class LoggingSetupTests(unittest.TestCase):
    def test_configure_logging_creates_directories_and_writes_level_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            configure_logging(
                "DEBUG",
                log_dir=temp_dir,
                rotate_max_bytes=4096,
                backup_count=2,
            )
            logger = get_logger("aats.test.logging")
            logger.debug("debug-message")
            logger.info("info-message")
            logger.warning("warning-message")
            logger.error("error-message")

            for handler in logging.getLogger().handlers:
                handler.flush()

            base_path = Path(temp_dir)
            expected_directories = (
                base_path / "runtime",
                base_path / "debug",
                base_path / "info",
                base_path / "warning",
                base_path / "error",
            )
            for directory in expected_directories:
                self.assertTrue(directory.exists(), directory)

            runtime_log = (base_path / "runtime" / "aats.log").read_text(encoding="utf-8")
            debug_log = (base_path / "debug" / "debug.log").read_text(encoding="utf-8")
            info_log = (base_path / "info" / "info.log").read_text(encoding="utf-8")
            warning_log = (base_path / "warning" / "warning.log").read_text(encoding="utf-8")
            error_log = (base_path / "error" / "error.log").read_text(encoding="utf-8")

            self.assertIn("debug-message", runtime_log)
            self.assertIn("info-message", runtime_log)
            self.assertIn("warning-message", runtime_log)
            self.assertIn("error-message", runtime_log)
            self.assertIn("debug-message", debug_log)
            self.assertIn("info-message", info_log)
            self.assertIn("warning-message", warning_log)
            self.assertIn("error-message", error_log)

            root_logger = logging.getLogger()
            for handler in list(root_logger.handlers):
                root_logger.removeHandler(handler)
                handler.close()
            logging.shutdown()


# ── _JSONFormatter 测试 ──────────────────────────────────────────────

class TestJSONFormatter(unittest.TestCase):
    """验证 _JSONFormatter JSON 输出格式与字段完整性。"""

    def setUp(self) -> None:
        self.formatter = _JSONFormatter()

    def _make_record(
        self,
        msg: str = "test message",
        level: int = logging.INFO,
        name: str = "aats.test",
        exc_info: object = None,
    ) -> logging.LogRecord:
        record = logging.LogRecord(
            name=name, level=level, pathname="", lineno=0,
            msg=msg, args=(), exc_info=exc_info,
        )
        record.otelTraceID = _ZERO_TRACE_ID  # type: ignore[attr-defined]
        record.otelSpanID = _ZERO_SPAN_ID  # type: ignore[attr-defined]
        return record

    def test_output_is_valid_single_line_json(self) -> None:
        record = self._make_record()
        output = self.formatter.format(record)
        self.assertNotIn("\n", output)
        data = json.loads(output)
        self.assertIsInstance(data, dict)

    def test_required_fields_present(self) -> None:
        record = self._make_record()
        data = json.loads(self.formatter.format(record))
        for key in ("timestamp", "level", "logger", "message", "process_role", "trace_id", "span_id"):
            self.assertIn(key, data, f"missing field: {key}")

    def test_level_matches_record(self) -> None:
        for level in (logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR):
            record = self._make_record(level=level)
            data = json.loads(self.formatter.format(record))
            self.assertEqual(data["level"], logging.getLevelName(level))

    def test_message_content(self) -> None:
        record = self._make_record(msg="hello world")
        data = json.loads(self.formatter.format(record))
        self.assertEqual(data["message"], "hello world")

    def test_timestamp_iso8601_utc(self) -> None:
        """时间戳应为 ISO 8601 毫秒精度 UTC 格式。"""
        import re

        record = self._make_record()
        data = json.loads(self.formatter.format(record))
        self.assertRegex(
            data["timestamp"],
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z",
        )

    def test_zero_trace_ids_when_no_otel(self) -> None:
        record = self._make_record()
        data = json.loads(self.formatter.format(record))
        self.assertEqual(data["trace_id"], _ZERO_TRACE_ID)
        self.assertEqual(data["span_id"], _ZERO_SPAN_ID)

    def test_exception_field_included_on_error(self) -> None:
        try:
            raise ValueError("boom")
        except ValueError:
            exc_info = sys.exc_info()

        record = self._make_record(msg="error occurred", level=logging.ERROR, exc_info=exc_info)
        data = json.loads(self.formatter.format(record))
        self.assertIn("exception", data)
        self.assertIn("ValueError: boom", data["exception"])

    def test_exception_field_absent_on_normal_log(self) -> None:
        record = self._make_record()
        data = json.loads(self.formatter.format(record))
        self.assertNotIn("exception", data)

    def test_event_name_included_when_set(self) -> None:
        record = self._make_record(msg="kill_switch_check safe=True")
        record.event_name = "kill_switch_check"  # type: ignore[attr-defined]
        data = json.loads(self.formatter.format(record))
        self.assertEqual(data["event_name"], "kill_switch_check")

    def test_event_name_absent_when_not_set(self) -> None:
        record = self._make_record()
        data = json.loads(self.formatter.format(record))
        self.assertNotIn("event_name", data)

    def test_chinese_characters_not_escaped(self) -> None:
        """ensure_ascii=False 保证中文直接输出。"""
        record = self._make_record(msg="交易执行成功")
        data = json.loads(self.formatter.format(record))
        self.assertEqual(data["message"], "交易执行成功")


# ── _OTelTraceFilter 测试 ────────────────────────────────────────────

class TestOTelTraceFilter(unittest.TestCase):
    """验证 _OTelTraceFilter 注入 trace_id / span_id。"""

    def setUp(self) -> None:
        self.filt = _OTelTraceFilter()

    def _make_record(self) -> logging.LogRecord:
        return logging.LogRecord(
            name="aats.test", level=logging.INFO, pathname="", lineno=0,
            msg="test", args=(), exc_info=None,
        )

    def test_always_returns_true(self) -> None:
        """Filter 不应抑制任何日志记录。"""
        record = self._make_record()
        self.assertTrue(self.filt.filter(record))

    def test_zero_ids_without_active_span(self) -> None:
        """无活跃 OTel span 时注入全零 ID。"""
        record = self._make_record()
        self.filt.filter(record)
        self.assertEqual(record.otelTraceID, _ZERO_TRACE_ID)  # type: ignore[attr-defined]
        self.assertEqual(record.otelSpanID, _ZERO_SPAN_ID)  # type: ignore[attr-defined]

    def test_trace_id_format(self) -> None:
        """trace_id 为 32 位十六进制字符串。"""
        record = self._make_record()
        self.filt.filter(record)
        self.assertEqual(len(record.otelTraceID), 32)  # type: ignore[attr-defined]
        int(record.otelTraceID, 16)  # 不抛异常即为合法 hex  # type: ignore[attr-defined]

    def test_span_id_format(self) -> None:
        """span_id 为 16 位十六进制字符串。"""
        record = self._make_record()
        self.filt.filter(record)
        self.assertEqual(len(record.otelSpanID), 16)  # type: ignore[attr-defined]
        int(record.otelSpanID, 16)  # type: ignore[attr-defined]


# ── log_event() 测试 ─────────────────────────────────────────────────

class _CaptureHandler(logging.Handler):
    """测试辅助 handler，捕获 LogRecord 供断言。"""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class TestLogEvent(unittest.TestCase):
    """验证 log_event() 结构化输出、字段排序、event_name 注入。"""

    def setUp(self) -> None:
        self.logger = logging.getLogger("aats.test.log_event")
        self.logger.setLevel(logging.DEBUG)
        self.capture = _CaptureHandler()
        self.logger.addHandler(self.capture)

    def tearDown(self) -> None:
        self.logger.removeHandler(self.capture)

    def test_event_name_in_message(self) -> None:
        log_event(self.logger, "test_event", key1="val1")
        msg = self.capture.records[0].getMessage()
        self.assertTrue(msg.startswith("test_event"))
        self.assertIn("key1=val1", msg)

    def test_event_name_only_no_fields(self) -> None:
        log_event(self.logger, "heartbeat")
        msg = self.capture.records[0].getMessage()
        self.assertEqual(msg, "heartbeat")

    def test_correlation_field_order(self) -> None:
        """decision_id → intent_id → order_id → fill_id 优先，其余按字母序。"""
        log_event(
            self.logger, "evt",
            extra="x", decision_id="d1", order_id="o1", fill_id="f1",
        )
        msg = self.capture.records[0].getMessage()
        d_pos = msg.index("decision_id=d1")
        o_pos = msg.index("order_id=o1")
        f_pos = msg.index("fill_id=f1")
        e_pos = msg.index("extra=x")
        self.assertLess(d_pos, o_pos)
        self.assertLess(o_pos, f_pos)
        self.assertLess(f_pos, e_pos)

    def test_level_routing(self) -> None:
        """level 字段控制日志级别。"""
        log_event(self.logger, "warn_event", level="warning")
        self.assertEqual(self.capture.records[0].levelno, logging.WARNING)

    def test_event_name_injected_as_record_attribute(self) -> None:
        """log_event() 应通过 extra 将 event_name 注入 LogRecord。"""
        log_event(self.logger, "kill_switch_check", safe=True)
        record = self.capture.records[0]
        self.assertEqual(record.event_name, "kill_switch_check")  # type: ignore[attr-defined]

    def test_json_formatter_receives_event_name(self) -> None:
        """端到端：log_event() → _OTelTraceFilter → _JSONFormatter。"""
        formatter = _JSONFormatter()
        otel_filter = _OTelTraceFilter()

        capture_output: list[str] = []

        class JSONCapture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                capture_output.append(formatter.format(record))

        handler = JSONCapture()
        handler.addFilter(otel_filter)

        json_logger = logging.getLogger("aats.test.json_integration")
        json_logger.setLevel(logging.DEBUG)
        json_logger.addHandler(handler)
        try:
            log_event(json_logger, "position_sync", symbol="BTC-USDT")
        finally:
            json_logger.removeHandler(handler)

        self.assertEqual(len(capture_output), 1)
        data = json.loads(capture_output[0])
        self.assertEqual(data["event_name"], "position_sync")
        self.assertIn("position_sync", data["message"])
        self.assertIn("symbol=BTC-USDT", data["message"])
        self.assertEqual(data["trace_id"], _ZERO_TRACE_ID)
