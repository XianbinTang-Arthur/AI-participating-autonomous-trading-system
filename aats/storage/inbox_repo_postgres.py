from __future__ import annotations

from datetime import datetime

from sqlalchemy import asc, select
from sqlalchemy.orm import Session, sessionmaker

from aats.schemas.common import dump_payload_exact
from aats.storage.sqlalchemy_models import ExternalEventInboxModel


class PostgresExternalInboxRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def save_incoming(
        self,
        *,
        inbox_id: str,
        source_system: str,
        dedupe_key: str,
        payload: dict,
        received_at: datetime,
    ) -> bool:
        with self.session_factory() as session:
            row = session.get(ExternalEventInboxModel, inbox_id)
            if row is None:
                row = session.scalar(
                    select(ExternalEventInboxModel).where(ExternalEventInboxModel.dedupe_key == dedupe_key).limit(1)
                )
            if row is not None:
                return False
            session.add(
                ExternalEventInboxModel(
                    inbox_id=inbox_id,
                    source_system=source_system,
                    dedupe_key=dedupe_key,
                    payload=dump_payload_exact(payload),
                    received_at=received_at,
                    processed_at=None,
                    processing_result=None,
                    last_error=None,
                )
            )
            session.commit()
            return True

    def mark_processed(
        self,
        *,
        inbox_id: str,
        processing_result: str,
        processed_at: datetime,
        last_error: str | None = None,
    ) -> None:
        with self.session_factory() as session:
            row = session.get(ExternalEventInboxModel, inbox_id)
            if row is None:
                return
            row.processing_result = processing_result
            row.processed_at = processed_at
            row.last_error = None if last_error is None else last_error[:1024]
            session.commit()

    def unprocessed(self, *, limit: int) -> list[dict]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(ExternalEventInboxModel)
                .where(ExternalEventInboxModel.processed_at.is_(None))
                .order_by(asc(ExternalEventInboxModel.received_at), asc(ExternalEventInboxModel.inbox_id))
                .limit(limit)
            ).all()
        return [_inbox_row_to_dict(row) for row in rows]


def _inbox_row_to_dict(row: ExternalEventInboxModel) -> dict:
    return {
        "inbox_id": row.inbox_id,
        "source_system": row.source_system,
        "dedupe_key": row.dedupe_key,
        "payload": dict(row.payload),
        "received_at": row.received_at,
        "processed_at": row.processed_at,
        "processing_result": row.processing_result,
        "last_error": row.last_error,
    }
