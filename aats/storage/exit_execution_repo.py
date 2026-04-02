from __future__ import annotations

from aats.schemas.exit_execution import ChildExitOrderRef, ExitExecutionIntent


class InMemoryExitExecutionRepository:
    def __init__(self) -> None:
        self._parents_by_id: dict[str, ExitExecutionIntent] = {}
        self._parent_ids_by_chain: dict[str, str] = {}
        self._child_refs_by_parent: dict[str, dict[str, ChildExitOrderRef]] = {}
        self._parent_ids_by_child: dict[str, str] = {}

    def save_exit_execution_intent(self, intent: ExitExecutionIntent) -> ExitExecutionIntent:
        self._parents_by_id[intent.parent_intent_id] = intent
        self._parent_ids_by_chain[intent.execution_chain_id] = intent.parent_intent_id
        self._child_refs_by_parent.setdefault(intent.parent_intent_id, {})
        return intent

    def get_exit_execution_intent(self, parent_intent_id: str) -> ExitExecutionIntent | None:
        return self._parents_by_id.get(parent_intent_id)

    def get_exit_execution_intent_by_execution_chain(
        self,
        execution_chain_id: str,
    ) -> ExitExecutionIntent | None:
        parent_intent_id = self._parent_ids_by_chain.get(execution_chain_id)
        return None if parent_intent_id is None else self._parents_by_id.get(parent_intent_id)

    def list_exit_execution_intents(self) -> list[ExitExecutionIntent]:
        return list(self._parents_by_id.values())

    def save_child_exit_order_ref(self, child_ref: ChildExitOrderRef) -> ChildExitOrderRef:
        parent_refs = self._child_refs_by_parent.setdefault(child_ref.parent_intent_id, {})
        parent_refs[child_ref.client_order_id] = child_ref
        self._parent_ids_by_child[child_ref.client_order_id] = child_ref.parent_intent_id
        return child_ref

    def child_refs_for_parent(self, *, parent_intent_id: str) -> list[ChildExitOrderRef]:
        return list(self._child_refs_by_parent.get(parent_intent_id, {}).values())

    def parent_intent_id_for_child(self, *, client_order_id: str) -> str | None:
        return self._parent_ids_by_child.get(client_order_id)
