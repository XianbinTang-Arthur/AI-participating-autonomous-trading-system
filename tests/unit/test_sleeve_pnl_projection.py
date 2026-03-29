from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
import unittest

from aats.schemas.common import utc_now
from aats.schemas.execution import FillEvent
from aats.schemas.portfolio import FillOutcomeRecord, FundingFeeRecord
from aats.schemas.strategy_runtime import StrategySleeveRecord
from aats.services.runtime_scope import RuntimeStateScope
from aats.services.strategy_engines.sleeve_pnl_projection import SleevePnLProjectionService
from aats.storage.execution_repo import InMemoryExecutionRepository
from aats.storage.fill_outcome_repo import InMemoryFillOutcomeRepository
from aats.storage.funding_fee_repo import InMemoryFundingFeeRepository
from aats.storage.sleeve_pnl_repo import InMemorySleevePnLRepository
from aats.storage.strategy_sleeve_repo import InMemoryStrategySleeveRepository


class TestSleevePnLProjection(unittest.TestCase):
    def test_projection_tracks_fill_and_split_funding_fee_by_open_inventory(self) -> None:
        now = utc_now()
        fill_outcome_repo = InMemoryFillOutcomeRepository()
        funding_fee_repo = InMemoryFundingFeeRepository()
        sleeve_pnl_repo = InMemorySleevePnLRepository()
        execution_repo = InMemoryExecutionRepository()
        strategy_sleeve_repo = InMemoryStrategySleeveRepository()
        strategy_sleeve_repo.save_sleeve(
            StrategySleeveRecord(
                sleeve_id="directional_btc_core",
                family="directional",
                name="directional_btc_core",
                product_scope="derivatives",
                margin_scope="cross",
                symbol_scope=("BTC-USDT-SWAP",),
            )
        )
        strategy_sleeve_repo.save_sleeve(
            StrategySleeveRecord(
                sleeve_id="smart_arbitrage_btc_pair",
                family="smart_arbitrage",
                name="smart_arbitrage_btc_pair",
                product_scope="derivatives",
                margin_scope="cross",
                symbol_scope=("BTC-USDT-SWAP",),
            )
        )

        fill_a = FillEvent(
            fill_id="fill_open_a",
            decision_id="decision_a",
            intent_id="intent_a",
            client_order_id="order_a",
            exchange_order_id="venue_a",
            symbol="BTC-USDT-SWAP",
            venue="OKX",
            side="buy",
            fill_qty=Decimal("1"),
            fill_price=Decimal("100"),
            fee_amount=Decimal("0.10"),
            fee_currency="USDT",
            liquidity_role="taker",
            exchange_timestamp=now - timedelta(minutes=10),
            ingestion_timestamp=now - timedelta(minutes=10),
            order_status_after_fill="FILLED",
            strategy_family="directional",
            strategy_sleeve_id="directional_btc_core",
            allocation_id="alloc_a",
            strategy_bundle_id="bundle_a",
            strategy_leg_role="primary",
            target_leverage=3.0,
            exposure_side="long",
            execution_action="enter",
            position_intent="open_long",
            product_type="derivatives",
            margin_mode="cross",
        )
        fill_b = FillEvent(
            fill_id="fill_open_b",
            decision_id="decision_b",
            intent_id="intent_b",
            client_order_id="order_b",
            exchange_order_id="venue_b",
            symbol="BTC-USDT-SWAP",
            venue="OKX",
            side="buy",
            fill_qty=Decimal("2"),
            fill_price=Decimal("100"),
            fee_amount=Decimal("0.20"),
            fee_currency="USDT",
            liquidity_role="taker",
            exchange_timestamp=now - timedelta(minutes=9),
            ingestion_timestamp=now - timedelta(minutes=9),
            order_status_after_fill="FILLED",
            strategy_family="smart_arbitrage",
            strategy_sleeve_id="smart_arbitrage_btc_pair",
            allocation_id="alloc_b",
            strategy_bundle_id="bundle_b",
            strategy_leg_role="hedge",
            target_leverage=3.0,
            exposure_side="long",
            execution_action="enter",
            position_intent="open_long",
            product_type="derivatives",
            margin_mode="cross",
        )
        execution_repo.save_fill(fill_a)
        execution_repo.save_fill(fill_b)
        fill_outcome_repo.save_outcome(
            FillOutcomeRecord(
                fill_id="fill_open_a",
                decision_id="decision_a",
                intent_id="intent_a",
                order_id="order_a",
                symbol="BTC-USDT-SWAP",
                venue="OKX",
                side="buy",
                fill_qty=Decimal("1"),
                fill_price=Decimal("100"),
                fill_notional=Decimal("100"),
                fee_amount=Decimal("0.10"),
                fee_currency="USDT",
                liquidity_role="taker",
                exchange_timestamp=now - timedelta(minutes=10),
                ingestion_timestamp=now - timedelta(minutes=10),
                order_status_after_fill="FILLED",
                strategy_family="directional",
                strategy_sleeve_id="directional_btc_core",
                allocation_id="alloc_a",
                strategy_bundle_id="bundle_a",
                strategy_leg_role="primary",
                target_leverage=3.0,
                exposure_side="long",
                execution_action="open_long",
                position_intent="open_long",
                starting_position_qty=Decimal("0"),
                ending_position_qty=Decimal("1"),
                realized_pnl_delta=Decimal("0"),
                fee_delta=Decimal("0.10"),
                product_type="derivatives",
                margin_mode="cross",
                created_at=now - timedelta(minutes=10),
            )
        )
        fill_outcome_repo.save_outcome(
            FillOutcomeRecord(
                fill_id="fill_open_b",
                decision_id="decision_b",
                intent_id="intent_b",
                order_id="order_b",
                symbol="BTC-USDT-SWAP",
                venue="OKX",
                side="buy",
                fill_qty=Decimal("2"),
                fill_price=Decimal("100"),
                fill_notional=Decimal("200"),
                fee_amount=Decimal("0.20"),
                fee_currency="USDT",
                liquidity_role="taker",
                exchange_timestamp=now - timedelta(minutes=9),
                ingestion_timestamp=now - timedelta(minutes=9),
                order_status_after_fill="FILLED",
                strategy_family="smart_arbitrage",
                strategy_sleeve_id="smart_arbitrage_btc_pair",
                allocation_id="alloc_b",
                strategy_bundle_id="bundle_b",
                strategy_leg_role="hedge",
                target_leverage=3.0,
                exposure_side="long",
                execution_action="open_long",
                position_intent="open_long",
                starting_position_qty=Decimal("0"),
                ending_position_qty=Decimal("2"),
                realized_pnl_delta=Decimal("0"),
                fee_delta=Decimal("0.20"),
                product_type="derivatives",
                margin_mode="cross",
                created_at=now - timedelta(minutes=9),
            )
        )
        funding_fee_repo.save_record(
            FundingFeeRecord(
                bill_id="bill_funding_split",
                symbol="BTC-USDT-SWAP",
                currency="USDT",
                amount=Decimal("-3"),
                bill_type="8",
                sub_type="173",
                type_label="funding_fee",
                sub_type_label="funding_fee_expense",
                funding_direction="expense",
                bill_ts=now - timedelta(minutes=5),
                ledger_posting_state="POSTED",
                product_type="derivatives",
                margin_mode="cross",
            )
        )

        service = SleevePnLProjectionService(
            fill_outcome_repo=fill_outcome_repo,
            funding_fee_repo=funding_fee_repo,
            sleeve_pnl_repo=sleeve_pnl_repo,
            execution_repo=execution_repo,
            strategy_sleeve_repo=strategy_sleeve_repo,
        )
        scope = RuntimeStateScope(
            product_type="derivatives",
            margin_mode="cross",
            allowed_symbols=("BTC-USDT-SWAP",),
            default_symbol="BTC-USDT-SWAP",
        )

        records = service.rebuild_scope(scope=scope)

        self.assertEqual(len(records), 4)
        direct_records = [record for record in records if record.event_type == "fill_realization"]
        self.assertEqual(len(direct_records), 2)
        self.assertTrue(any(record.strategy_sleeve_id == "directional_btc_core" for record in direct_records))
        self.assertTrue(any(record.strategy_sleeve_id == "smart_arbitrage_btc_pair" for record in direct_records))
        funding_records = [record for record in records if record.event_type == "funding_fee"]
        self.assertEqual(len(funding_records), 2)
        funding_by_sleeve = {record.strategy_sleeve_id: record.funding_fee_amount for record in funding_records}
        self.assertEqual(funding_by_sleeve["directional_btc_core"], Decimal("-1"))
        self.assertEqual(funding_by_sleeve["smart_arbitrage_btc_pair"], Decimal("-2"))
        self.assertEqual(
            sum((record.funding_fee_amount for record in funding_records), start=Decimal("0")),
            Decimal("-3"),
        )


if __name__ == "__main__":
    unittest.main()
