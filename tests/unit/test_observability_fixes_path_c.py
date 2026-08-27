"""Path C 观测性修复锁定测试 (2026-04-19)

覆盖:
1. Fix 1: `execution_control/order_service._ensure_order_row` 在 raw_payload
   顶层落库 `execution_style` (来自 OrderIntent)
2. Fix 2a: `schemas/exchange.ExchangeFill` 和 `schemas/execution.FillEvent` 都
   有 `raw_exchange` 字段, 默认 None
3. Fix 2b: `okx_adapter._parse_fill_rows` 按白名单提取 OKX 原始字段到 raw_exchange
   (feeRate / execType / liquidity / ordType / posSide / tradeId), 其余丢弃

参考: docs/review/cost_audit_live_reconciliation_2026_04_19.md §7.2
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from aats.schemas.exchange import ExchangeFill, InstrumentMetadata
from aats.schemas.execution import FillEvent, OrderIntent
from aats.services.execution_engine.okx_adapter import OKXExecutionAdapter


def _make_intent(execution_style: str) -> OrderIntent:
    return OrderIntent(
        intent_id="intent_test_h4_obs",
        decision_id="decision_test",
        symbol="BTC-USDT-SWAP",
        side="buy",
        quantity=Decimal("0.01"),
        execution_style=execution_style,
        order_type="market",
        urgency="medium",
        time_in_force="IOC",
        idempotency_key="idem_test_h4_obs",
        created_at=datetime.now(timezone.utc),
    )


class _FakeOrderStateRepo:
    """极简 stub — 只记录 create_order 收到的 raw_payload 做断言。"""

    def __init__(self) -> None:
        self.last_raw_payload: dict[str, Any] | None = None

    def get_order_by_client_order_id(self, client_order_id: str):
        return None

    def create_order(self, *, order_id: str, intent: OrderIntent, initial_state: str,
                     created_at: datetime, raw_payload: dict[str, Any]) -> None:
        self.last_raw_payload = raw_payload


def _linear_btc_usdt_swap_metadata() -> InstrumentMetadata:
    return InstrumentMetadata(
        instrument_id="BTC-USDT-SWAP",
        symbol="BTC-USDT-SWAP",
        base_currency="BTC",
        quote_currency="USDT",
        lot_size=Decimal("0.01"),
        tick_size=Decimal("0.1"),
        min_size=Decimal("0.01"),
        contract_value=Decimal("0.01"),
        contract_multiplier=Decimal("1"),
        contract_type="linear",
        instrument_type="SWAP",
        settle_currency="USDT",
        contract_value_currency="BTC",
        state="live",
    )


class _FakeAccountService:
    def instrument_metadata(self, symbol: str) -> InstrumentMetadata:
        if symbol != "BTC-USDT-SWAP":
            raise AssertionError(f"unexpected instrument metadata request: {symbol}")
        return _linear_btc_usdt_swap_metadata()


class TestExecutionStyleInRawPayload(unittest.TestCase):
    """Fix 1 锁定: execution_style 必须出现在 raw_payload 顶层。

    场景: cost_audit_live_reconciliation_2026_04_19 发现所有 28 条线上订单的
    `execution_orders.raw_payload.execution_style` 都是 None, 导致事后无法
    还原当时的 execution style 做 fee_resolver 审计.
    """

    def test_ensure_order_row_includes_execution_style_in_raw_payload(self) -> None:
        from aats.services.execution_control.order_service import ExecutionOrderService

        fake_repo = _FakeOrderStateRepo()
        # 用 object.__new__ 跳过 __init__ (不需要真正的依赖图)
        service = object.__new__(ExecutionOrderService)
        service.execution_order_repo = fake_repo  # type: ignore[attr-defined]
        service.execution_order_history_repo = None  # type: ignore[attr-defined]

        intent = _make_intent(execution_style="bounded_limit_ioc")
        service._ensure_order_row(
            intent=intent,
            client_order_id="cl_test_obs",
            initial_state="SUBMITTED",
            order_state=None,
        )

        self.assertIsNotNone(fake_repo.last_raw_payload)
        self.assertIn("execution_style", fake_repo.last_raw_payload,
                      "raw_payload 必须顶层落库 execution_style (Path C 观测性修复)")
        self.assertEqual(fake_repo.last_raw_payload["execution_style"], "bounded_limit_ioc")

    def test_ensure_order_row_passes_through_different_execution_styles(self) -> None:
        """对不同 execution_style 字符串都要原样落库(不 transform, 不 mangle)。"""
        from aats.services.execution_control.order_service import ExecutionOrderService

        for style in ("bounded_limit_ioc", "bounded_taker_cap", "passive_first",
                      "maker", "taker", "exchange"):
            fake_repo = _FakeOrderStateRepo()
            service = object.__new__(ExecutionOrderService)
            service.execution_order_repo = fake_repo  # type: ignore[attr-defined]
            service.execution_order_history_repo = None  # type: ignore[attr-defined]

            intent = _make_intent(execution_style=style)
            service._ensure_order_row(
                intent=intent,
                client_order_id=f"cl_test_obs_{style}",
                initial_state="SUBMITTED",
                order_state=None,
            )
            self.assertEqual(
                fake_repo.last_raw_payload["execution_style"],  # type: ignore[index]
                style,
                f"execution_style={style} 应原样落库",
            )


class TestExchangeFillRawExchangeField(unittest.TestCase):
    """Fix 2a 锁定: ExchangeFill 和 FillEvent schema 都有可选 raw_exchange 字段。"""

    def test_exchange_fill_raw_exchange_defaults_to_none(self) -> None:
        fill = ExchangeFill(
            fill_id="f1",
            exchange_order_id="ord_1",
            instrument_id="BTC-USDT-SWAP",
            symbol="BTC-USDT-SWAP",
            side="buy",
            fill_qty=Decimal("0.01"),
            fill_price=Decimal("75000"),
        )
        self.assertIsNone(fill.raw_exchange)

    def test_exchange_fill_accepts_raw_exchange_dict(self) -> None:
        fill = ExchangeFill(
            fill_id="f1",
            exchange_order_id="ord_1",
            instrument_id="BTC-USDT-SWAP",
            symbol="BTC-USDT-SWAP",
            side="buy",
            fill_qty=Decimal("0.01"),
            fill_price=Decimal("75000"),
            raw_exchange={"feeRate": "-0.0005", "execType": "T"},
        )
        self.assertEqual(fill.raw_exchange, {"feeRate": "-0.0005", "execType": "T"})

    def test_fill_event_raw_exchange_defaults_to_none(self) -> None:
        fill = FillEvent(
            fill_id="f1",
            decision_id="dec1",
            intent_id="int1",
            client_order_id="cl1",
            exchange_order_id="ord1",
            symbol="BTC-USDT-SWAP",
            side="buy",
            fill_qty=Decimal("0.01"),
            fill_price=Decimal("75000"),
            fee_amount=Decimal("-0.375"),
            liquidity_role="taker",
            exchange_timestamp=datetime.now(timezone.utc),
            ingestion_timestamp=datetime.now(timezone.utc),
        )
        self.assertIsNone(fill.raw_exchange)

    def test_fill_event_accepts_raw_exchange_dict(self) -> None:
        fill = FillEvent(
            fill_id="f1",
            decision_id="dec1",
            intent_id="int1",
            client_order_id="cl1",
            exchange_order_id="ord1",
            symbol="BTC-USDT-SWAP",
            side="buy",
            fill_qty=Decimal("0.01"),
            fill_price=Decimal("75000"),
            fee_amount=Decimal("-0.375"),
            liquidity_role="taker",
            exchange_timestamp=datetime.now(timezone.utc),
            ingestion_timestamp=datetime.now(timezone.utc),
            raw_exchange={"feeRate": "-0.0005", "execType": "T", "liquidity": "T"},
        )
        self.assertEqual(
            fill.raw_exchange,
            {"feeRate": "-0.0005", "execType": "T", "liquidity": "T"},
        )


class TestParseFillRowsRawExchangeWhitelist(unittest.TestCase):
    """Fix 2b 锁定: _parse_fill_rows 提取 OKX 原始字段到 raw_exchange 白名单子集。"""

    def _call_parse(self, payload: dict[str, Any]) -> list[ExchangeFill]:
        """调用 _parse_fill_rows, 用 object.__new__ 跳过 adapter 完整 __init__。"""
        adapter = object.__new__(OKXExecutionAdapter)
        adapter.account_service = _FakeAccountService()  # type: ignore[attr-defined]
        return OKXExecutionAdapter._parse_fill_rows(adapter, payload)

    def _base_row(self) -> dict[str, Any]:
        return {
            "tradeId": "t123",
            "ordId": "ord_abc",
            "clOrdId": "cl_xyz",
            "instId": "BTC-USDT-SWAP",
            "side": "buy",
            "fillSz": "0.01",
            "fillPx": "75000",
            "fee": "0.375",
            "feeCcy": "USDT",
            "fillTime": "1714500000000",
        }

    def test_whitelist_fields_preserved(self) -> None:
        row = self._base_row()
        row.update({
            "feeRate": "-0.0005",
            "execType": "T",
            "liquidity": "T",
            "ordType": "market",
            "posSide": "long",
        })
        fills = self._call_parse({"data": [row]})
        self.assertEqual(len(fills), 1)
        raw = fills[0].raw_exchange
        self.assertIsNotNone(raw)
        self.assertEqual(raw["feeRate"], "-0.0005")
        self.assertEqual(raw["execType"], "T")
        self.assertEqual(raw["liquidity"], "T")
        self.assertEqual(raw["ordType"], "market")
        self.assertEqual(raw["posSide"], "long")
        self.assertEqual(raw["tradeId"], "t123")

    def test_non_whitelisted_fields_dropped(self) -> None:
        """OKX 可能返回大量其他字段, 不在白名单的必须丢弃以控制数据体积 + 安全。"""
        row = self._base_row()
        row.update({
            "feeRate": "-0.0005",
            "accountId": "should-be-dropped",   # 不在白名单
            "userId": "should-also-drop",        # 不在白名单
            "random_field": "xyz",                # 不在白名单
        })
        fills = self._call_parse({"data": [row]})
        raw = fills[0].raw_exchange
        self.assertIsNotNone(raw)
        self.assertIn("feeRate", raw)
        self.assertNotIn("accountId", raw)
        self.assertNotIn("userId", raw)
        self.assertNotIn("random_field", raw)

    def test_empty_whitelist_fields_result_in_none(self) -> None:
        """如果白名单字段都缺失, raw_exchange 应为 None (不是 {})。"""
        row = self._base_row()
        # 移除所有白名单字段 (base_row 里有 tradeId 在白名单)
        for key in ("feeRate", "execType", "liquidity", "ordType", "posSide", "tradeId"):
            row.pop(key, None)
        # 仍需要一个 fallback ID, 添加 billId
        row["billId"] = "b999"
        fills = self._call_parse({"data": [row]})
        self.assertIsNone(fills[0].raw_exchange)

    def test_none_and_empty_string_values_ignored(self) -> None:
        """None 或空字符串不应该进 raw_exchange。"""
        row = self._base_row()
        row.update({
            "feeRate": "-0.0005",
            "execType": None,        # 不应进
            "liquidity": "",          # 不应进
            "ordType": "market",
        })
        fills = self._call_parse({"data": [row]})
        raw = fills[0].raw_exchange
        self.assertIsNotNone(raw)
        self.assertIn("feeRate", raw)
        self.assertIn("ordType", raw)
        self.assertNotIn("execType", raw)
        self.assertNotIn("liquidity", raw)

    def test_existing_fields_still_work(self) -> None:
        """其余 ExchangeFill 字段 (fill_qty, fill_price, fee_amount) 仍正常填充。"""
        row = self._base_row()
        row["feeRate"] = "-0.0005"
        fills = self._call_parse({"data": [row]})
        fill = fills[0]
        # fill_id 取 tradeId
        self.assertEqual(fill.fill_id, "t123")
        self.assertEqual(fill.exchange_order_id, "ord_abc")
        self.assertEqual(fill.fill_price, Decimal("75000"))
        # fee_amount 取负号 (OKX: fee 为正 → ExchangeFill 记为负)
        self.assertEqual(fill.fee_amount, Decimal("-0.375"))
        self.assertEqual(fill.fee_currency, "USDT")


if __name__ == "__main__":
    unittest.main()
