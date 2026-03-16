from __future__ import annotations

from aats.schemas.portfolio import PortfolioSnapshot
from aats.services.runtime_scope import RuntimeStateScope, filter_snapshots, latest_matching_snapshot


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

    def history_for_scope(
        self,
        *,
        scope: RuntimeStateScope,
        limit: int | None = None,
    ) -> list[PortfolioSnapshot]:
        rows = filter_snapshots(self._snapshots, scope)
        if limit is not None:
            rows = rows[-limit:]
        return list(rows)

    def latest_for_scope(self, *, scope: RuntimeStateScope) -> PortfolioSnapshot | None:
        return latest_matching_snapshot(self._snapshots, scope)
