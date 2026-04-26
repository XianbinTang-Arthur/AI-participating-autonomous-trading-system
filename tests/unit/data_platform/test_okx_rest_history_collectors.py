"""P1-D Stage 5 单元测试 — OKX REST history backfill collectors.

对齐 aats/data_platform/collectors/backfill/okx_rest_history_collectors.py:
  - _parse_oi_history_row      : OKX [ts_ms, oi, oiCcy(, oiUsd)] → dict
  - _parse_mark_candle_row     : OKX [ts_ms, o, h, l, c, confirm] → dict (confirm=1 only)
  - _parse_ls_ratio_row        : OKX [ts_ms, longShortRatio(, posRatio)] → dict
  - estimate_*_requests        : pages/rows/time 预估正确
  - normalize_ls_symbol        : "BTC" → "BTC-USDT-SWAP" 规范化
  - _dedupe_by_ts              : (symbol, ts) 去重
  - collect_*_history (dry-run): 不发请求, 返回合理 stats

测试不发真实 OKX 请求 (单测 offline), 全部 mock HTTP / 构造假 response.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from aats.data_platform.collectors.backfill.okx_rest_history_collectors import (
    BackfillStats,
    _dedupe_by_ts,
    _ms_to_dt,
    _parse_ls_ratio_row,
    _parse_mark_candle_row,
    _parse_oi_history_row,
    _ts_ms,
    collect_ls_ratio_history,
    collect_mark_candles_history,
    collect_oi_history,
    estimate_ls_ratio_requests,
    estimate_mark_candles_requests,
    estimate_oi_history_requests,
    ls_ratio_storage_for_period,
    normalize_ls_symbol,
)


# ─────────────────────────────────────────────────────────────────────
# ts helpers
# ─────────────────────────────────────────────────────────────────────


class TestTsHelpers(unittest.TestCase):
    def test_ts_ms_roundtrip(self) -> None:
        ts = datetime(2026, 3, 20, 0, 0, 0, tzinfo=timezone.utc)
        ms = _ts_ms(ts)
        self.assertEqual(_ms_to_dt(ms), ts)
        # sanity: ms 为正 13 位数量级 (2001年后)
        self.assertTrue(1_000_000_000_000 <= ms < 2_000_000_000_000)

    def test_ts_ms_naive_treated_as_utc(self) -> None:
        ts = datetime(2026, 3, 20, 0, 0, 0)  # naive
        ms = _ts_ms(ts)
        # 等价于 UTC 0:00
        self.assertEqual(_ms_to_dt(ms), ts.replace(tzinfo=timezone.utc))


# ─────────────────────────────────────────────────────────────────────
# parse row
# ─────────────────────────────────────────────────────────────────────


class TestParseOIHistoryRow(unittest.TestCase):
    _TS = datetime(2026, 3, 20, tzinfo=timezone.utc)
    _TS_MS = str(_ts_ms(_TS))

    def test_3_elem_format(self) -> None:
        row = _parse_oi_history_row([self._TS_MS, "450000.5", "6100.123"])
        self.assertIsNotNone(row)
        self.assertEqual(row["ts"], self._TS)
        self.assertEqual(row["oi"], Decimal("450000.5"))
        self.assertEqual(row["oi_ccy"], Decimal("6100.123"))
        self.assertIsNone(row["oi_usd"])

    def test_4_elem_format_with_oi_usd(self) -> None:
        row = _parse_oi_history_row(
            [self._TS_MS, "450000", "6100", "450000000.5"]
        )
        self.assertEqual(row["oi_usd"], Decimal("450000000.5"))

    def test_rejects_malformed(self) -> None:
        self.assertIsNone(_parse_oi_history_row([]))
        self.assertIsNone(_parse_oi_history_row("not-a-list"))
        self.assertIsNone(_parse_oi_history_row(["bad-ts", "100"]))
        self.assertIsNone(_parse_oi_history_row([self._TS_MS, "not-decimal"]))

    def test_missing_oi_returns_none(self) -> None:
        self.assertIsNone(_parse_oi_history_row([self._TS_MS, None]))
        self.assertIsNone(_parse_oi_history_row([self._TS_MS, ""]))


class TestParseMarkCandleRow(unittest.TestCase):
    _TS_MS = str(_ts_ms(datetime(2026, 3, 20, tzinfo=timezone.utc)))

    def test_confirm_1_accepted(self) -> None:
        row = _parse_mark_candle_row(
            [self._TS_MS, "73000", "73100", "72950", "73050", "1"]
        )
        self.assertIsNotNone(row)
        self.assertEqual(row["open"], Decimal("73000"))
        self.assertEqual(row["close"], Decimal("73050"))

    def test_confirm_0_rejected(self) -> None:
        row = _parse_mark_candle_row(
            [self._TS_MS, "73000", "73100", "72950", "73050", "0"]
        )
        self.assertIsNone(row)

    def test_5_elem_no_confirm_accepted(self) -> None:
        """Some endpoints omit confirm; 我们应该接受."""
        row = _parse_mark_candle_row(
            [self._TS_MS, "73000", "73100", "72950", "73050"]
        )
        self.assertIsNotNone(row)
        self.assertEqual(row["close"], Decimal("73050"))

    def test_any_price_missing_rejected(self) -> None:
        self.assertIsNone(_parse_mark_candle_row(
            [self._TS_MS, "73000", "", "72950", "73050", "1"]
        ))


class TestParseLSRatioRow(unittest.TestCase):
    _TS_MS = str(_ts_ms(datetime(2026, 3, 20, tzinfo=timezone.utc)))

    def test_2_elem_account_only(self) -> None:
        row = _parse_ls_ratio_row([self._TS_MS, "1.07"])
        self.assertIsNotNone(row)
        self.assertEqual(row["ls_ratio_accounts"], Decimal("1.07"))
        self.assertIsNone(row["ls_ratio_positions"])

    def test_3_elem_with_position_ratio(self) -> None:
        row = _parse_ls_ratio_row([self._TS_MS, "1.07", "0.85"])
        self.assertEqual(row["ls_ratio_accounts"], Decimal("1.07"))
        self.assertEqual(row["ls_ratio_positions"], Decimal("0.85"))

    def test_malformed_rejected(self) -> None:
        self.assertIsNone(_parse_ls_ratio_row([]))
        self.assertIsNone(_parse_ls_ratio_row([self._TS_MS]))
        self.assertIsNone(_parse_ls_ratio_row(["not-ts", "1.07"]))


# ─────────────────────────────────────────────────────────────────────
# normalize_ls_symbol
# ─────────────────────────────────────────────────────────────────────


class TestNormalizeLsSymbol(unittest.TestCase):
    def test_btc(self) -> None:
        self.assertEqual(normalize_ls_symbol("BTC"), "BTC-USDT-SWAP")
        self.assertEqual(normalize_ls_symbol("btc"), "BTC-USDT-SWAP")

    def test_eth(self) -> None:
        self.assertEqual(normalize_ls_symbol("ETH"), "ETH-USDT-SWAP")

    def test_already_normalized(self) -> None:
        self.assertEqual(normalize_ls_symbol("BTC-USDT-SWAP"), "BTC-USDT-SWAP")
        self.assertEqual(normalize_ls_symbol("btc-usdt-swap"), "BTC-USDT-SWAP")


# ─────────────────────────────────────────────────────────────────────
# _dedupe_by_ts
# ─────────────────────────────────────────────────────────────────────


class TestDedupeByTs(unittest.TestCase):
    def test_dedupe_keeps_first(self) -> None:
        ts = datetime(2026, 3, 20, tzinfo=timezone.utc)
        rows = [
            {"symbol": "BTC", "ts": ts, "value": 1},
            {"symbol": "BTC", "ts": ts, "value": 2},  # dup
            {"symbol": "BTC", "ts": ts + timedelta(hours=1), "value": 3},
            {"symbol": "ETH", "ts": ts, "value": 4},
        ]
        out = _dedupe_by_ts(rows)
        self.assertEqual(len(out), 3)
        self.assertEqual(out[0]["value"], 1)  # first of (BTC, ts) kept
        self.assertEqual(out[1]["value"], 3)
        self.assertEqual(out[2]["value"], 4)


# ─────────────────────────────────────────────────────────────────────
# estimate_*_requests
# ─────────────────────────────────────────────────────────────────────


class TestEstimates(unittest.TestCase):
    def test_oi_90d_1h(self) -> None:
        est = estimate_oi_history_requests(90, "1H")
        self.assertEqual(est["target_days"], 90)
        self.assertEqual(est["estimated_rows"], 2160)  # 24×90
        # 2160 rows / 100 limit = 22 pages (ceil)
        self.assertEqual(est["estimated_pages"], 22)

    def test_oi_unsupported_period_raises(self) -> None:
        with self.assertRaises(ValueError):
            estimate_oi_history_requests(90, "7m")

    def test_mark_30d_1m(self) -> None:
        est = estimate_mark_candles_requests(30, "1m")
        self.assertEqual(est["estimated_rows"], 43200)  # 1440×30
        self.assertEqual(est["estimated_pages"], 432)

    def test_ls_30d_5m(self) -> None:
        est = estimate_ls_ratio_requests(30, "5m")
        self.assertEqual(est["estimated_rows"], 8640)  # 288×30
        self.assertEqual(est["estimated_pages"], 87)

    def test_ls_30d_1h(self) -> None:
        est = estimate_ls_ratio_requests(30, "1H")
        self.assertEqual(est["estimated_rows"], 720)  # 24×30
        self.assertEqual(est["estimated_pages"], 8)

    def test_ls_storage_for_period(self) -> None:
        self.assertEqual(
            ls_ratio_storage_for_period("5m"),
            ("bronze.market_long_short_ratio_5m", "ls_5m"),
        )
        self.assertEqual(
            ls_ratio_storage_for_period("1h"),
            ("bronze.market_long_short_ratio_1h", "ls_1h"),
        )


# ─────────────────────────────────────────────────────────────────────
# collect_* dry-run (不发请求, 返回 estimate)
# ─────────────────────────────────────────────────────────────────────


class TestCollectDryRun(unittest.TestCase):
    def test_oi_dry_run_no_http(self) -> None:
        stats = collect_oi_history(
            session=MagicMock(),  # dry-run 不碰 session
            symbol="BTC-USDT-SWAP",
            target_days=90,
            dry_run=True,
        )
        self.assertIsInstance(stats, BackfillStats)
        self.assertEqual(stats.endpoint,
                         "/api/v5/rubik/stat/contracts/open-interest-history")
        self.assertEqual(stats.rows_fetched, 2160)
        self.assertEqual(stats.pages_fetched, 22)
        # 没写入
        self.assertEqual(stats.rows_written, 0)

    def test_mark_dry_run_no_http(self) -> None:
        stats = collect_mark_candles_history(
            session=MagicMock(),
            symbol="BTC-USDT-SWAP",
            target_days=30,
            dry_run=True,
        )
        self.assertEqual(stats.rows_fetched, 43200)
        self.assertEqual(stats.pages_fetched, 432)

    def test_ls_dry_run_no_http(self) -> None:
        stats = collect_ls_ratio_history(
            session=MagicMock(),
            ccy="BTC",
            target_days=30,
            dry_run=True,
        )
        self.assertEqual(stats.symbol, "BTC-USDT-SWAP")
        self.assertEqual(stats.rows_fetched, 8640)

    def test_ls_1h_dry_run_no_http(self) -> None:
        stats = collect_ls_ratio_history(
            session=MagicMock(),
            ccy="BTC",
            target_days=30,
            period="1H",
            dry_run=True,
        )
        self.assertEqual(stats.symbol, "BTC-USDT-SWAP")
        self.assertEqual(stats.period, "1H")
        self.assertEqual(stats.rows_fetched, 720)


# ─────────────────────────────────────────────────────────────────────
# BackfillStats serialization
# ─────────────────────────────────────────────────────────────────────


class TestBackfillStats(unittest.TestCase):
    def test_to_dict_has_expected_keys(self) -> None:
        stats = BackfillStats(
            endpoint="/api/v5/x",
            symbol="BTC-USDT-SWAP",
            period="1H",
            target_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
            target_end=datetime(2026, 4, 1, tzinfo=timezone.utc),
        )
        stats.pages_fetched = 5
        stats.rows_fetched = 500
        stats.rows_written = 500
        stats.elapsed_sec = 3.14

        d = stats.to_dict()
        self.assertEqual(d["endpoint"], "/api/v5/x")
        self.assertEqual(d["pages_fetched"], 5)
        self.assertEqual(d["rows_fetched"], 500)
        self.assertIn("target_start", d)
        self.assertEqual(d["elapsed_sec"], 3.14)


# ─────────────────────────────────────────────────────────────────────
# Mocked HTTP apply path for _paged_request
# ─────────────────────────────────────────────────────────────────────


class TestCollectOIWithMockedHTTP(unittest.TestCase):
    """模拟 httpx.Client 返回 2 页 OI 数据, 验证:
      - 分页按 'end' cursor 向前走
      - rows_fetched 累加
      - _write_bronze_oi_history 被调用并写入对应行数
    """

    def _build_mock_response(self, data: list) -> MagicMock:
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"code": "0", "data": data}
        return resp

    def test_two_pages_accumulate(self) -> None:
        # Page 1: 最新 100 行, ts 从 now 到 now-100h
        # Page 2: 50 行, ts 从 now-100h 到 now-150h (API 到底)
        now_ms = 1776641100000  # Apr 2026 某 ts
        page1 = [[str(now_ms - i * 3600_000), "100", "0.5"] for i in range(100)]
        page2 = [[str(now_ms - (100 + i) * 3600_000), "100", "0.5"] for i in range(50)]

        # 使 httpx.Client() context manager 返回的 client 顺序返回 page1, page2
        mock_client = MagicMock()
        mock_client.get.side_effect = [
            self._build_mock_response(page1),
            self._build_mock_response(page2),
        ]

        mock_cm = MagicMock()
        mock_cm.__enter__ = MagicMock(return_value=mock_client)
        mock_cm.__exit__ = MagicMock(return_value=False)

        # 不碰 DB: patch _write_bronze_oi_history & upsert_checkpoint & utc_now
        with patch(
            "httpx.Client",
            return_value=mock_cm,
        ), patch(
            "aats.data_platform.collectors.backfill."
            "okx_rest_history_collectors._write_bronze_oi_history",
            return_value=150,
        ) as mock_write, patch(
            "aats.data_platform.jobs.checkpoint_manager.upsert_checkpoint",
            return_value="cp-1",
        ):
            from aats.data_platform.collectors.backfill.okx_rest_history_collectors import (
                collect_oi_history,
            )
            session = MagicMock()
            stats = collect_oi_history(
                session,
                symbol="BTC-USDT-SWAP",
                target_days=30,  # 30d
                period="1H",
                rate_limit_sleep=0,
                dry_run=False,
                ingest_run_id="run-1",
            )
            # page 1 返回 100 rows + page 2 返回 50 < 100 → api_exhausted
            self.assertEqual(stats.pages_fetched, 2)
            self.assertEqual(stats.rows_fetched, 150)
            # _write_bronze_oi_history 被调用 1 次, 预期 写入 150 rows
            mock_write.assert_called_once()
            self.assertEqual(stats.rows_written, 150)
            self.assertTrue(stats.api_exhausted)


class TestCollectOIRateLimitBackoff(unittest.TestCase):
    """验证 429 响应触发 backoff 并最终成功."""

    def test_429_then_200(self) -> None:
        now_ms = 1776641100000
        page_data = [[str(now_ms - i * 3600_000), "100", "0.5"] for i in range(50)]

        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.json.return_value = {"code": "50011", "msg": "rate limit"}

        resp_ok = MagicMock()
        resp_ok.status_code = 200
        resp_ok.json.return_value = {"code": "0", "data": page_data}

        mock_client = MagicMock()
        mock_client.get.side_effect = [resp_429, resp_ok]

        mock_cm = MagicMock()
        mock_cm.__enter__ = MagicMock(return_value=mock_client)
        mock_cm.__exit__ = MagicMock(return_value=False)

        # Mock time.sleep 跳过 backoff 延时 (不 real-wait)
        with patch(
            "httpx.Client",
            return_value=mock_cm,
        ), patch(
            "aats.data_platform.collectors.backfill."
            "okx_rest_history_collectors._write_bronze_oi_history",
            return_value=50,
        ), patch(
            "aats.data_platform.jobs.checkpoint_manager.upsert_checkpoint",
            return_value="cp-1",
        ), patch(
            "aats.data_platform.collectors.backfill."
            "okx_rest_history_collectors.time"
        ) as mock_time_mod:
            # 保留 monotonic 但让 sleep 立即 return
            import time as real_time
            mock_time_mod.sleep = lambda _: None
            mock_time_mod.monotonic = real_time.monotonic
            from aats.data_platform.collectors.backfill.okx_rest_history_collectors import (
                collect_oi_history,
            )
            stats = collect_oi_history(
                MagicMock(),
                symbol="BTC-USDT-SWAP",
                target_days=5,
                period="1H",
                rate_limit_sleep=0,
                dry_run=False,
                ingest_run_id="run-1",
            )
            # 50 rows < 100, api_exhausted
            self.assertEqual(stats.rate_limit_hits, 1)
            self.assertEqual(stats.rows_fetched, 50)


if __name__ == "__main__":
    unittest.main()
