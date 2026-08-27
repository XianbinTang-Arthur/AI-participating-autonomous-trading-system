"""Unit tests for backtest PositionTracker.

覆盖开仓 / 加仓 / 减仓 / 平仓 / 翻仓 / 盯市 / fee 分离 / 快照不可变 /
空 fill 边界。所有 PnL 值按 Decimal assertEqual。

线性与反向合约均通过显式 ``InstrumentContract`` 固定数量、PnL 与币种语义。
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from decimal import Decimal, localcontext

from aats.data_platform.replay.backtest.position_tracker import (
    Fill,
    PositionSnapshot,
    PositionTracker,
)
from aats.domain.instrument_contract import InstrumentContractError
from tests.unit.replay_contract_fixtures import (
    INVERSE_SWAP_CONTRACT,
    LINEAR_SWAP_CONTRACT,
    SPOT_CONTRACT,
)


def _fill(
    side: str,
    qty: str,
    price: str,
    *,
    fee: str = "0",
    fee_currency: str = "USDT",
    fee_asset: str | None = None,
    fee_asset_quantity: str | None = None,
    instrument_contract=LINEAR_SWAP_CONTRACT,
    ts_ms: int = 0,
) -> Fill:
    """Test helper：快速造 Fill。"""
    return Fill(
        side=side,  # type: ignore[arg-type]
        filled_qty=Decimal(qty),
        avg_fill_price=Decimal(price),
        fee_notional=Decimal(fee),
        fee_currency=fee_currency,
        instrument_symbol=instrument_contract.symbol,
        instrument_contract_fingerprint=instrument_contract.fingerprint,
        ts_ms=ts_ms,
        fee_asset=fee_asset,
        fee_asset_quantity=(
            None if fee_asset_quantity is None else Decimal(fee_asset_quantity)
        ),
    )


def _tracker() -> PositionTracker:
    return PositionTracker(instrument_contract=LINEAR_SWAP_CONTRACT)


class OpenAndAddTests(unittest.TestCase):
    def test_open_long_from_flat(self) -> None:
        tracker = _tracker()
        snap = tracker.apply_fill(_fill("buy", "10", "100", ts_ms=1_000))

        self.assertEqual(snap.net_qty, Decimal("10"))
        self.assertEqual(snap.avg_entry_price, Decimal("100"))
        self.assertEqual(snap.realized_pnl, Decimal("0"))
        self.assertEqual(snap.unrealized_pnl, Decimal("0"))  # mark = fill price
        self.assertEqual(snap.last_mark_price, Decimal("100"))
        self.assertEqual(snap.accumulated_fees, Decimal("0"))
        self.assertEqual(snap.fill_count, 1)
        self.assertEqual(snap.ts_ms, 1_000)

    def test_open_short_from_flat(self) -> None:
        tracker = _tracker()
        snap = tracker.apply_fill(_fill("sell", "5", "200"))

        self.assertEqual(snap.net_qty, Decimal("-5"))
        self.assertEqual(snap.avg_entry_price, Decimal("200"))
        self.assertEqual(snap.realized_pnl, Decimal("0"))
        self.assertEqual(snap.unrealized_pnl, Decimal("0"))

    def test_add_to_long_updates_avg(self) -> None:
        tracker = _tracker()
        tracker.apply_fill(_fill("buy", "10", "100"))
        snap = tracker.apply_fill(_fill("buy", "10", "120"))

        # WAC: (10*100 + 10*120) / 20 = 110
        self.assertEqual(snap.net_qty, Decimal("20"))
        self.assertEqual(snap.avg_entry_price, Decimal("110"))
        self.assertEqual(snap.realized_pnl, Decimal("0"))

    def test_add_to_short_updates_avg(self) -> None:
        tracker = _tracker()
        tracker.apply_fill(_fill("sell", "4", "200"))
        snap = tracker.apply_fill(_fill("sell", "6", "220"))

        # WAC: (4*200 + 6*220) / 10 = 212
        self.assertEqual(snap.net_qty, Decimal("-10"))
        self.assertEqual(snap.avg_entry_price, Decimal("212"))
        self.assertEqual(snap.realized_pnl, Decimal("0"))


class ReduceAndCloseTests(unittest.TestCase):
    def test_reduce_long_realizes_pnl(self) -> None:
        tracker = _tracker()
        tracker.apply_fill(_fill("buy", "10", "100"))
        snap = tracker.apply_fill(_fill("sell", "4", "150"))

        # realized = 4 * (150 - 100) * (+1) * 0.01 = 2.0
        self.assertEqual(snap.net_qty, Decimal("6"))
        self.assertEqual(snap.avg_entry_price, Decimal("100"))  # 基线不变
        self.assertEqual(snap.realized_pnl, Decimal("2.00"))
        # unrealized at mark=150: 6 * (150-100) * 1 * 0.01 = 3.0
        self.assertEqual(snap.unrealized_pnl, Decimal("3.00"))

    def test_close_long_zeroes_position(self) -> None:
        tracker = _tracker()
        tracker.apply_fill(_fill("buy", "10", "100"))
        snap = tracker.apply_fill(_fill("sell", "10", "130"))

        # realized = 10 * (130-100) * 1 * 0.01 = 3.0
        self.assertEqual(snap.net_qty, Decimal("0"))
        self.assertEqual(snap.avg_entry_price, Decimal("0"))
        self.assertEqual(snap.realized_pnl, Decimal("3.00"))
        self.assertEqual(snap.unrealized_pnl, Decimal("0"))  # flat → no unrealized

    def test_reduce_short_realizes_pnl(self) -> None:
        tracker = _tracker()
        tracker.apply_fill(_fill("sell", "10", "200"))
        snap = tracker.apply_fill(_fill("buy", "3", "150"))

        # realized = 3 * (150-200) * (-1) * 0.01 = 1.5
        self.assertEqual(snap.net_qty, Decimal("-7"))
        self.assertEqual(snap.avg_entry_price, Decimal("200"))  # 基线不变
        self.assertEqual(snap.realized_pnl, Decimal("1.50"))


class ReverseTests(unittest.TestCase):
    def test_reverse_long_to_short(self) -> None:
        tracker = _tracker()
        tracker.apply_fill(_fill("buy", "10", "100"))
        # 卖出 15：先平 10，再开空 5
        snap = tracker.apply_fill(_fill("sell", "15", "120"))

        # 原仓 PnL = 10 * (120-100) * 1 * 0.01 = 2.0
        self.assertEqual(snap.realized_pnl, Decimal("2.00"))
        # 新仓：5 contracts short @ 120
        self.assertEqual(snap.net_qty, Decimal("-5"))
        self.assertEqual(snap.avg_entry_price, Decimal("120"))
        # unrealized at mark=120: 0
        self.assertEqual(snap.unrealized_pnl, Decimal("0"))

    def test_reverse_short_to_long(self) -> None:
        tracker = _tracker()
        tracker.apply_fill(_fill("sell", "8", "200"))
        # 买入 12：先平 8，再开多 4
        snap = tracker.apply_fill(_fill("buy", "12", "180"))

        # 原仓 PnL = 8 * (180-200) * (-1) * 0.01 = 1.6
        self.assertEqual(snap.realized_pnl, Decimal("1.60"))
        self.assertEqual(snap.net_qty, Decimal("4"))
        self.assertEqual(snap.avg_entry_price, Decimal("180"))


class ShortSideSymmetryTests(unittest.TestCase):
    def test_short_side_symmetric(self) -> None:
        """short path 与 long path 对称：开 → 减 → 平，PnL 符号正确。"""
        tracker = _tracker()

        # 开空 10 @ 200
        s1 = tracker.apply_fill(_fill("sell", "10", "200"))
        self.assertEqual(s1.net_qty, Decimal("-10"))
        self.assertEqual(s1.avg_entry_price, Decimal("200"))

        # 价格跌到 180 → 买回 4：realized = 4*(180-200)*(-1)*0.01 = 0.8
        s2 = tracker.apply_fill(_fill("buy", "4", "180"))
        self.assertEqual(s2.net_qty, Decimal("-6"))
        self.assertEqual(s2.realized_pnl, Decimal("0.80"))
        self.assertEqual(s2.avg_entry_price, Decimal("200"))

        # 价格继续跌到 170 → 买回 6（平仓）：realized += 6*(170-200)*(-1)*0.01 = 1.8
        s3 = tracker.apply_fill(_fill("buy", "6", "170"))
        self.assertEqual(s3.net_qty, Decimal("0"))
        self.assertEqual(s3.avg_entry_price, Decimal("0"))
        self.assertEqual(s3.realized_pnl, Decimal("2.60"))  # 0.8 + 1.8
        self.assertEqual(s3.unrealized_pnl, Decimal("0"))


class MarkToMarketTests(unittest.TestCase):
    def test_mark_to_market_updates_unrealized(self) -> None:
        tracker = _tracker()
        tracker.apply_fill(_fill("buy", "10", "100", ts_ms=1_000))

        snap = tracker.mark_to_market(Decimal("150"), ts_ms=2_000)

        # 仓位不动
        self.assertEqual(snap.net_qty, Decimal("10"))
        self.assertEqual(snap.avg_entry_price, Decimal("100"))
        self.assertEqual(snap.realized_pnl, Decimal("0"))
        # unrealized = 10 * (150-100) * 1 * 0.01 = 5.0
        self.assertEqual(snap.unrealized_pnl, Decimal("5.00"))
        self.assertEqual(snap.last_mark_price, Decimal("150"))
        self.assertEqual(snap.ts_ms, 2_000)
        # fill_count 不加
        self.assertEqual(snap.fill_count, 1)

    def test_mark_to_market_on_flat_position(self) -> None:
        tracker = _tracker()
        snap = tracker.mark_to_market(Decimal("100"), ts_ms=500)

        self.assertEqual(snap.net_qty, Decimal("0"))
        self.assertEqual(snap.unrealized_pnl, Decimal("0"))

    def test_mark_to_market_rejects_non_positive_price(self) -> None:
        tracker = _tracker()
        with self.assertRaises(ValueError):
            tracker.mark_to_market(Decimal("0"), ts_ms=1)
        with self.assertRaises(ValueError):
            tracker.mark_to_market(Decimal("-1"), ts_ms=1)


class FeesTests(unittest.TestCase):
    def test_fees_accumulate_separately(self) -> None:
        """fee 独立累计，**不**污染 realized_pnl。"""
        tracker = _tracker()
        tracker.apply_fill(_fill("buy", "10", "100", fee="0.5"))
        snap = tracker.apply_fill(_fill("sell", "10", "120", fee="0.6"))

        # realized = 10 * (120-100) * 1 * 0.01 = 2.0（无 fee 干扰）
        self.assertEqual(snap.realized_pnl, Decimal("2.00"))
        # fees 独立总和
        self.assertEqual(snap.accumulated_fees, Decimal("1.1"))


class SnapshotImmutabilityTests(unittest.TestCase):
    def test_snapshot_property_is_immutable(self) -> None:
        tracker = _tracker()
        tracker.apply_fill(_fill("buy", "10", "100"))

        s1 = tracker.snapshot
        s2 = tracker.snapshot

        # 多次调用返回等价快照（值相等）
        self.assertEqual(s1, s2)
        # frozen dataclass：不可变
        with self.assertRaises(Exception):
            s1.net_qty = Decimal("999")  # type: ignore[misc]

        # snapshot 不会 side-effect 更新内部
        self.assertEqual(tracker.snapshot.net_qty, Decimal("10"))

    def test_snapshot_type(self) -> None:
        tracker = _tracker()
        self.assertIsInstance(tracker.snapshot, PositionSnapshot)


class SequenceTests(unittest.TestCase):
    def test_sequence_long_then_add_then_reduce(self) -> None:
        """三步操作：开多 → 加多 → 减多，每步 snapshot 都正确。"""
        tracker = _tracker()

        # 步 1：开多 10 @ 100
        s1 = tracker.apply_fill(_fill("buy", "10", "100", ts_ms=1))
        self.assertEqual(s1.net_qty, Decimal("10"))
        self.assertEqual(s1.avg_entry_price, Decimal("100"))
        self.assertEqual(s1.realized_pnl, Decimal("0"))

        # 步 2：加多 10 @ 120 → WAC = 110
        s2 = tracker.apply_fill(_fill("buy", "10", "120", ts_ms=2))
        self.assertEqual(s2.net_qty, Decimal("20"))
        self.assertEqual(s2.avg_entry_price, Decimal("110"))
        self.assertEqual(s2.realized_pnl, Decimal("0"))

        # 步 3：减多 8 @ 130 → realized = 8*(130-110)*1*0.01 = 1.6
        s3 = tracker.apply_fill(_fill("sell", "8", "130", ts_ms=3))
        self.assertEqual(s3.net_qty, Decimal("12"))
        self.assertEqual(s3.avg_entry_price, Decimal("110"))  # 基线不变
        self.assertEqual(s3.realized_pnl, Decimal("1.60"))
        # unrealized at mark=130: 12 * (130-110) * 1 * 0.01 = 2.4
        self.assertEqual(s3.unrealized_pnl, Decimal("2.40"))
        self.assertEqual(s3.fill_count, 3)

    def test_sequence_reverse_then_close(self) -> None:
        """翻仓后 close 路径。"""
        tracker = _tracker()

        # 开多 10 @ 100
        tracker.apply_fill(_fill("buy", "10", "100"))
        # 翻为空 5 @ 120（realized = 10*(120-100)*1*0.01 = 2.0）
        s_rev = tracker.apply_fill(_fill("sell", "15", "120"))
        self.assertEqual(s_rev.net_qty, Decimal("-5"))
        self.assertEqual(s_rev.avg_entry_price, Decimal("120"))
        self.assertEqual(s_rev.realized_pnl, Decimal("2.00"))

        # 平空 5 @ 110（realized += 5*(110-120)*(-1)*0.01 = 0.5 → 2.5）
        s_close = tracker.apply_fill(_fill("buy", "5", "110"))
        self.assertEqual(s_close.net_qty, Decimal("0"))
        self.assertEqual(s_close.avg_entry_price, Decimal("0"))
        self.assertEqual(s_close.realized_pnl, Decimal("2.50"))
        self.assertEqual(s_close.unrealized_pnl, Decimal("0"))


class ValidationTests(unittest.TestCase):
    def test_zero_qty_fill_raises(self) -> None:
        tracker = _tracker()
        with self.assertRaises(ValueError):
            tracker.apply_fill(_fill("buy", "0", "100"))

    def test_negative_qty_fill_raises(self) -> None:
        tracker = _tracker()
        with self.assertRaises(ValueError):
            tracker.apply_fill(_fill("buy", "-1", "100"))

    def test_non_positive_price_raises(self) -> None:
        tracker = _tracker()
        with self.assertRaises(ValueError):
            tracker.apply_fill(_fill("buy", "1", "0"))
        with self.assertRaises(ValueError):
            tracker.apply_fill(_fill("buy", "1", "-10"))

    def test_negative_fee_is_preserved_as_maker_rebate(self) -> None:
        tracker = _tracker()
        snap = tracker.apply_fill(_fill("buy", "1", "100", fee="-0.1"))

        self.assertEqual(snap.accumulated_fees, Decimal("-0.1"))

    def test_invalid_side_raises(self) -> None:
        tracker = _tracker()
        bad = Fill(
            side="hold",  # type: ignore[arg-type]
            filled_qty=Decimal("1"),
            avg_fill_price=Decimal("100"),
            fee_notional=Decimal("0"),
            fee_currency="USDT",
            instrument_symbol=LINEAR_SWAP_CONTRACT.symbol,
            instrument_contract_fingerprint=LINEAR_SWAP_CONTRACT.fingerprint,
            ts_ms=1,
        )
        with self.assertRaises(ValueError):
            tracker.apply_fill(bad)

    def test_instrument_contract_is_required(self) -> None:
        with self.assertRaisesRegex(ValueError, "instrument_contract_required"):
            PositionTracker(None)  # type: ignore[arg-type]

    def test_fee_currency_must_match_settlement_currency(self) -> None:
        tracker = _tracker()
        with self.assertRaisesRegex(ValueError, "fill_fee_currency_mismatch"):
            tracker.apply_fill(
                _fill("buy", "1", "100", fee="0.1", fee_currency="BTC")
            )

    def test_spot_base_fee_value_must_match_settlement_valuation(self) -> None:
        tracker = PositionTracker(instrument_contract=SPOT_CONTRACT)
        with self.assertRaisesRegex(
            ValueError,
            "fill_fee_asset_settlement_value_mismatch",
        ):
            tracker.apply_fill(
                _fill(
                    "buy",
                    "1",
                    "100",
                    fee="0.1",
                    fee_asset="BTC",
                    fee_asset_quantity="0.002",
                    instrument_contract=SPOT_CONTRACT,
                )
            )

        self.assertEqual(tracker.snapshot.net_qty, Decimal("0"))
        self.assertEqual(tracker.snapshot.accumulated_fees, Decimal("0"))

    def test_fill_contract_identity_must_match_tracker(self) -> None:
        tracker = _tracker()
        with self.assertRaisesRegex(ValueError, "fill_instrument_symbol_mismatch"):
            tracker.apply_fill(
                _fill(
                    "buy",
                    "1",
                    "100",
                    instrument_contract=SPOT_CONTRACT,
                )
            )

    def test_direct_fill_must_respect_contract_lot_size(self) -> None:
        tracker = _tracker()
        with self.assertRaisesRegex(
            ValueError,
            "exchange_quantity_lot_misaligned",
        ):
            tracker.apply_fill(_fill("buy", "1.001", "100"))

    def test_spot_short_and_margin_accounting_fail_closed(self) -> None:
        spot_tracker = PositionTracker(instrument_contract=SPOT_CONTRACT)
        with self.assertRaisesRegex(ValueError, "spot_short_position_unavailable"):
            spot_tracker.apply_fill(
                _fill(
                    "sell",
                    "0.1",
                    "50000",
                    instrument_contract=SPOT_CONTRACT,
                )
            )
        margin_contract = replace(SPOT_CONTRACT, instrument_type="MARGIN")
        with self.assertRaisesRegex(ValueError, "margin_position_accounting_unavailable"):
            PositionTracker(instrument_contract=margin_contract)

    def test_apply_fill_rolls_back_all_state_on_arithmetic_failure(self) -> None:
        tracker = _tracker()
        tracker.apply_fill(_fill("buy", "1", "100", fee="9e9999"))
        before = tracker.snapshot

        with self.assertRaises(InstrumentContractError):
            tracker.apply_fill(_fill("buy", "1", "110", fee="9e9999"))

        self.assertEqual(tracker.snapshot, before)

    def test_position_result_is_independent_from_ambient_decimal_context(self) -> None:
        precise_contract = replace(
            LINEAR_SWAP_CONTRACT,
            lot_size=Decimal("0.00000001"),
            min_size=Decimal("0.00000001"),
        )

        def _run_with_precision(precision: int) -> PositionSnapshot:
            with localcontext() as ctx:
                ctx.prec = precision
                tracker = PositionTracker(instrument_contract=precise_contract)
                tracker.apply_fill(
                    _fill(
                        "sell",
                        "1.23456789",
                        "100",
                        instrument_contract=precise_contract,
                    )
                )
                return tracker.apply_fill(
                    _fill(
                        "buy",
                        "0.23456789",
                        "110",
                        instrument_contract=precise_contract,
                    )
                )

        self.assertEqual(_run_with_precision(8), _run_with_precision(50))

    def test_large_opposing_fills_do_not_create_inventory_by_rounding(self) -> None:
        unit_contract = replace(
            SPOT_CONTRACT,
            lot_size=Decimal("1"),
            min_size=Decimal("1"),
        )
        tracker = PositionTracker(instrument_contract=unit_contract)
        opened_qty = Decimal("9" * 53)
        tracker.apply_fill(
            _fill(
                "buy",
                str(opened_qty),
                "100",
                instrument_contract=unit_contract,
            )
        )
        reduced = tracker.apply_fill(
            _fill(
                "sell",
                "9" * 52 + "8",
                "100",
                instrument_contract=unit_contract,
            )
        )

        self.assertEqual(reduced.net_qty, Decimal("1"))


class InverseContractTests(unittest.TestCase):
    def test_inverse_add_uses_harmonic_entry_and_base_settlement_pnl(self) -> None:
        tracker = PositionTracker(instrument_contract=INVERSE_SWAP_CONTRACT)
        tracker.apply_fill(
            _fill(
                "buy",
                "1",
                "50000",
                fee_currency="BTC",
                instrument_contract=INVERSE_SWAP_CONTRACT,
            )
        )
        added = tracker.apply_fill(
            _fill(
                "buy",
                "1",
                "100000",
                fee_currency="BTC",
                instrument_contract=INVERSE_SWAP_CONTRACT,
            )
        )

        self.assertEqual(
            added.avg_entry_price,
            Decimal("66666.666666666666666666666666666666666666666666667"),
        )
        closed = tracker.apply_fill(
            _fill(
                "sell",
                "2",
                "80000",
                fee_currency="BTC",
                instrument_contract=INVERSE_SWAP_CONTRACT,
            )
        )
        self.assertEqual(closed.realized_pnl, Decimal("0.00050000000000000000000000000000000000000000000000000"))
        self.assertEqual(closed.settlement_currency, "BTC")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
