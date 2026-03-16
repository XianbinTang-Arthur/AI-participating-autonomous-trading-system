from __future__ import annotations

from aats.schemas.portfolio import PortfolioSnapshot


class InMemoryPortfolioRepository:
    def __init__(self) -> None:
        self._snapshots: list[PortfolioSnapshot] = []

    def save_snapshot(self, snapshot: PortfolioSnapshot) -> None:
        self._snapshots.append(snapshot)

    def latest(self) -> PortfolioSnapshot | None:
        return self._snapshots[-1] if self._snapshots else None

    def history(self) -> list[PortfolioSnapshot]:
        return list(self._snapshots)

    def recent_history(self, *, limit: int) -> list[PortfolioSnapshot]:
        if limit <= 0:
            return []
        return list(self._snapshots[-limit:])
