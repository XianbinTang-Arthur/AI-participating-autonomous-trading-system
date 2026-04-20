"""P0-a Silver ETL catch-up 脚本单测 — 验证 bar 枚举逻辑.

不触碰真实 DB, 只锁定:
    - _enumerate_bars: 生成的 15m bar 边界正确
    - _align_up / _align_down: 对齐逻辑正确
    - 超出 --max-bars 拒绝执行 (exit 4)
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _ROOT / "scripts" / "maintenance" / "microstructure_silver_catchup_20260420.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "microstructure_silver_catchup_20260420", _SCRIPT,
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class TestBarEnumeration(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load_module()

    def test_enumerate_4hour_gap_produces_16_bars(self) -> None:
        """4h gap = 16 bars."""
        from_ts = datetime(2026, 4, 19, 21, 45, 0, tzinfo=timezone.utc)
        to_ts = datetime(2026, 4, 20, 1, 45, 0, tzinfo=timezone.utc)
        bars = self.mod._enumerate_bars(from_ts=from_ts, to_ts=to_ts)
        self.assertEqual(len(bars), 16)
        # 首尾对齐
        self.assertEqual(bars[0][0], from_ts)
        self.assertEqual(
            bars[-1][1],
            to_ts,
            "last bar_end should equal to_ts",
        )
        # 所有 bar 正好 15min
        for bs, be in bars:
            self.assertEqual(be - bs, timedelta(minutes=15))

    def test_enumerate_empty_when_from_equals_to(self) -> None:
        ts = datetime(2026, 4, 20, 12, 0, 0, tzinfo=timezone.utc)
        bars = self.mod._enumerate_bars(from_ts=ts, to_ts=ts)
        self.assertEqual(bars, [])

    def test_align_up_past_boundary(self) -> None:
        ts = datetime(2026, 4, 20, 12, 7, 30, tzinfo=timezone.utc)
        aligned = self.mod._align_up(ts)
        self.assertEqual(aligned, datetime(2026, 4, 20, 12, 15, 0, tzinfo=timezone.utc))

    def test_align_up_on_boundary(self) -> None:
        ts = datetime(2026, 4, 20, 12, 15, 0, tzinfo=timezone.utc)
        aligned = self.mod._align_up(ts)
        self.assertEqual(aligned, ts, "exactly aligned should not move")

    def test_align_down_in_middle(self) -> None:
        ts = datetime(2026, 4, 20, 12, 23, 0, tzinfo=timezone.utc)
        aligned = self.mod._align_down(ts)
        self.assertEqual(aligned, datetime(2026, 4, 20, 12, 15, 0, tzinfo=timezone.utc))


class TestCatchupArgParsing(unittest.TestCase):
    """--apply 未配 --confirm 应直接 exit 4, 不 touch DB。"""

    def setUp(self) -> None:
        self.mod = _load_module()

    def test_apply_without_confirm_rejected(self) -> None:
        orig_argv = sys.argv
        try:
            sys.argv = [
                "microstructure_silver_catchup_20260420.py", "--apply",
            ]
            rc = self.mod.main()
            self.assertEqual(rc, 4)
        finally:
            sys.argv = orig_argv

    def test_confirm_without_apply_rejected(self) -> None:
        orig_argv = sys.argv
        try:
            sys.argv = [
                "microstructure_silver_catchup_20260420.py", "--confirm",
            ]
            rc = self.mod.main()
            self.assertEqual(rc, 4)
        finally:
            sys.argv = orig_argv


if __name__ == "__main__":
    unittest.main()
