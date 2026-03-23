from __future__ import annotations

import os
import unittest
from decimal import Decimal

from sqlalchemy import func, select

from aats.schemas.common import utc_now
from aats.schemas.execution import FillEvent
from aats.services.ledger.lot_projection import LotBasedProjectionBuilder
from aats.services.ledger.persistent_lot_book import PersistentLotBookService
from aats.storage.lot_repo_postgres import PostgresLotEventRepository, PostgresPositionLotRepository
from aats.storage.sqlalchemy_models import LotEventModel
from tests.support.postgres import temporary_postgres_runtime


def _fill(*, fill_id: str, side: str, qty: str, price: str) -> FillEvent:
    timestamp = utc_now()
    return FillEvent(
        fill_id=fill_id,
        decision_id=f"decision_{fill_id}",
        intent_id=f"intent_{fill_id}",
        client_order_id=f"order_{fill_id}",
        exchange_order_id=f"venue_{fill_id}",
        symbol="BTC-USDT",
        venue="PAPER",
        side=side,  # type: ignore[arg-type]
        fill_qty=Decimal(qty),
        fill_price=Decimal(price),
        fee_amount=Decimal("0"),
        fee_currency="USDT",
        product_type="spot",
        target_leverage=1.0,
        margin_mode="cash",
        exposure_side="long" if side == "buy" else "short",
        execution_action="enter",
        position_intent="open_long" if side == "buy" else "close_long",
        liquidity_role="taker",
        exchange_timestamp=timestamp,
        ingestion_timestamp=timestamp,
        order_status_after_fill="FILLED",
    )


@unittest.skipUnless(os.getenv("AATS_DATABASE_URL"), "AATS_DATABASE_URL is required for PostgreSQL-backed tests")
class TestTask58PersistentLotBook(unittest.TestCase):
    def test_rebuild_from_fills_persists_open_lots_and_lot_events(self) -> None:
        with temporary_postgres_runtime() as (runtime, _admin_engine, _schema_name):
                position_repo = PostgresPositionLotRepository(runtime.session_factory)
                event_repo = PostgresLotEventRepository(runtime.session_factory)
                service = PersistentLotBookService(
                    position_lot_repo=position_repo,
                    lot_event_repo=event_repo,
                    projection_builder=LotBasedProjectionBuilder(),
                )
                fills = [
                    _fill(fill_id="fill_buy_1", side="buy", qty="1", price="100"),
                    _fill(fill_id="fill_buy_2", side="buy", qty="1", price="110"),
                    _fill(fill_id="fill_sell_1", side="sell", qty="1.5", price="120"),
                    _fill(fill_id="fill_sell_2", side="sell", qty="1", price="90"),
                ]

                service.rebuild_from_fills(
                    fills=fills,
                    product_type="spot",
                    margin_mode="cash",
                )

                lots = position_repo.lots_for_scope(
                    symbol="BTC-USDT",
                    product_type="spot",
                    margin_mode="cash",
                    open_only=True,
                )
                self.assertEqual(len(lots), 1)
                self.assertEqual(Decimal(str(lots[0]["signed_quantity_open"])), Decimal("-0.5"))
                self.assertEqual(Decimal(str(lots[0]["entry_price"])), Decimal("90"))
                original_lot_id = str(lots[0]["lot_id"])

                terminal_fill_events = event_repo.events_for_fill("fill_sell_2")
                self.assertEqual({event["event_type"] for event in terminal_fill_events}, {"close", "open"})
                original_event_ids = {str(event["event_id"]) for event in terminal_fill_events}

                service.rebuild_from_fills(
                    fills=fills,
                    product_type="spot",
                    margin_mode="cash",
                )

                rebuilt_lots = position_repo.lots_for_scope(
                    symbol="BTC-USDT",
                    product_type="spot",
                    margin_mode="cash",
                    open_only=True,
                )
                rebuilt_events = event_repo.events_for_fill("fill_sell_2")
                self.assertEqual(str(rebuilt_lots[0]["lot_id"]), original_lot_id)
                self.assertEqual({str(event["event_id"]) for event in rebuilt_events}, original_event_ids)

    def test_lot_event_scope_replace_does_not_delete_other_scope_rows(self) -> None:
        with temporary_postgres_runtime() as (runtime, _admin_engine, _schema_name):
                position_repo = PostgresPositionLotRepository(runtime.session_factory)
                event_repo = PostgresLotEventRepository(runtime.session_factory)

                position_repo.replace_scope(
                    symbol="BTC-USDT",
                    product_type="spot",
                    margin_mode="cash",
                    lots=[
                        {
                            "lot_id": "lot_spot",
                            "signed_quantity_open": Decimal("1"),
                            "entry_price": Decimal("100"),
                            "source_fill_id": "fill_spot",
                            "target_leverage": 1.0,
                            "exposure_side": "long",
                            "status": "OPEN",
                            "opened_at": utc_now(),
                            "closed_at": None,
                            "updated_at": utc_now(),
                            "metadata": {},
                        }
                    ],
                )
                event_repo.replace_scope(
                    symbol="BTC-USDT",
                    product_type="spot",
                    margin_mode="cash",
                    events=[
                        {
                            "event_id": "evt_spot",
                            "fill_id": "fill_spot",
                            "lot_id": "lot_spot",
                            "event_type": "open",
                            "quantity": Decimal("1"),
                            "entry_price": Decimal("100"),
                            "exit_price": None,
                            "realized_pnl_delta": Decimal("0"),
                            "created_at": utc_now(),
                            "payload": {},
                        }
                    ],
                )

                position_repo.replace_scope(
                    symbol="BTC-USDT",
                    product_type="derivatives",
                    margin_mode="isolated",
                    lots=[
                        {
                            "lot_id": "lot_perp",
                            "signed_quantity_open": Decimal("-1"),
                            "entry_price": Decimal("110"),
                            "source_fill_id": "fill_perp",
                            "target_leverage": 3.0,
                            "exposure_side": "short",
                            "status": "OPEN",
                            "opened_at": utc_now(),
                            "closed_at": None,
                            "updated_at": utc_now(),
                            "metadata": {},
                        }
                    ],
                )
                event_repo.replace_scope(
                    symbol="BTC-USDT",
                    product_type="derivatives",
                    margin_mode="isolated",
                    events=[
                        {
                            "event_id": "evt_perp",
                            "fill_id": "fill_perp",
                            "lot_id": "lot_perp",
                            "event_type": "open",
                            "quantity": Decimal("1"),
                            "entry_price": Decimal("110"),
                            "exit_price": None,
                            "realized_pnl_delta": Decimal("0"),
                            "created_at": utc_now(),
                            "payload": {},
                        }
                    ],
                )

                self.assertEqual(len(event_repo.events_for_fill("fill_spot")), 1)
                self.assertEqual(len(event_repo.events_for_fill("fill_perp")), 1)
                with runtime.session_factory() as session:
                    self.assertEqual(session.scalar(select(func.count()).select_from(LotEventModel)), 2)


if __name__ == "__main__":
    unittest.main()
