from __future__ import annotations

from decimal import Decimal

from aats.services.ledger.lot_projection import LotBasedProjectionBuilder
from aats.services.portfolio_service.decimals import to_decimal
from aats.services.strategy_engines.sleeve_identity import build_strategy_sleeve_id, normalized_symbol_scope


class StrategySleeveInventoryService:
    def __init__(
        self,
        *,
        execution_repo=None,
        position_lot_repo=None,
        projection_builder: LotBasedProjectionBuilder | None = None,
    ) -> None:
        self.execution_repo = execution_repo
        self.position_lot_repo = position_lot_repo
        self.projection_builder = projection_builder or LotBasedProjectionBuilder()
        self._open_lot_cache: dict[tuple[str, str, str], list[dict]] = {}
        self._quantity_cache: dict[tuple[str, str, str, str], Decimal] = {}

    def reset(self) -> None:
        self._open_lot_cache.clear()
        self._quantity_cache.clear()

    def sleeve_id_for(
        self,
        *,
        family: str,
        primary_symbol: str,
        product_scope: str,
        margin_scope: str,
        symbol_scope: tuple[str, ...],
    ) -> str:
        return build_strategy_sleeve_id(
            family=family,
            primary_symbol=primary_symbol,
            product_scope=product_scope,
            margin_scope=margin_scope,
            symbol_scope=normalized_symbol_scope(*symbol_scope),
        )

    def quantity_for_strategy(
        self,
        *,
        family: str,
        primary_symbol: str,
        product_scope: str,
        margin_scope: str,
        symbol_scope: tuple[str, ...],
        symbol: str,
        product_type: str,
        margin_mode: str,
    ) -> Decimal:
        sleeve_id = self.sleeve_id_for(
            family=family,
            primary_symbol=primary_symbol,
            product_scope=product_scope,
            margin_scope=margin_scope,
            symbol_scope=symbol_scope,
        )
        return self.quantity_for_sleeve(
            sleeve_id=sleeve_id,
            symbol=symbol,
            product_type=product_type,
            margin_mode=margin_mode,
        )

    def quantity_for_sleeve(
        self,
        *,
        sleeve_id: str,
        symbol: str,
        product_type: str,
        margin_mode: str,
    ) -> Decimal:
        normalized_symbol = str(symbol).upper()
        cache_key = (sleeve_id, normalized_symbol, str(product_type), str(margin_mode))
        cached = self._quantity_cache.get(cache_key)
        if cached is not None:
            return cached

        quantity = self._quantity_from_lots(
            sleeve_id=sleeve_id,
            symbol=normalized_symbol,
            product_type=product_type,
            margin_mode=margin_mode,
        )
        if quantity is None:
            quantity = self._quantity_from_fills(
                sleeve_id=sleeve_id,
                symbol=normalized_symbol,
                product_type=product_type,
                margin_mode=margin_mode,
            )
        self._quantity_cache[cache_key] = quantity
        return quantity

    def _quantity_from_lots(
        self,
        *,
        sleeve_id: str,
        symbol: str,
        product_type: str,
        margin_mode: str,
    ) -> Decimal | None:
        if self.position_lot_repo is None:
            return None
        scope_key = (symbol, str(product_type), str(margin_mode))
        rows = self._open_lot_cache.get(scope_key)
        if rows is None:
            rows = self.position_lot_repo.lots_for_scope(
                symbol=symbol,
                product_type=product_type,
                margin_mode=margin_mode,
                open_only=True,
            )
            self._open_lot_cache[scope_key] = rows
        return sum(
            (
                to_decimal(row.get("signed_quantity_open"))
                for row in rows
                if str(row.get("strategy_sleeve_id") or "") == sleeve_id
            ),
            start=Decimal("0"),
        )

    def _quantity_from_fills(
        self,
        *,
        sleeve_id: str,
        symbol: str,
        product_type: str,
        margin_mode: str,
    ) -> Decimal:
        if self.execution_repo is None:
            return Decimal("0")
        fills = [
            fill
            for fill in self.execution_repo.fills()
            if str(getattr(fill, "strategy_sleeve_id", "") or "") == sleeve_id
            and str(fill.symbol).upper() == symbol
            and str(fill.product_type) == str(product_type)
            and str(fill.margin_mode) == str(margin_mode)
        ]
        if not fills:
            return Decimal("0")
        snapshot = self.projection_builder.rebuild_lot_book(fills=fills)
        return sum(
            (
                to_decimal(position.quantity)
                for position in snapshot.positions.values()
                if str(position.symbol).upper() == symbol
            ),
            start=Decimal("0"),
        )
