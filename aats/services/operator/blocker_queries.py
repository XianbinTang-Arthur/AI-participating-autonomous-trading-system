from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aats.events import topics

if TYPE_CHECKING:
    from aats.services.operator.query_service import OperatorQueryService


class BlockerQueryFacade:
    def __init__(self, owner: "OperatorQueryService") -> None:
        self.owner = owner

    def blockers(self) -> list[dict[str, Any]]:
        return self.owner._build_blockers()

    def blocker_control(self) -> dict[str, Any]:
        return self.owner._build_blocker_control().model_dump(mode="json")

    def blocker_history(self, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        rows = [item.payload for item in reversed(self.owner.runtime.event_store.by_topic(topics.BLOCKER_SNAPSHOTS))]
        return self.owner._paginate_rows(rows, limit=limit, offset=offset, key="history")
