from __future__ import annotations

from dataclasses import dataclass, field

from aats.events import topics
from aats.schemas.audit import DecisionAuditRecord
from aats.schemas.execution import FillEvent
from aats.schemas.market import MarketSnapshot
from aats.schemas.portfolio import PortfolioSnapshot
from aats.storage.base import EventStore
from aats.services.portfolio_service.reconstruction import PortfolioReconstructionService


@dataclass(slots=True)
class ReplayResult:
    replayed_event_count: int
    stored_snapshot_count: int
    divergence_count: int
    divergences: list[str] = field(default_factory=list)
    final_reconstructed_snapshot: PortfolioSnapshot | None = None
    final_stored_snapshot: PortfolioSnapshot | None = None
    decision_chains: dict[str, DecisionAuditRecord] = field(default_factory=dict)


class ReplayEngine:
    def __init__(
        self,
        *,
        event_store: EventStore,
        reconstruction_service: PortfolioReconstructionService,
    ) -> None:
        self.event_store = event_store
        self.reconstruction_service = reconstruction_service

    def replay(self) -> ReplayResult:
        latest_prices: dict[str, float] = {}
        processed_fills: list[FillEvent] = []
        decision_chains: dict[str, DecisionAuditRecord] = {}
        divergences: list[str] = []
        stored_snapshot_count = 0
        final_stored_snapshot: PortfolioSnapshot | None = None

        events = self.event_store.all()
        for envelope in events:
            if envelope.topic == topics.MARKET_SNAPSHOTS:
                snapshot = MarketSnapshot.model_validate(envelope.payload)
                latest_prices[snapshot.symbol] = snapshot.last_price
                continue

            if envelope.topic == topics.FILL_EVENTS:
                processed_fills.append(FillEvent.model_validate(envelope.payload))
                continue

            if envelope.topic == topics.AUDIT_RECORDS:
                record = DecisionAuditRecord.model_validate(envelope.payload)
                decision_chains[record.decision_id] = record
                continue

            if envelope.topic == topics.PORTFOLIO_SNAPSHOTS:
                stored_snapshot_count += 1
                stored_snapshot = PortfolioSnapshot.model_validate(envelope.payload)
                final_stored_snapshot = stored_snapshot
                reconstructed_snapshot = self.reconstruction_service.rebuild_snapshot(
                    fills=processed_fills,
                    price_provider=lambda symbol: latest_prices.get(symbol, 0.0),
                )
                mismatch = self._snapshot_mismatch(
                    stored_snapshot=stored_snapshot,
                    reconstructed_snapshot=reconstructed_snapshot,
                )
                if mismatch:
                    divergences.append(
                        f"portfolio_snapshot_event={envelope.event_id} mismatch={mismatch}"
                    )

        final_reconstructed_snapshot = None
        if stored_snapshot_count > 0 or processed_fills:
            final_reconstructed_snapshot = self.reconstruction_service.rebuild_snapshot(
                fills=processed_fills,
                price_provider=lambda symbol: latest_prices.get(symbol, 0.0),
            )

        return ReplayResult(
            replayed_event_count=len(events),
            stored_snapshot_count=stored_snapshot_count,
            divergence_count=len(divergences),
            divergences=divergences,
            final_reconstructed_snapshot=final_reconstructed_snapshot,
            final_stored_snapshot=final_stored_snapshot,
            decision_chains=decision_chains,
        )

    @staticmethod
    def _snapshot_mismatch(
        *,
        stored_snapshot: PortfolioSnapshot,
        reconstructed_snapshot: PortfolioSnapshot,
    ) -> dict[str, object]:
        mismatch: dict[str, object] = {}
        if stored_snapshot.balances != reconstructed_snapshot.balances:
            mismatch["balances"] = {
                "stored": stored_snapshot.balances,
                "reconstructed": reconstructed_snapshot.balances,
            }
        if stored_snapshot.cost_basis != reconstructed_snapshot.cost_basis:
            mismatch["cost_basis"] = {
                "stored": stored_snapshot.cost_basis,
                "reconstructed": reconstructed_snapshot.cost_basis,
            }

        stored_positions = {position.symbol: position.position_qty for position in stored_snapshot.positions}
        replayed_positions = {
            position.symbol: position.position_qty for position in reconstructed_snapshot.positions
        }
        if stored_positions != replayed_positions:
            mismatch["positions"] = {
                "stored": stored_positions,
                "reconstructed": replayed_positions,
            }

        numeric_fields = (
            "realized_pnl",
            "unrealized_pnl",
            "total_equity",
            "gross_exposure",
            "net_exposure",
        )
        for field_name in numeric_fields:
            if abs(getattr(stored_snapshot, field_name) - getattr(reconstructed_snapshot, field_name)) > 1e-9:
                mismatch[field_name] = {
                    "stored": getattr(stored_snapshot, field_name),
                    "reconstructed": getattr(reconstructed_snapshot, field_name),
                }

        return mismatch
