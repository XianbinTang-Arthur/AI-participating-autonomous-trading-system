"""Unit tests for aats.data_platform.governance._time_util (RDP A-0.4)."""

from __future__ import annotations

import logging
import unittest
from datetime import datetime, timedelta, timezone

from aats.data_platform.governance._time_util import parse_iso_datetime_utc


class TestParseIsoDatetimeUtc(unittest.TestCase):
    def test_none_returns_none(self) -> None:
        self.assertIsNone(parse_iso_datetime_utc(None))

    def test_empty_string_returns_none(self) -> None:
        self.assertIsNone(parse_iso_datetime_utc(""))
        self.assertIsNone(parse_iso_datetime_utc("   "))

    def test_illegal_format_raises_value_error_with_context(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            parse_iso_datetime_utc("not-a-date", context="gate_rules.created_at")
        msg = str(ctx.exception)
        self.assertIn("illegal_iso_datetime", msg)
        self.assertIn("'not-a-date'", msg)
        self.assertIn("gate_rules.created_at", msg)

    def test_naive_input_returns_utc_with_warning(self) -> None:
        with self.assertLogs("aats.data_platform.governance._time_util", level="WARNING") as cap:
            result = parse_iso_datetime_utc("2026-04-17T10:00:00", context="t")
        self.assertIsNotNone(result)
        self.assertEqual(result.tzinfo, timezone.utc)
        self.assertEqual(result, datetime(2026, 4, 17, 10, 0, 0, tzinfo=timezone.utc))
        self.assertTrue(any("naive datetime" in msg for msg in cap.output))

    def test_z_suffix_parses_as_utc(self) -> None:
        result = parse_iso_datetime_utc("2026-04-17T10:00:00Z")
        self.assertEqual(result, datetime(2026, 4, 17, 10, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(result.tzinfo, timezone.utc)

    def test_offset_is_normalized_to_utc(self) -> None:
        result = parse_iso_datetime_utc("2026-04-17T12:00:00+02:00")
        self.assertEqual(result, datetime(2026, 4, 17, 10, 0, 0, tzinfo=timezone.utc))

    def test_already_utc_is_idempotent(self) -> None:
        result = parse_iso_datetime_utc("2026-04-17T10:00:00+00:00")
        self.assertEqual(result, datetime(2026, 4, 17, 10, 0, 0, tzinfo=timezone.utc))

    def test_datetime_input_naive_gets_utc_tag(self) -> None:
        naive = datetime(2026, 4, 17, 10, 0, 0)
        with self.assertLogs("aats.data_platform.governance._time_util", level="WARNING"):
            result = parse_iso_datetime_utc(naive, context="dt")
        self.assertEqual(result.tzinfo, timezone.utc)

    def test_datetime_input_aware_is_converted(self) -> None:
        tz_plus_2 = timezone(timedelta(hours=2))
        aware = datetime(2026, 4, 17, 12, 0, 0, tzinfo=tz_plus_2)
        result = parse_iso_datetime_utc(aware)
        self.assertEqual(result, datetime(2026, 4, 17, 10, 0, 0, tzinfo=timezone.utc))

    def test_silent_swallow_is_forbidden(self) -> None:
        """Explicit regression guard: previous local helpers returned None on bad input,
        letting upstream gate comparisons pass unchecked. The util must NOT do that."""
        with self.assertRaises(ValueError):
            parse_iso_datetime_utc("2026-13-40T99:99:99", context="regression")

    def test_context_defaults_render_readably_in_errors(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            parse_iso_datetime_utc("garbage")
        self.assertIn("<unknown>", str(ctx.exception))


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    unittest.main()
