"""P1-D Phase 1A Stage 2 单元测试 — microstructure message parsers。

覆盖 4 个 parser 函数的 happy-path + 关键 defensive 分支:
  - parse_trades_message (OKX trades-all)
  - parse_bbo_message (OKX bbo-tbt)
  - parse_books5_message (OKX books5)
  - parse_oi_funding_mark_message (open-interest / funding-rate / mark-price 共用)

对齐设计 §6.1-§6.4 的字段映射与 Stage 2 指令的采样/限流需求。
parser 是纯函数 (没有 DB / 没有 WS),不需要 mock。
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from aats.data_platform.collectors.microstructure_ws_collector import (
    parse_bbo_message,
    parse_books5_message,
    parse_oi_funding_mark_message,
    parse_trades_message,
)


_TS_MS_1 = "1745000000000"
_EXPECT_TS_1 = datetime.fromtimestamp(1_745_000_000, tz=timezone.utc)


# =====================================================================
# parse_trades_message
# =====================================================================


class TestParseTradesMessage(unittest.TestCase):
    """trades-all 频道按 tradeId 做 natural key, PK=(symbol, ts, trade_id)。"""

    def _push(self, **details_overrides: object) -> dict:
        detail = {
            "instId": "BTC-USDT-SWAP",
            "tradeId": "T-1",
            "px": "95000.5",
            "sz": "0.01",
            "side": "buy",
            "ts": _TS_MS_1,
        }
        detail.update(details_overrides)
        return {
            "arg": {"channel": "trades", "instId": "BTC-USDT-SWAP"},
            "data": [detail],
        }

    def test_happy_path(self) -> None:
        rows = parse_trades_message(self._push())
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r.symbol, "BTC-USDT-SWAP")
        self.assertEqual(r.trade_id, "T-1")
        self.assertEqual(r.side, "buy")
        self.assertEqual(r.px, Decimal("95000.5"))
        self.assertEqual(r.sz, Decimal("0.01"))
        self.assertEqual(r.ts, _EXPECT_TS_1)
        # raw_payload 保留 detail 原样,供 Silver/Gold 回放
        self.assertEqual(r.raw_payload["tradeId"], "T-1")
        self.assertEqual(r.raw_payload["side"], "buy")

    def test_multi_trades_same_ts_different_trade_id(self) -> None:
        """OKX liquidation cascade 可让同一 ts 出多笔 trade,
        PK (symbol, ts, trade_id) 允许这种并发插入 (Stage 1 已验证)。
        """
        push = {
            "arg": {"channel": "trades", "instId": "BTC-USDT-SWAP"},
            "data": [
                {"instId": "BTC-USDT-SWAP", "tradeId": "T-1",
                 "px": "95000", "sz": "0.1", "side": "buy", "ts": _TS_MS_1},
                {"instId": "BTC-USDT-SWAP", "tradeId": "T-2",
                 "px": "95001", "sz": "0.2", "side": "sell", "ts": _TS_MS_1},
            ],
        }
        rows = parse_trades_message(push)
        self.assertEqual(len(rows), 2)
        self.assertEqual({r.trade_id for r in rows}, {"T-1", "T-2"})

    def test_unknown_side_dropped(self) -> None:
        """chk_brz_trades_side DB CHECK 只认 buy/sell;其他值应 parser
        层过滤,避免写到 DB 被 IntegrityError 阻塞整个 batch。
        """
        push = self._push(side="short")    # 非 buy/sell
        self.assertEqual(parse_trades_message(push), [])

    def test_missing_trade_id_dropped(self) -> None:
        push = self._push(tradeId="")
        self.assertEqual(parse_trades_message(push), [])

    def test_missing_ts_dropped(self) -> None:
        push = self._push(ts="")
        self.assertEqual(parse_trades_message(push), [])

    def test_malformed_data_returns_empty(self) -> None:
        # OKX schema evolution guard
        self.assertEqual(parse_trades_message({"data": "nope"}), [])
        self.assertEqual(parse_trades_message({"data": [42]}), [])
        self.assertEqual(parse_trades_message({}), [])


# =====================================================================
# parse_bbo_message
# =====================================================================


class TestParseBboMessage(unittest.TestCase):
    """bbo-tbt 频道: 每条 push 只有一层 best bid/ask。"""

    def test_happy_path(self) -> None:
        push = {
            "arg": {"channel": "bbo-tbt", "instId": "BTC-USDT-SWAP"},
            "data": [
                {
                    "asks": [["95010", "2", "0", "3"]],
                    "bids": [["95000", "1", "0", "5"]],
                    "ts": _TS_MS_1,
                    "seqId": 123,
                    "instId": "BTC-USDT-SWAP",
                }
            ],
        }
        rows = parse_bbo_message(push)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r.symbol, "BTC-USDT-SWAP")
        self.assertEqual(r.bid_px, Decimal("95000"))
        self.assertEqual(r.bid_sz, Decimal("1"))
        self.assertEqual(r.ask_px, Decimal("95010"))
        self.assertEqual(r.ask_sz, Decimal("2"))
        # 解析阶段 ts 与 source_ts 相等;客户端限流在 collector 里替换 ts
        self.assertEqual(r.ts, _EXPECT_TS_1)
        self.assertEqual(r.source_ts, _EXPECT_TS_1)

    def test_symbol_defaults_to_arg_when_entry_missing(self) -> None:
        """OKX 通常在 entry 里也带 instId,但规范上允许 entry 省略,
        parser 应从 arg 取值。"""
        push = {
            "arg": {"channel": "bbo-tbt", "instId": "ETH-USDT-SWAP"},
            "data": [
                {
                    "asks": [["3010", "2"]],
                    "bids": [["3000", "1"]],
                    "ts": _TS_MS_1,
                }
            ],
        }
        rows = parse_bbo_message(push)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].symbol, "ETH-USDT-SWAP")

    def test_empty_bids_dropped(self) -> None:
        push = {
            "arg": {"channel": "bbo-tbt", "instId": "BTC-USDT-SWAP"},
            "data": [{"asks": [["95010", "2"]], "bids": [], "ts": _TS_MS_1}],
        }
        self.assertEqual(parse_bbo_message(push), [])

    def test_short_bid_entry_dropped(self) -> None:
        push = {
            "arg": {"channel": "bbo-tbt", "instId": "BTC-USDT-SWAP"},
            "data": [{"asks": [["95010"]], "bids": [["95000", "1"]], "ts": _TS_MS_1}],
        }
        self.assertEqual(parse_bbo_message(push), [])


# =====================================================================
# parse_books5_message
# =====================================================================


class TestParseBooks5Message(unittest.TestCase):
    """books5 频道: 最多 5 档深度, OKX 可能返回 < 5 档。"""

    def _full_5_levels(self) -> dict:
        return {
            "arg": {"channel": "books5", "instId": "BTC-USDT-SWAP"},
            "data": [
                {
                    "asks": [
                        ["95010", "2", "0", "3"],
                        ["95020", "5", "0", "4"],
                        ["95030", "8", "0", "2"],
                        ["95040", "11", "0", "1"],
                        ["95050", "14", "0", "1"],
                    ],
                    "bids": [
                        ["95000", "1", "0", "5"],
                        ["94990", "3", "0", "6"],
                        ["94980", "6", "0", "4"],
                        ["94970", "10", "0", "2"],
                        ["94960", "15", "0", "1"],
                    ],
                    "ts": _TS_MS_1,
                    "instId": "BTC-USDT-SWAP",
                }
            ],
        }

    def test_happy_path_5_levels(self) -> None:
        rows = parse_books5_message(self._full_5_levels())
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r.bid_px_1, Decimal("95000"))
        self.assertEqual(r.bid_px_5, Decimal("94960"))
        self.assertEqual(r.ask_px_1, Decimal("95010"))
        self.assertEqual(r.ask_px_5, Decimal("95050"))
        self.assertEqual(r.bid_sz_3, Decimal("6"))

    def test_thin_book_3_levels(self) -> None:
        """OKX 薄市场时可能只返回 3 档,level 4/5 必须 NULL。"""
        push = {
            "arg": {"channel": "books5", "instId": "BTC-USDT-SWAP"},
            "data": [
                {
                    "asks": [
                        ["95010", "2"],
                        ["95020", "5"],
                        ["95030", "8"],
                    ],
                    "bids": [
                        ["95000", "1"],
                        ["94990", "3"],
                        ["94980", "6"],
                    ],
                    "ts": _TS_MS_1,
                }
            ],
        }
        rows = parse_books5_message(push)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r.bid_px_3, Decimal("94980"))
        self.assertIsNone(r.bid_px_4)
        self.assertIsNone(r.bid_sz_5)
        self.assertIsNone(r.ask_px_4)
        self.assertIsNone(r.ask_sz_5)

    def test_top_of_book_invalid_dropped(self) -> None:
        """Level 1 是 NOT NULL (§6.3); parser 必须拒绝首档无法解析的消息。"""
        push = {
            "arg": {"channel": "books5", "instId": "BTC-USDT-SWAP"},
            "data": [
                {
                    "asks": [["not_a_number", "2"]],
                    "bids": [["95000", "1"]],
                    "ts": _TS_MS_1,
                }
            ],
        }
        self.assertEqual(parse_books5_message(push), [])

    def test_empty_data(self) -> None:
        self.assertEqual(parse_books5_message({"arg": {}, "data": []}), [])


# =====================================================================
# parse_oi_funding_mark_message
# =====================================================================


class TestParseOiFundingMarkMessage(unittest.TestCase):
    """3 个频道共用一个 parser,按 arg.channel 分派到 tick_type。"""

    def test_open_interest_happy_path(self) -> None:
        push = {
            "arg": {"channel": "open-interest", "instId": "BTC-USDT-SWAP"},
            "data": [{
                "instType": "SWAP",
                "instId": "BTC-USDT-SWAP",
                "oi": "1234567",
                "oiCcy": "12345.6789",
                "ts": _TS_MS_1,
            }],
        }
        rows = parse_oi_funding_mark_message(push)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r.tick_type, "oi")
        self.assertEqual(r.symbol, "BTC-USDT-SWAP")
        self.assertEqual(r.oi, Decimal("1234567"))
        self.assertEqual(r.oi_ccy, Decimal("12345.6789"))
        self.assertEqual(r.ts, _EXPECT_TS_1)
        # non-oi fields 必须 None
        self.assertIsNone(r.funding_rate)
        self.assertIsNone(r.mark_px)

    def test_funding_rate_happy_path(self) -> None:
        push = {
            "arg": {"channel": "funding-rate", "instId": "BTC-USDT-SWAP"},
            "data": [{
                "instId": "BTC-USDT-SWAP",
                "fundingRate": "0.000150000000",
                "nextFundingRate": "0.000200000000",
                "fundingTime": _TS_MS_1,
                "nextFundingTime": "1745028800000",
            }],
        }
        rows = parse_oi_funding_mark_message(push)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r.tick_type, "funding")
        self.assertEqual(r.funding_rate, Decimal("0.000150000000"))
        self.assertEqual(r.next_funding_rate, Decimal("0.000200000000"))
        self.assertEqual(r.ts, _EXPECT_TS_1)   # 用 fundingTime 作 ts
        self.assertIsNotNone(r.next_funding_time)
        self.assertIsNone(r.oi)

    def test_mark_price_happy_path(self) -> None:
        push = {
            "arg": {"channel": "mark-price", "instId": "BTC-USDT-SWAP"},
            "data": [{
                "instType": "SWAP",
                "instId": "BTC-USDT-SWAP",
                "markPx": "95000.25",
                "ts": _TS_MS_1,
            }],
        }
        rows = parse_oi_funding_mark_message(push)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r.tick_type, "mark")
        self.assertEqual(r.mark_px, Decimal("95000.25"))
        self.assertIsNone(r.oi)
        self.assertIsNone(r.funding_rate)

    def test_unknown_channel_returns_empty(self) -> None:
        """Dispatcher 收到非 oi/funding/mark 的 channel 应静默返回空,
        避免污染 staging 表 (DB chk_staging_oif_type CHECK 会拒绝任何
        非 {oi, funding, mark} 的 tick_type 值)。
        """
        push = {
            "arg": {"channel": "some-unknown-channel", "instId": "BTC-USDT-SWAP"},
            "data": [{"instId": "BTC-USDT-SWAP", "ts": _TS_MS_1}],
        }
        self.assertEqual(parse_oi_funding_mark_message(push), [])

    def test_missing_required_field_dropped(self) -> None:
        # funding-rate 缺 fundingRate
        push = {
            "arg": {"channel": "funding-rate", "instId": "BTC-USDT-SWAP"},
            "data": [{"instId": "BTC-USDT-SWAP", "fundingTime": _TS_MS_1}],
        }
        self.assertEqual(parse_oi_funding_mark_message(push), [])

        # mark-price 缺 markPx
        push = {
            "arg": {"channel": "mark-price", "instId": "BTC-USDT-SWAP"},
            "data": [{"instId": "BTC-USDT-SWAP", "ts": _TS_MS_1}],
        }
        self.assertEqual(parse_oi_funding_mark_message(push), [])

    def test_nextFundingTime_missing_ok(self) -> None:
        """nextFundingTime 可选字段,缺失不应拖垮整行。"""
        push = {
            "arg": {"channel": "funding-rate", "instId": "BTC-USDT-SWAP"},
            "data": [{
                "instId": "BTC-USDT-SWAP",
                "fundingRate": "0.0001",
                "fundingTime": _TS_MS_1,
            }],
        }
        rows = parse_oi_funding_mark_message(push)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].next_funding_time)
        self.assertIsNone(rows[0].next_funding_rate)


if __name__ == "__main__":
    unittest.main()
