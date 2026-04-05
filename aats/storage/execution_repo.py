from __future__ import annotations

from datetime import datetime

from aats.schemas.execution import FillEvent, OrderState
from aats.bootstrap.logging import get_logger, log_event
from aats.services.fill_ordering import fill_processing_sort_key
from aats.services.execution_engine.state_machine import OrderStateMachine
from aats.services.runtime_scope import RuntimeStateScope, filter_fills, filter_order_states


class InMemoryExecutionRepository:
    _TERMINAL_STATUSES = frozenset({"FILLED", "CANCELED", "REJECTED", "FAILED", "BLOCKED", "DRY_RUN", "EXPIRED"})

    def __init__(self) -> None:
        self._order_states_by_client_order_id: dict[str, OrderState] = {}
        self._order_states_by_intent_id: dict[str, OrderState] = {}
        self._fills_by_fill_id: dict[str, FillEvent] = {}
        self._state_machine = OrderStateMachine()
        self._logger = get_logger("aats.execution_repo")
        # Secondary indexes for hot-path queries.
        self._bundle_index: dict[str, set[str]] = {}  # bundle_id → {client_order_id}
        self._non_terminal_ids: set[str] = set()       # client_order_ids in non-terminal state

    def save_order_state(self, state: OrderState) -> OrderState:
        current = self._order_states_by_client_order_id.get(state.client_order_id)
        if current is None:
            current = self._order_states_by_intent_id.get(state.intent_id)
        validation = self._state_machine.validate_transition(
            current_status=None if current is None else current.status,
            next_status=state.status,
        )
        if not validation.accepted:
            log_event(
                self._logger,
                "order_state_transition_rejected",
                level="warning",
                decision_id=state.decision_id,
                intent_id=state.intent_id,
                order_id=state.client_order_id,
                current_status=None if current is None else current.status,
                incoming_status=state.status,
                reason=validation.reason,
            )
            if validation.reason == "invalid_transition":
                raise ValueError(
                    f"invalid_order_state_transition current={None if current is None else current.status} next={state.status}"
                )
        merged = self._state_machine.merge(current=current, incoming=state)
        if current is not None and current.client_order_id != merged.client_order_id:
            self._order_states_by_client_order_id.pop(current.client_order_id, None)
        self._order_states_by_client_order_id[merged.client_order_id] = merged
        self._order_states_by_intent_id[merged.intent_id] = merged
        # Maintain secondary indexes.
        bundle_id = str(merged.strategy_bundle_id or "").strip()
        if bundle_id:
            self._bundle_index.setdefault(bundle_id, set()).add(merged.client_order_id)
        if merged.status.upper() in self._TERMINAL_STATUSES:
            self._non_terminal_ids.discard(merged.client_order_id)
        else:
            self._non_terminal_ids.add(merged.client_order_id)
        return merged

    def has_intent(self, intent_id: str) -> bool:
        return intent_id in self._order_states_by_intent_id

    def save_fill(self, fill: FillEvent) -> bool:
        if fill.fill_id in self._fills_by_fill_id:
            return False
        self._fills_by_fill_id[fill.fill_id] = fill
        return True

    def order_states(self) -> list[OrderState]:
        return list(self._order_states_by_client_order_id.values())

    def get_order_state(self, client_order_id: str) -> OrderState | None:
        return self._order_states_by_client_order_id.get(client_order_id)

    def order_states_by_bundle_id(self, bundle_id: str) -> list[OrderState]:
        """O(K) lookup via bundle secondary index (K = orders in the bundle)."""
        client_order_ids = self._bundle_index.get(bundle_id, set())
        return [
            state
            for cid in client_order_ids
            if (state := self._order_states_by_client_order_id.get(cid)) is not None
        ]

    def non_terminal_order_states(self) -> list[OrderState]:
        """O(K) lookup via non-terminal secondary index (K = non-terminal orders)."""
        return [
            state
            for cid in self._non_terminal_ids
            if (state := self._order_states_by_client_order_id.get(cid)) is not None
        ]

    def recent_order_states(
        self,
        *,
        limit: int = 20,
        statuses: tuple[str, ...] | None = None,
    ) -> list[OrderState]:
        rows = sorted(
            self.order_states(),
            key=lambda item: (item.last_update_ts or item.created_at, item.client_order_id),
            reverse=True,
        )
        if statuses is not None:
            allowed = {status.upper() for status in statuses}
            rows = [row for row in rows if row.status.upper() in allowed]
        return rows[:limit]

    def open_order_states(self) -> list[OrderState]:
        return [state for state in self.order_states() if self._state_machine.is_open(state.status)]

    def fills(self) -> list[FillEvent]:
        return list(self._fills_by_fill_id.values())

    def fills_for_order(self, client_order_id: str) -> list[FillEvent]:
        return sorted(
            [fill for fill in self._fills_by_fill_id.values() if fill.client_order_id == client_order_id],
            key=fill_processing_sort_key,
        )

    def fills_since(
        self,
        *,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[FillEvent]:
        rows = sorted(
            self._fills_by_fill_id.values(),
            key=fill_processing_sort_key,
        )
        if since is not None:
            rows = [fill for fill in rows if fill.ingestion_timestamp >= since]
        if limit is not None:
            rows = rows[-limit:]
        return rows

    def order_states_for_scope(
        self,
        *,
        scope: RuntimeStateScope,
        statuses: tuple[str, ...] | None = None,
        limit: int | None = None,
        open_only: bool = False,
    ) -> list[OrderState]:
        rows = self.open_order_states() if open_only else self.order_states()
        rows = filter_order_states(rows, scope)
        if statuses is not None:
            allowed = {status.upper() for status in statuses}
            rows = [row for row in rows if row.status.upper() in allowed]
        rows = sorted(
            rows,
            key=lambda item: (item.last_update_ts or item.created_at, item.client_order_id),
        )
        if limit is not None:
            rows = rows[-limit:]
        return rows

    def fills_for_scope(
        self,
        *,
        scope: RuntimeStateScope,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[FillEvent]:
        rows = filter_fills(self.fills_since(since=since), scope)
        if limit is not None:
            rows = rows[-limit:]
        return rows
