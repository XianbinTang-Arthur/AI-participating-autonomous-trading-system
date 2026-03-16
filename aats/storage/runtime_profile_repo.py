from __future__ import annotations

from aats.schemas.runtime_profiles import RuntimeProfileActivationState, RuntimeProfileRevision


class InMemoryRuntimeProfileRepository:
    def __init__(self) -> None:
        self._revisions: dict[str, RuntimeProfileRevision] = {}
        self._activation = RuntimeProfileActivationState()

    def save_revision(self, revision: RuntimeProfileRevision) -> RuntimeProfileRevision:
        self._revisions[revision.revision_id] = revision
        return revision

    def get_revision(self, revision_id: str) -> RuntimeProfileRevision | None:
        return self._revisions.get(revision_id)

    def list_revisions(self) -> list[RuntimeProfileRevision]:
        return sorted(self._revisions.values(), key=lambda item: item.created_at, reverse=True)

    def activation_state(self) -> RuntimeProfileActivationState:
        return self._activation

    def save_activation_state(self, state: RuntimeProfileActivationState) -> RuntimeProfileActivationState:
        self._activation = state
        return state
