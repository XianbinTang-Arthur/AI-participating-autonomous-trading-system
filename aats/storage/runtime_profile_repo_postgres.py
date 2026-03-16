from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aats.schemas.runtime_profiles import RuntimeProfileActivationState, RuntimeProfileRevision
from aats.storage.sqlalchemy_models import RuntimeProfileActivationModel, RuntimeProfileRevisionModel


class PostgresRuntimeProfileRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def save_revision(self, revision: RuntimeProfileRevision) -> RuntimeProfileRevision:
        with self.session_factory() as session:
            row = session.scalar(
                select(RuntimeProfileRevisionModel).where(RuntimeProfileRevisionModel.revision_id == revision.revision_id)
            )
            if row is None:
                row = RuntimeProfileRevisionModel(
                    revision_id=revision.revision_id,
                    profile_label=revision.profile_label,
                    status=revision.status,
                    change_classification=revision.change_classification,
                    created_at=revision.created_at,
                    created_by=revision.created_by,
                    supersedes_revision_id=revision.supersedes_revision_id,
                    activation_note=revision.activation_note,
                    payload=revision.payload,
                    summary=revision.summary,
                )
                session.add(row)
            else:
                row.profile_label = revision.profile_label
                row.status = revision.status
                row.change_classification = revision.change_classification
                row.created_by = revision.created_by
                row.supersedes_revision_id = revision.supersedes_revision_id
                row.activation_note = revision.activation_note
                row.payload = revision.payload
                row.summary = revision.summary
            session.commit()
        return revision

    def get_revision(self, revision_id: str) -> RuntimeProfileRevision | None:
        with self.session_factory() as session:
            row = session.scalar(
                select(RuntimeProfileRevisionModel).where(RuntimeProfileRevisionModel.revision_id == revision_id)
            )
        if row is None:
            return None
        return RuntimeProfileRevision(
            revision_id=row.revision_id,
            profile_label=row.profile_label,
            status=row.status,
            change_classification=row.change_classification,
            created_at=row.created_at,
            created_by=row.created_by,
            supersedes_revision_id=row.supersedes_revision_id,
            activation_note=row.activation_note,
            payload=row.payload,
            summary=row.summary,
        )

    def list_revisions(self) -> list[RuntimeProfileRevision]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(RuntimeProfileRevisionModel).order_by(RuntimeProfileRevisionModel.created_at.desc())
            ).all()
        return [
            RuntimeProfileRevision(
                revision_id=row.revision_id,
                profile_label=row.profile_label,
                status=row.status,
                change_classification=row.change_classification,
                created_at=row.created_at,
                created_by=row.created_by,
                supersedes_revision_id=row.supersedes_revision_id,
                activation_note=row.activation_note,
                payload=row.payload,
                summary=row.summary,
            )
            for row in rows
        ]

    def activation_state(self) -> RuntimeProfileActivationState:
        with self.session_factory() as session:
            row = session.scalar(
                select(RuntimeProfileActivationModel).where(RuntimeProfileActivationModel.activation_id == "runtime_profile_activation")
            )
            if row is None:
                row = RuntimeProfileActivationModel(
                    activation_id="runtime_profile_activation",
                    payload=RuntimeProfileActivationState().model_dump(mode="json"),
                )
                session.add(row)
                session.commit()
                session.refresh(row)
        return RuntimeProfileActivationState.model_validate(row.payload)

    def save_activation_state(self, state: RuntimeProfileActivationState) -> RuntimeProfileActivationState:
        payload = state.model_dump(mode="json")
        with self.session_factory() as session:
            row = session.scalar(
                select(RuntimeProfileActivationModel).where(RuntimeProfileActivationModel.activation_id == state.activation_id)
            )
            if row is None:
                row = RuntimeProfileActivationModel(activation_id=state.activation_id, payload=payload)
                session.add(row)
            else:
                row.payload = payload
            session.commit()
        return state
