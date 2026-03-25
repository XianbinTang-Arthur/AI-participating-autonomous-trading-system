from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
import hashlib

from aats.schemas.portfolio import FillOutcomeRecord, FundingFeeRecord, SleevePnLRecord
from aats.services.ledger.lot_projection import LotBasedProjectionBuilder
from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12, quantize_decimal, to_decimal
from aats.services.runtime_scope import RuntimeStateScope, fill_outcomes_for_scope, funding_fee_records_for_scope


@dataclass(slots=True)
class SleevePnLProjectionService:
    fill_outcome_repo: object
    sleeve_pnl_repo: object
    execution_repo: object | None = None
    funding_fee_repo: object | None = None
    strategy_sleeve_repo: object | None = None
    projection_builder: LotBasedProjectionBuilder = field(default_factory=LotBasedProjectionBuilder)

    def rebuild_scope(self, *, scope: RuntimeStateScope) -> list[SleevePnLRecord]:
        family_by_sleeve = self._family_by_sleeve()
        outcomes = fill_outcomes_for_scope(self.fill_outcome_repo, scope)
        records = [
            self._record_from_fill_outcome(outcome=outcome, family_by_sleeve=family_by_sleeve)
            for outcome in outcomes
        ]
        if self.funding_fee_repo is not None:
            funding_records = funding_fee_records_for_scope(self.funding_fee_repo, scope)
            for record in funding_records:
                records.extend(
                    self._records_from_funding_fee(
                        record=record,
                        family_by_sleeve=family_by_sleeve,
                    )
                )
        records.sort(key=lambda item: (item.event_timestamp or item.created_at, item.record_id))
        self.sleeve_pnl_repo.replace_scope(scope=scope, records=records)
        return records

    def save_fill_outcome_in_session(self, session, *, outcome: FillOutcomeRecord) -> SleevePnLRecord:
        family_by_sleeve = self._family_by_sleeve()
        record = self._record_from_fill_outcome(outcome=outcome, family_by_sleeve=family_by_sleeve)
        save_record = getattr(self.sleeve_pnl_repo, "save_record_in_session", None)
        if callable(save_record):
            save_record(session, record)
        else:
            self.sleeve_pnl_repo.save_record(record)
        return record

    def _family_by_sleeve(self) -> dict[str, str]:
        repo = self.strategy_sleeve_repo
        if repo is None or not hasattr(repo, "list_sleeves"):
            return {}
        return {
            sleeve.sleeve_id: sleeve.family
            for sleeve in repo.list_sleeves()
        }

    def _record_from_fill_outcome(
        self,
        *,
        outcome: FillOutcomeRecord,
        family_by_sleeve: dict[str, str],
    ) -> SleevePnLRecord:
        inventory_move_qty = Decimal("0")
        fill_qty = to_decimal(outcome.fill_qty)
        if fill_qty > EPSILON_DECIMAL_12:
            if str(outcome.side or "").lower() == "buy":
                inventory_move_qty = fill_qty
            elif str(outcome.side or "").lower() == "sell":
                inventory_move_qty = -fill_qty
        return SleevePnLRecord(
            record_id=self._stable_id("fill", outcome.fill_id),
            strategy_sleeve_id=outcome.strategy_sleeve_id,
            strategy_family=outcome.strategy_family or family_by_sleeve.get(str(outcome.strategy_sleeve_id or "")),
            allocation_id=outcome.allocation_id,
            strategy_bundle_id=outcome.strategy_bundle_id,
            strategy_leg_role=outcome.strategy_leg_role,
            symbol=outcome.symbol,
            event_type="fill_realization",
            fill_id=outcome.fill_id,
            funding_fee_id=None,
            fee_currency=outcome.fee_currency,
            realized_pnl=to_decimal(outcome.realized_pnl_delta),
            fee_amount=to_decimal(outcome.fee_delta),
            funding_fee_amount=Decimal("0"),
            inventory_move_qty=inventory_move_qty,
            attribution_type="direct_fill",
            product_type=outcome.product_type,
            margin_mode=outcome.margin_mode,
            event_timestamp=outcome.ingestion_timestamp or outcome.exchange_timestamp or outcome.created_at,
            created_at=outcome.created_at,
            raw_payload={
                "truth_source": "fill_outcomes",
                "decision_id": outcome.decision_id,
                "intent_id": outcome.intent_id,
                "order_id": outcome.order_id,
                "position_key": outcome.position_key,
                "execution_action": outcome.execution_action,
                "position_intent": outcome.position_intent,
            },
        )

    def _records_from_funding_fee(
        self,
        *,
        record: FundingFeeRecord,
        family_by_sleeve: dict[str, str],
    ) -> list[SleevePnLRecord]:
        event_ts = record.bill_ts or record.created_at
        if not record.symbol or self.execution_repo is None:
            return [self._unassigned_funding_fee_record(record=record, attribution_type="account_level_unassigned")]

        active_lots = self._active_lots_at(
            symbol=record.symbol,
            product_type=record.product_type,
            margin_mode=record.margin_mode,
            as_of=event_ts,
        )
        sleeve_weights: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        sleeve_allocations: dict[str, set[str]] = defaultdict(set)
        for lot in active_lots:
            sleeve_id = str(lot.get("strategy_sleeve_id") or "").strip()
            if not sleeve_id:
                continue
            qty = abs(to_decimal(lot.get("signed_quantity_open")))
            price = abs(to_decimal(lot.get("entry_price")))
            weight = qty * price
            if weight <= EPSILON_DECIMAL_12:
                weight = qty
            if weight <= EPSILON_DECIMAL_12:
                continue
            sleeve_weights[sleeve_id] += weight
            allocation_id = str(lot.get("allocation_id") or "").strip()
            if allocation_id:
                sleeve_allocations[sleeve_id].add(allocation_id)

        if not sleeve_weights:
            return [self._unassigned_funding_fee_record(record=record, attribution_type="no_matching_inventory_window")]

        total_weight = sum(sleeve_weights.values(), start=Decimal("0"))
        if total_weight <= EPSILON_DECIMAL_12:
            return [self._unassigned_funding_fee_record(record=record, attribution_type="zero_weight_inventory")]

        amount = to_decimal(record.amount)
        sorted_items = sorted(sleeve_weights.items(), key=lambda item: item[0])
        remainder = amount
        records: list[SleevePnLRecord] = []
        for index, (sleeve_id, weight) in enumerate(sorted_items):
            share_amount = remainder if index == len(sorted_items) - 1 else quantize_decimal(amount * weight / total_weight)
            remainder -= share_amount
            allocation_ids = sleeve_allocations.get(sleeve_id, set())
            records.append(
                SleevePnLRecord(
                    record_id=self._stable_id("funding", record.bill_id, sleeve_id),
                    strategy_sleeve_id=sleeve_id,
                    strategy_family=family_by_sleeve.get(sleeve_id),
                    allocation_id=next(iter(allocation_ids)) if len(allocation_ids) == 1 else None,
                    strategy_bundle_id=None,
                    strategy_leg_role="hedge",
                    symbol=record.symbol,
                    event_type="funding_fee",
                    fill_id=None,
                    funding_fee_id=record.bill_id,
                    fee_currency=record.currency,
                    realized_pnl=Decimal("0"),
                    fee_amount=Decimal("0"),
                    funding_fee_amount=share_amount,
                    inventory_move_qty=Decimal("0"),
                    attribution_type="symbol_inventory_share",
                    product_type=record.product_type,
                    margin_mode=record.margin_mode,
                    event_timestamp=event_ts,
                    created_at=record.created_at,
                    raw_payload={
                        "truth_source": "funding_fee_records",
                        "source_bill_id": record.bill_id,
                        "weight": format(weight, "f"),
                        "total_weight": format(total_weight, "f"),
                        "funding_direction": record.funding_direction,
                    },
                )
            )
        return records

    def _unassigned_funding_fee_record(
        self,
        *,
        record: FundingFeeRecord,
        attribution_type: str,
    ) -> SleevePnLRecord:
        return SleevePnLRecord(
            record_id=self._stable_id("funding", record.bill_id, attribution_type),
            strategy_sleeve_id=None,
            strategy_family=None,
            allocation_id=None,
            strategy_bundle_id=None,
            strategy_leg_role=None,
            symbol=record.symbol,
            event_type="funding_fee",
            fill_id=None,
            funding_fee_id=record.bill_id,
            fee_currency=record.currency,
            realized_pnl=Decimal("0"),
            fee_amount=Decimal("0"),
            funding_fee_amount=to_decimal(record.amount),
            inventory_move_qty=Decimal("0"),
            attribution_type=attribution_type,
            product_type=record.product_type,
            margin_mode=record.margin_mode,
            event_timestamp=record.bill_ts or record.created_at,
            created_at=record.created_at,
            raw_payload={
                "truth_source": "funding_fee_records",
                "source_bill_id": record.bill_id,
                "funding_direction": record.funding_direction,
            },
        )

    def _active_lots_at(
        self,
        *,
        symbol: str,
        product_type: str,
        margin_mode: str,
        as_of: datetime,
    ) -> list[dict]:
        fills = self._fills_for_symbol(
            symbol=symbol,
            product_type=product_type,
            margin_mode=margin_mode,
            as_of=as_of,
        )
        if not fills:
            return []
        lot_book = self.projection_builder.rebuild_lot_book(fills=fills)
        return [
            dict(lot)
            for lot in lot_book.lots
            if str(lot.get("status") or "") == "OPEN"
        ]

    def _fills_for_symbol(
        self,
        *,
        symbol: str,
        product_type: str,
        margin_mode: str,
        as_of: datetime,
    ) -> list:
        if self.execution_repo is None:
            return []
        scope = RuntimeStateScope(
            product_type=product_type,
            margin_mode=margin_mode,
            allowed_symbols=(symbol,),
            default_symbol=symbol,
        )
        rows = self.execution_repo.fills_for_scope(scope=scope)
        return [
            fill
            for fill in rows
            if (
                fill.ingestion_timestamp
                or fill.exchange_timestamp
                or getattr(fill, "created_at", None)
                or as_of
            ) <= as_of
        ]

    @staticmethod
    def _stable_id(prefix: str, *parts: object) -> str:
        raw = "|".join(str(part) for part in parts).encode("utf-8")
        return f"sleevepnl_{prefix}_{hashlib.sha1(raw).hexdigest()[:32]}"
