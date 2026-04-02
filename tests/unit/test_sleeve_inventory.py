from __future__ import annotations

from decimal import Decimal
import unittest

from aats.schemas.common import utc_now
from aats.schemas.execution import FillEvent
from aats.services.strategy_engines.sleeve_identity import build_strategy_sleeve_id
from aats.services.strategy_engines.sleeve_inventory import StrategySleeveInventoryService
from aats.storage.execution_repo import InMemoryExecutionRepository


def _fill_event(
    *,
    fill_id: str,
    symbol: str,
    side: str,
    qty: str,
    product_type: str,
    margin_mode: str,
    strategy_sleeve_id: str,
) -> FillEvent:
    timestamp = utc_now()
    return FillEvent(
        fill_id=fill_id,
        decision_id="decision_test",
        intent_id=f"intent_{fill_id}",
        client_order_id=f"client_{fill_id}",
        exchange_order_id=f"exchange_{fill_id}",
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        fill_qty=Decimal(qty),
        fill_price=Decimal("100"),
        fee_amount=Decimal("0"),
        fee_currency="USDT",
        strategy_family="smart_arbitrage",
        strategy_sleeve_id=strategy_sleeve_id,
        product_type=product_type,  # type: ignore[arg-type]
        margin_mode=margin_mode,  # type: ignore[arg-type]
        exposure_side="long" if side == "buy" else "short",
        execution_action="enter",
        liquidity_role="maker",
        exchange_timestamp=timestamp,
        ingestion_timestamp=timestamp,
        position_intent="open_long" if side == "buy" else "open_short",
        td_mode=margin_mode,  # type: ignore[arg-type]
        settle_currency="USDT",
    )


class TestStrategySleeveInventoryService(unittest.TestCase):
    def test_quantity_for_strategy_uses_family_scope_and_leg_scope_together(self) -> None:
        execution_repo = InMemoryExecutionRepository()
        sleeve_id = build_strategy_sleeve_id(
            family="smart_arbitrage",
            primary_symbol="BTC-USDT-SWAP",
            product_scope="derivatives",
            margin_scope="cross",
            symbol_scope=("BTC-USDT", "BTC-USDT-SWAP"),
        )
        execution_repo.save_fill(
            _fill_event(
                fill_id="fill_spot",
                symbol="BTC-USDT",
                side="buy",
                qty="0.5",
                product_type="spot",
                margin_mode="cash",
                strategy_sleeve_id=sleeve_id,
            )
        )
        execution_repo.save_fill(
            _fill_event(
                fill_id="fill_hedge",
                symbol="BTC-USDT-SWAP",
                side="sell",
                qty="0.5",
                product_type="derivatives",
                margin_mode="cross",
                strategy_sleeve_id=sleeve_id,
            )
        )
        service = StrategySleeveInventoryService(execution_repo=execution_repo)

        spot_qty = service.quantity_for_strategy(
            family="smart_arbitrage",
            primary_symbol="BTC-USDT-SWAP",
            product_scope="derivatives",
            margin_scope="cross",
            symbol_scope=("BTC-USDT", "BTC-USDT-SWAP"),
            symbol="BTC-USDT",
            product_type="spot",
            margin_mode="cash",
        )
        wrong_scope_qty = service.quantity_for_strategy(
            family="smart_arbitrage",
            primary_symbol="BTC-USDT-SWAP",
            product_scope="spot",
            margin_scope="cash",
            symbol_scope=("BTC-USDT", "BTC-USDT-SWAP"),
            symbol="BTC-USDT",
            product_type="spot",
            margin_mode="cash",
        )

        self.assertEqual(spot_qty, Decimal("0.5"))
        self.assertEqual(wrong_scope_qty, Decimal("0"))


if __name__ == "__main__":
    unittest.main()
