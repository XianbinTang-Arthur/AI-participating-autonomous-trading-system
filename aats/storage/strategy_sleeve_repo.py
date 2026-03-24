from __future__ import annotations

from aats.schemas.strategy_runtime import StrategySleeveRecord


class InMemoryStrategySleeveRepository:
    def __init__(self) -> None:
        self._records: dict[str, StrategySleeveRecord] = {}

    def save_sleeve(self, sleeve: StrategySleeveRecord) -> StrategySleeveRecord:
        current = self._records.get(sleeve.sleeve_id)
        merged = sleeve if current is None else current.model_copy(update=sleeve.model_dump(mode="python"))
        self._records[merged.sleeve_id] = merged
        return merged

    def get_sleeve(self, sleeve_id: str) -> StrategySleeveRecord | None:
        return self._records.get(sleeve_id)

    def list_sleeves(self) -> list[StrategySleeveRecord]:
        return sorted(self._records.values(), key=lambda item: (item.family, item.name, item.sleeve_id))
