"""Unit tests for OKX liquidation-orders WebSocket collector.

Covers the pure-function parser, the DB write helper (via capture), and the
collector's buffered flush path (using an in-process mock session).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import patch

from aats.bootstrap.settings import AATSSettings
from aats.data_platform.collectors.liquidations_ws_collector import (
    LiquidationRow,
    LiquidationsCollector,
    OKXLiquidationsWSClient,
    parse_liquidation_message,
    write_liquidation_batch,
)
from aats.services.market_gateway.okx_websocket import (
    OKXWebSocketConsumerBase,
    _subscription_key,
)


def _ws_settings(**overrides: object) -> AATSSettings:
    defaults: dict[str, object] = {
        "okx_ws_read_timeout_seconds": 0.5,
        "okx_ws_market_data_timeout_seconds": 1.5,
        "okx_market_reconnect_delay_seconds": 0.1,
        "okx_market_reconnect_max_delay_seconds": 0.2,
        "okx_ws_open_timeout_seconds": 5.0,
        "okx_private_ws_idle_ping_interval_seconds": 0.5,
    }
    defaults.update(overrides)
    return AATSSettings.model_validate(defaults)


_SAMPLE_PUSH: dict[str, Any] = {
    "arg": {"channel": "liquidation-orders", "instType": "SWAP"},
    "data": [
        {
            "instType": "SWAP",
            "instFamily": "BTC-USDT",
            "instId": "BTC-USDT-SWAP",
            "details": [
                {
                    "side": "sell",
                    "bkPx": "95000",
                    "sz": "1.5",
                    "bkLoss": "0",
                    "ccy": "USDT",
                    "ts": "1745000000000",
                }
            ],
        }
    ],
}


class TestSubscriptionKeyHelper(unittest.TestCase):
    """The normalization helper must handle both instId and instType channels."""

    def test_instid_based_channel(self) -> None:
        arg = {"channel": "tickers", "instId": "BTC-USDT-SWAP"}
        self.assertEqual(_subscription_key(arg), ("tickers", "BTC-USDT-SWAP"))

    def test_insttype_based_channel(self) -> None:
        arg = {"channel": "liquidation-orders", "instType": "SWAP"}
        self.assertEqual(_subscription_key(arg), ("liquidation-orders", "SWAP"))

    def test_instfamily_based_channel(self) -> None:
        arg = {"channel": "price-limit", "instFamily": "BTC-USDT"}
        self.assertEqual(_subscription_key(arg), ("price-limit", "BTC-USDT"))

    def test_missing_filter_returns_empty(self) -> None:
        self.assertEqual(_subscription_key({"channel": "foo"}), ("foo", ""))

    def test_instid_takes_priority(self) -> None:
        arg = {"channel": "tickers", "instId": "A", "instType": "SWAP", "instFamily": "F"}
        self.assertEqual(_subscription_key(arg), ("tickers", "A"))


class TestParseLiquidationMessage(unittest.TestCase):
    def test_happy_path(self) -> None:
        rows = parse_liquidation_message(_SAMPLE_PUSH)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r.inst_id, "BTC-USDT-SWAP")
        self.assertEqual(r.inst_type, "SWAP")
        self.assertEqual(r.inst_family, "BTC-USDT")
        self.assertEqual(r.side, "sell")
        self.assertEqual(r.bk_px, Decimal("95000"))
        self.assertEqual(r.sz, Decimal("1.5"))
        self.assertEqual(r.bk_loss, Decimal("0"))
        self.assertEqual(r.ccy, "USDT")
        # ts is 2025-04-18 16:53:20 UTC-ish; we just verify tz-awareness + ms→s
        self.assertEqual(r.ts, datetime.fromtimestamp(1745000000, tz=timezone.utc))
        self.assertIn("event", r.raw_payload)
        self.assertIn("detail", r.raw_payload)

    def test_multi_details(self) -> None:
        push = {
            "arg": {"channel": "liquidation-orders", "instType": "SWAP"},
            "data": [
                {
                    "instType": "SWAP",
                    "instId": "ETH-USDT-SWAP",
                    "details": [
                        {"side": "buy", "bkPx": "3000", "sz": "0.5", "ts": "1745000000000"},
                        {"side": "sell", "bkPx": "2999", "sz": "2.0", "ts": "1745000000001"},
                    ],
                }
            ],
        }
        rows = parse_liquidation_message(push)
        self.assertEqual(len(rows), 2)
        self.assertEqual({r.side for r in rows}, {"buy", "sell"})

    def test_empty_data(self) -> None:
        self.assertEqual(parse_liquidation_message({"arg": {}, "data": []}), [])
        self.assertEqual(parse_liquidation_message({"arg": {}}), [])

    def test_missing_required_field_skipped(self) -> None:
        push = {
            "arg": {"channel": "liquidation-orders"},
            "data": [
                {
                    "instType": "SWAP",
                    "instId": "BTC-USDT-SWAP",
                    "details": [
                        # missing bkPx
                        {"side": "sell", "sz": "1.5", "ts": "1745000000000"},
                        # unknown side
                        {"side": "unknown", "bkPx": "1", "sz": "1", "ts": "1745000000000"},
                        # valid row salvaged
                        {"side": "buy", "bkPx": "1", "sz": "1", "ts": "1745000000000"},
                    ],
                }
            ],
        }
        rows = parse_liquidation_message(push)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].side, "buy")

    def test_non_dict_data_does_not_raise(self) -> None:
        # OKX schema evolution guard — unexpected shapes must not crash.
        self.assertEqual(parse_liquidation_message({"data": "nope"}), [])
        self.assertEqual(parse_liquidation_message({"data": [42]}), [])
        self.assertEqual(parse_liquidation_message({"data": [{"details": "nope"}]}), [])

    def test_optional_fields_nullable(self) -> None:
        push = {
            "arg": {"channel": "liquidation-orders"},
            "data": [
                {
                    "instType": "SWAP",
                    "instId": "BTC-USDT-SWAP",
                    "details": [
                        {"side": "sell", "bkPx": "1", "sz": "1", "ts": "1745000000000"},
                    ],
                }
            ],
        }
        rows = parse_liquidation_message(push)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].bk_loss)
        self.assertIsNone(rows[0].ccy)
        self.assertIsNone(rows[0].inst_family)


class _CapturingSession:
    def __init__(self) -> None:
        self.executed: list[tuple[str, list[dict[str, Any]]]] = []

    def execute(self, stmt, params):  # noqa: ANN001 — SQLAlchemy text stmt is opaque
        sql = str(stmt).strip()
        batch = list(params) if not isinstance(params, dict) else [params]
        self.executed.append((sql, batch))


class TestWriteLiquidationBatch(unittest.TestCase):
    def test_batch_insert_emits_on_conflict_clause(self) -> None:
        session = _CapturingSession()
        rows = [
            LiquidationRow(
                ts=datetime.fromtimestamp(1745000000, tz=timezone.utc),
                inst_id="BTC-USDT-SWAP",
                inst_type="SWAP",
                inst_family="BTC-USDT",
                side="sell",
                bk_px=Decimal("95000"),
                sz=Decimal("1.5"),
                bk_loss=Decimal("0"),
                ccy="USDT",
                raw_payload={"foo": "bar"},
            )
        ]
        count = write_liquidation_batch(session, rows)  # type: ignore[arg-type]
        self.assertEqual(count, 1)
        self.assertEqual(len(session.executed), 1)
        sql, batch = session.executed[0]
        self.assertIn("INSERT INTO staging.raw_liquidations", sql)
        self.assertIn("ON CONFLICT ON CONSTRAINT uq_raw_liquidations_natural_key DO NOTHING", sql)
        self.assertEqual(len(batch), 1)
        self.assertEqual(batch[0]["inst_id"], "BTC-USDT-SWAP")
        # raw_payload must be serialized JSON for CAST(:raw_payload AS JSONB)
        self.assertEqual(json.loads(batch[0]["raw_payload"]), {"foo": "bar"})

    def test_empty_rows_noop(self) -> None:
        session = _CapturingSession()
        self.assertEqual(write_liquidation_batch(session, []), 0)  # type: ignore[arg-type]
        self.assertEqual(session.executed, [])


class TestLiquidationsWSClient(unittest.TestCase):
    def test_connection_specs_single_swap(self) -> None:
        client = OKXLiquidationsWSClient(settings=_ws_settings(), inst_types=("SWAP",))
        specs = client._connection_specs()
        self.assertEqual(len(specs), 1)
        name, _url, args = specs[0]
        self.assertEqual(name, "public")
        self.assertEqual(args, [{"channel": "liquidation-orders", "instType": "SWAP"}])

    def test_connection_specs_multi_inst_type(self) -> None:
        client = OKXLiquidationsWSClient(settings=_ws_settings(), inst_types=("SWAP", "FUTURES"))
        _name, _url, args = client._connection_specs()[0]
        self.assertEqual(
            args,
            [
                {"channel": "liquidation-orders", "instType": "SWAP"},
                {"channel": "liquidation-orders", "instType": "FUTURES"},
            ],
        )

    def test_empty_inst_types_rejected(self) -> None:
        with self.assertRaises(ValueError):
            OKXLiquidationsWSClient(settings=_ws_settings(), inst_types=())

    def test_is_consumer_subclass(self) -> None:
        client = OKXLiquidationsWSClient(settings=_ws_settings())
        self.assertIsInstance(client, OKXWebSocketConsumerBase)


class _MockFlushSession:
    """Session that records executed batches and commits cleanly."""

    def __init__(self, store: list[list[dict[str, Any]]]) -> None:
        self._store = store

    def execute(self, stmt, params):  # noqa: ANN001
        self._store.append(list(params) if not isinstance(params, dict) else [params])

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


class TestCollectorBufferFlush(unittest.IsolatedAsyncioTestCase):
    async def test_flush_on_max_rows(self) -> None:
        executed: list[list[dict[str, Any]]] = []

        @contextlib.contextmanager
        def _fake_session():
            yield _MockFlushSession(executed)

        with patch(
            "aats.data_platform.collectors.liquidations_ws_collector.get_session",
            _fake_session,
        ):
            collector = LiquidationsCollector(
                settings=_ws_settings(),
                flush_max_rows=2,
                flush_max_seconds=60.0,
            )
            # Two messages of 1 row each — second one should trigger flush.
            await collector._handle_message(_SAMPLE_PUSH)
            self.assertEqual(executed, [])
            self.assertEqual(len(collector._buffer), 1)
            await collector._handle_message(_SAMPLE_PUSH)
            self.assertEqual(len(executed), 1)
            self.assertEqual(len(executed[0]), 2)
            self.assertEqual(collector._buffer, [])
            today = datetime.now(tz=timezone.utc).date().isoformat()
            self.assertEqual(collector._daily_counts[today], 2)

    async def test_flush_on_periodic(self) -> None:
        executed: list[list[dict[str, Any]]] = []

        @contextlib.contextmanager
        def _fake_session():
            yield _MockFlushSession(executed)

        with patch(
            "aats.data_platform.collectors.liquidations_ws_collector.get_session",
            _fake_session,
        ):
            collector = LiquidationsCollector(
                settings=_ws_settings(),
                flush_max_rows=1000,
                flush_max_seconds=0.05,
            )
            await collector._handle_message(_SAMPLE_PUSH)
            self.assertEqual(executed, [])
            flush_task = asyncio.create_task(collector._periodic_flush())
            # Wait just long enough for one periodic flush to fire.
            await asyncio.sleep(0.15)
            collector._stop_event.set()
            await asyncio.wait_for(flush_task, timeout=1.0)
            self.assertEqual(len(executed), 1)
            self.assertEqual(len(executed[0]), 1)

    async def test_status_exposes_daily_counts(self) -> None:
        collector = LiquidationsCollector(settings=_ws_settings())
        status = collector.status()
        self.assertIn("connected", status)
        self.assertIn("today_count", status)
        self.assertIn("inst_types", status)
        self.assertEqual(status["inst_types"], ["SWAP"])


if __name__ == "__main__":
    unittest.main()
