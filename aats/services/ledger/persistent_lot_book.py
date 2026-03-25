from __future__ import annotations

from dataclasses import dataclass

from aats.schemas.execution import FillEvent
from aats.services.ledger.lot_projection import LotBasedProjectionBuilder
from aats.storage.lot_repo import LotEventRepository, PositionLotRepository


@dataclass(slots=True)
class PersistentLotBookService:
    position_lot_repo: PositionLotRepository
    lot_event_repo: LotEventRepository
    projection_builder: LotBasedProjectionBuilder

    def rebuild_from_fills(
        self,
        *,
        fills: list[FillEvent],
        product_type: str,
        margin_mode: str,
    ) -> None:
        self._replace_lot_scopes(
            fills=fills,
            product_type=product_type,
            margin_mode=margin_mode,
            session=None,
        )

    def rebuild_from_fills_in_session(
        self,
        session,
        *,
        fills: list[FillEvent],
        product_type: str,
        margin_mode: str,
    ) -> None:
        self._replace_lot_scopes(
            fills=fills,
            product_type=product_type,
            margin_mode=margin_mode,
            session=session,
        )

    def _replace_lot_scopes(
        self,
        *,
        fills: list[FillEvent],
        product_type: str,
        margin_mode: str,
        session,
    ) -> None:
        fills_by_scope: dict[tuple[str, str, str], list[FillEvent]] = {}
        for fill in sorted(fills, key=lambda item: (item.ingestion_timestamp, item.fill_id)):
            scoped_product_type = str(fill.product_type or product_type)
            scoped_margin_mode = str(fill.margin_mode or margin_mode)
            fills_by_scope.setdefault((fill.symbol, scoped_product_type, scoped_margin_mode), []).append(fill)
        for (symbol, scoped_product_type, scoped_margin_mode), scoped_fills in fills_by_scope.items():
            lot_book = self.projection_builder.rebuild_lot_book(fills=scoped_fills)
            if (
                session is not None
                and hasattr(self.position_lot_repo, "replace_scope_in_session")
                and hasattr(self.lot_event_repo, "replace_scope_in_session")
            ):
                self.position_lot_repo.replace_scope_in_session(
                    session,
                    symbol=symbol,
                    product_type=scoped_product_type,
                    margin_mode=scoped_margin_mode,
                    lots=lot_book.lots,
                )
                self.lot_event_repo.replace_scope_in_session(
                    session,
                    symbol=symbol,
                    product_type=scoped_product_type,
                    margin_mode=scoped_margin_mode,
                    events=lot_book.events,
                )
                continue
            position_session_factory = getattr(self.position_lot_repo, "session_factory", None)
            event_session_factory = getattr(self.lot_event_repo, "session_factory", None)
            if (
                position_session_factory is not None
                and position_session_factory is event_session_factory
                and hasattr(self.position_lot_repo, "replace_scope_in_session")
                and hasattr(self.lot_event_repo, "replace_scope_in_session")
            ):
                with position_session_factory() as owned_session:
                    self.position_lot_repo.replace_scope_in_session(
                        owned_session,
                        symbol=symbol,
                        product_type=scoped_product_type,
                        margin_mode=scoped_margin_mode,
                        lots=lot_book.lots,
                    )
                    self.lot_event_repo.replace_scope_in_session(
                        owned_session,
                        symbol=symbol,
                        product_type=scoped_product_type,
                        margin_mode=scoped_margin_mode,
                        events=lot_book.events,
                    )
                    owned_session.commit()
                continue
            self.position_lot_repo.replace_scope(
                symbol=symbol,
                product_type=scoped_product_type,
                margin_mode=scoped_margin_mode,
                lots=lot_book.lots,
            )
            self.lot_event_repo.replace_scope(
                symbol=symbol,
                product_type=scoped_product_type,
                margin_mode=scoped_margin_mode,
                events=lot_book.events,
            )
