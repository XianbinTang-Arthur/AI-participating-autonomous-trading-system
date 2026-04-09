from __future__ import annotations

import hashlib
import threading
from datetime import datetime

from sqlalchemy import Select, and_, delete, desc, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from aats.events import topics as _topics
from aats.schemas.common import EventEnvelope
from aats.schemas.reconciliation import ReplayProjectionOffset
from aats.services.runtime_scope import RuntimeStateScope
from aats.storage.scope_metadata import envelope_scope_metadata
from aats.storage.sqlalchemy_models import EventEnvelopeArchiveModel, EventEnvelopeModel, ReplayProjectionOffsetModel

# 高频 topic 自动轮转配置：每个 topic 最多保留 _ROTATE_KEEP 行，
# 每 _ROTATE_INTERVAL 次写入触发一次清理检查。
_HIGH_FREQ_TOPICS: frozenset[str] = frozenset({
    _topics.MARKET_SNAPSHOTS,
    _topics.FEATURE_SNAPSHOTS,
})
_ROTATE_KEEP = 200          # 每个高频 topic 保留最新 200 条
_ROTATE_INTERVAL = 500      # 每写入 500 条后检查一次
_ROTATE_THRESHOLD = 1000    # 超过此数量才触发删除


class PostgresEventStore:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory
        self._high_freq_write_count = 0
        self._rotate_lock = threading.Lock()

    def append(self, envelope: EventEnvelope) -> None:
        with self.session_factory() as session:
            self.append_in_session(session, envelope)
            session.commit()
        # 高频 topic 自动轮转
        if envelope.topic in _HIGH_FREQ_TOPICS:
            self._maybe_rotate_high_freq()

    def _maybe_rotate_high_freq(self) -> None:
        """每 _ROTATE_INTERVAL 次高频写入后，清理超出 _ROTATE_KEEP 的旧行。

        仅清理 hot 表（EventEnvelopeModel）中的高频 topic 行，不涉及 archive。
        失败不影响正常写入——轮转是 best-effort 优化。
        """
        with self._rotate_lock:
            self._high_freq_write_count += 1
            if self._high_freq_write_count % _ROTATE_INTERVAL != 0:
                return

        # 到达检查点，逐 topic 清理（锁外执行 DB 操作，避免长时间持锁）
        for topic in _HIGH_FREQ_TOPICS:
            try:
                with self.session_factory() as session:
                    row_count = session.scalar(
                        select(func.count())
                        .select_from(EventEnvelopeModel)
                        .where(EventEnvelopeModel.topic == topic)
                    ) or 0
                    if row_count <= _ROTATE_THRESHOLD:
                        continue
                    # 找到第 _ROTATE_KEEP 新的 sequence_id 作为截断点
                    cutoff_id = session.scalar(
                        select(EventEnvelopeModel.sequence_id)
                        .where(EventEnvelopeModel.topic == topic)
                        .order_by(desc(EventEnvelopeModel.sequence_id))
                        .offset(_ROTATE_KEEP)
                        .limit(1)
                    )
                    if cutoff_id is None:
                        continue
                    session.execute(
                        delete(EventEnvelopeModel).where(
                            EventEnvelopeModel.topic == topic,
                            EventEnvelopeModel.sequence_id <= cutoff_id,
                        )
                    )
                    session.commit()
            except Exception:
                # 轮转失败不影响正常写入流程
                pass

    def append_in_session(self, session: Session, envelope: EventEnvelope) -> None:
        existing = session.scalar(
            select(EventEnvelopeModel.sequence_id).where(EventEnvelopeModel.event_id == envelope.event_id)
        )
        if existing is not None:
            return

        session.add(
            EventEnvelopeModel(
                event_id=envelope.event_id,
                schema_version=envelope.schema_version,
                created_at=envelope.created_at,
                event_type=envelope.event_type,
                event_timestamp=envelope.event_timestamp,
                source_component=envelope.source_component,
                topic=envelope.topic,
                event_key=envelope.key,
                decision_id=self._decision_id(envelope),
                symbol=envelope_scope_metadata(envelope)["symbol"],
                timeframe=envelope_scope_metadata(envelope)["timeframe"],
                product_type=envelope_scope_metadata(envelope)["product_type"],
                margin_mode=envelope_scope_metadata(envelope)["margin_mode"],
                payload=envelope.payload,
            )
        )

    def all(self) -> list[EventEnvelope]:
        with self.session_factory() as session:
            hot_rows = session.scalars(select(EventEnvelopeModel).order_by(EventEnvelopeModel.sequence_id)).all()
            archive_rows = session.scalars(
                select(EventEnvelopeArchiveModel).order_by(EventEnvelopeArchiveModel.source_sequence_id)
            ).all()
        return [self._to_schema(row) for row in [*archive_rows, *hot_rows]]

    def count(self, *, topic: str | None = None, decision_id: str | None = None) -> int:
        query = select(func.count()).select_from(EventEnvelopeModel)
        archive_query = select(func.count()).select_from(EventEnvelopeArchiveModel)
        if topic is not None:
            query = query.where(EventEnvelopeModel.topic == topic)
            archive_query = archive_query.where(EventEnvelopeArchiveModel.topic == topic)
        if decision_id is not None:
            query = query.where(EventEnvelopeModel.decision_id == decision_id)
            archive_query = archive_query.where(EventEnvelopeArchiveModel.decision_id == decision_id)
        with self.session_factory() as session:
            count = session.scalar(query)
            archive_count = session.scalar(archive_query)
        return int((count or 0) + (archive_count or 0))

    def get(self, event_id: str) -> EventEnvelope | None:
        with self.session_factory() as session:
            row = session.scalar(select(EventEnvelopeModel).where(EventEnvelopeModel.event_id == event_id))
            if row is None:
                row = session.scalar(select(EventEnvelopeArchiveModel).where(EventEnvelopeArchiveModel.event_id == event_id))
        return self._to_schema(row) if row is not None else None

    def latest(self, topic: str, key: str | None = None) -> EventEnvelope | None:
        query: Select[tuple[EventEnvelopeModel]] = select(EventEnvelopeModel).where(EventEnvelopeModel.topic == topic)
        if key is not None:
            query = query.where(EventEnvelopeModel.event_key == key)
        query = query.order_by(desc(EventEnvelopeModel.sequence_id)).limit(1)
        with self.session_factory() as session:
            row = session.scalar(query)
            if row is None:
                archive_query: Select[tuple[EventEnvelopeArchiveModel]] = (
                    select(EventEnvelopeArchiveModel).where(EventEnvelopeArchiveModel.topic == topic)
                )
                if key is not None:
                    archive_query = archive_query.where(EventEnvelopeArchiveModel.event_key == key)
                archive_query = archive_query.order_by(desc(EventEnvelopeArchiveModel.source_sequence_id)).limit(1)
                row = session.scalar(archive_query)
        return self._to_schema(row) if row is not None else None

    def by_topic(self, topic: str) -> list[EventEnvelope]:
        with self.session_factory() as session:
            hot_rows = session.scalars(
                select(EventEnvelopeModel)
                .where(EventEnvelopeModel.topic == topic)
                .order_by(EventEnvelopeModel.sequence_id)
            ).all()
            archive_rows = session.scalars(
                select(EventEnvelopeArchiveModel)
                .where(EventEnvelopeArchiveModel.topic == topic)
                .order_by(EventEnvelopeArchiveModel.source_sequence_id)
            ).all()
        return [self._to_schema(row) for row in [*archive_rows, *hot_rows]]

    def recent_by_topic(self, topic: str, *, limit: int) -> list[EventEnvelope]:
        if limit <= 0:
            return []
        with self.session_factory() as session:
            hot_rows = session.scalars(
                select(EventEnvelopeModel)
                .where(EventEnvelopeModel.topic == topic)
                .order_by(desc(EventEnvelopeModel.sequence_id))
                .limit(limit)
            ).all()
            archive_rows = session.scalars(
                select(EventEnvelopeArchiveModel)
                .where(EventEnvelopeArchiveModel.topic == topic)
                .order_by(desc(EventEnvelopeArchiveModel.source_sequence_id))
                .limit(limit)
            ).all()
        rows = [*archive_rows, *hot_rows]
        rows.sort(key=lambda row: getattr(row, "source_sequence_id", getattr(row, "sequence_id")))
        return [self._to_schema(row) for row in rows[-limit:]]

    def recent_by_topic_and_key(self, topic: str, *, key: str, limit: int) -> list[EventEnvelope]:
        if limit <= 0:
            return []
        with self.session_factory() as session:
            hot_rows = session.scalars(
                select(EventEnvelopeModel)
                .where(EventEnvelopeModel.topic == topic)
                .where(EventEnvelopeModel.event_key == key)
                .order_by(desc(EventEnvelopeModel.sequence_id))
                .limit(limit)
            ).all()
            archive_rows = session.scalars(
                select(EventEnvelopeArchiveModel)
                .where(EventEnvelopeArchiveModel.topic == topic)
                .where(EventEnvelopeArchiveModel.event_key == key)
                .order_by(desc(EventEnvelopeArchiveModel.source_sequence_id))
                .limit(limit)
            ).all()
        rows = [*archive_rows, *hot_rows]
        rows.sort(key=lambda row: getattr(row, "source_sequence_id", getattr(row, "sequence_id")))
        return [self._to_schema(row) for row in rows[-limit:]]

    def by_topic_scoped(
        self,
        topic: str,
        *,
        scope: RuntimeStateScope,
        limit: int | None = None,
    ) -> list[EventEnvelope]:
        query = (
            select(EventEnvelopeModel)
            .where(EventEnvelopeModel.topic == topic)
            .order_by(EventEnvelopeModel.sequence_id)
        )
        query = self._scope_query(query, scope, EventEnvelopeModel)
        with self.session_factory() as session:
            hot_rows = session.scalars(query).all()
            archive_query = (
                select(EventEnvelopeArchiveModel)
                .where(EventEnvelopeArchiveModel.topic == topic)
                .order_by(EventEnvelopeArchiveModel.source_sequence_id)
            )
            archive_query = self._scope_query(archive_query, scope, EventEnvelopeArchiveModel)
            archive_rows = session.scalars(archive_query).all()
        rows = [self._to_schema(row) for row in [*archive_rows, *hot_rows]]
        return rows if limit is None else rows[-limit:]

    def latest_by_topic_scoped(
        self,
        topic: str,
        *,
        scope: RuntimeStateScope,
        key: str | None = None,
    ) -> EventEnvelope | None:
        query = select(EventEnvelopeModel).where(EventEnvelopeModel.topic == topic)
        if key is not None:
            query = query.where(EventEnvelopeModel.event_key == key)
        query = self._scope_query(query, scope, EventEnvelopeModel).order_by(desc(EventEnvelopeModel.sequence_id)).limit(1)
        with self.session_factory() as session:
            row = session.scalar(query)
            if row is None:
                archive_query = select(EventEnvelopeArchiveModel).where(EventEnvelopeArchiveModel.topic == topic)
                if key is not None:
                    archive_query = archive_query.where(EventEnvelopeArchiveModel.event_key == key)
                archive_query = self._scope_query(archive_query, scope, EventEnvelopeArchiveModel).order_by(
                    desc(EventEnvelopeArchiveModel.source_sequence_id)
                ).limit(1)
                row = session.scalar(archive_query)
        return self._to_schema(row) if row is not None else None

    def by_decision(self, decision_id: str) -> list[EventEnvelope]:
        with self.session_factory() as session:
            hot_rows = session.scalars(
                select(EventEnvelopeModel)
                .where(EventEnvelopeModel.decision_id == decision_id)
                .order_by(EventEnvelopeModel.sequence_id)
            ).all()
            archive_rows = session.scalars(
                select(EventEnvelopeArchiveModel)
                .where(EventEnvelopeArchiveModel.decision_id == decision_id)
                .order_by(EventEnvelopeArchiveModel.source_sequence_id)
            ).all()
        return [self._to_schema(row) for row in [*archive_rows, *hot_rows]]

    def between(
        self,
        *,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        topic: str | None = None,
        decision_id: str | None = None,
    ) -> list[EventEnvelope]:
        query: Select[tuple[EventEnvelopeModel]] = select(EventEnvelopeModel)
        archive_query: Select[tuple[EventEnvelopeArchiveModel]] = select(EventEnvelopeArchiveModel)
        if start_at is not None:
            query = query.where(EventEnvelopeModel.event_timestamp >= start_at)
            archive_query = archive_query.where(EventEnvelopeArchiveModel.event_timestamp >= start_at)
        if end_at is not None:
            query = query.where(EventEnvelopeModel.event_timestamp <= end_at)
            archive_query = archive_query.where(EventEnvelopeArchiveModel.event_timestamp <= end_at)
        if topic is not None:
            query = query.where(EventEnvelopeModel.topic == topic)
            archive_query = archive_query.where(EventEnvelopeArchiveModel.topic == topic)
        if decision_id is not None:
            query = query.where(EventEnvelopeModel.decision_id == decision_id)
            archive_query = archive_query.where(EventEnvelopeArchiveModel.decision_id == decision_id)
        query = query.order_by(EventEnvelopeModel.sequence_id)
        archive_query = archive_query.order_by(EventEnvelopeArchiveModel.source_sequence_id)
        with self.session_factory() as session:
            hot_rows = session.scalars(query).all()
            archive_rows = session.scalars(archive_query).all()
        return [self._to_schema(row) for row in [*archive_rows, *hot_rows]]

    def archive_before(self, *, before_ts: datetime) -> dict[str, int]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(EventEnvelopeModel)
                .where(EventEnvelopeModel.event_timestamp < before_ts)
                .order_by(EventEnvelopeModel.sequence_id)
            ).all()
            archived_count = 0
            for row in rows:
                existing = session.scalar(
                    select(EventEnvelopeArchiveModel.archive_sequence_id).where(
                        EventEnvelopeArchiveModel.event_id == row.event_id
                    )
                )
                if existing is None:
                    session.add(
                        EventEnvelopeArchiveModel(
                            source_sequence_id=row.sequence_id,
                            event_id=row.event_id,
                            schema_version=row.schema_version,
                            created_at=row.created_at,
                            event_type=row.event_type,
                            event_timestamp=row.event_timestamp,
                            source_component=row.source_component,
                            topic=row.topic,
                            event_key=row.event_key,
                            decision_id=row.decision_id,
                            symbol=row.symbol,
                            timeframe=row.timeframe,
                            product_type=row.product_type,
                            margin_mode=row.margin_mode,
                            payload=row.payload,
                        )
                    )
                    archived_count += 1
                session.delete(row)
            session.commit()
            hot_event_count = int(session.scalar(select(func.count()).select_from(EventEnvelopeModel)) or 0)
            archive_event_count = int(session.scalar(select(func.count()).select_from(EventEnvelopeArchiveModel)) or 0)
        return {
            "archived_event_count": archived_count,
            "hot_event_count": hot_event_count,
            "archive_event_count": archive_event_count,
        }

    def archive_summary(self) -> dict[str, object]:
        with self.session_factory() as session:
            hot_event_count = int(session.scalar(select(func.count()).select_from(EventEnvelopeModel)) or 0)
            archive_event_count = int(session.scalar(select(func.count()).select_from(EventEnvelopeArchiveModel)) or 0)
            hot_window = session.execute(
                select(func.min(EventEnvelopeModel.event_timestamp), func.max(EventEnvelopeModel.event_timestamp))
            ).one()
            archive_window = session.execute(
                select(
                    func.min(EventEnvelopeArchiveModel.event_timestamp),
                    func.max(EventEnvelopeArchiveModel.event_timestamp),
                )
            ).one()
            replay_offset_count = int(session.scalar(select(func.count()).select_from(ReplayProjectionOffsetModel)) or 0)
        return {
            "hot_event_count": hot_event_count,
            "archive_event_count": archive_event_count,
            "total_event_count": hot_event_count + archive_event_count,
            "hot_window": {
                "start_at": hot_window[0],
                "end_at": hot_window[1],
            },
            "archive_window": {
                "start_at": archive_window[0],
                "end_at": archive_window[1],
            },
            "replay_offset_count": replay_offset_count,
        }

    def save_replay_offset(self, offset: ReplayProjectionOffset) -> ReplayProjectionOffset:
        with self.session_factory() as session:
            row = session.get(ReplayProjectionOffsetModel, offset.offset_id)
            scope_hash = self._scope_hash(tuple(offset.allowed_symbols))
            if row is None:
                row = session.scalar(
                    select(ReplayProjectionOffsetModel).where(
                        ReplayProjectionOffsetModel.projection_key == offset.projection_key,
                        ReplayProjectionOffsetModel.product_type == offset.product_type,
                        ReplayProjectionOffsetModel.margin_mode == offset.margin_mode,
                        ReplayProjectionOffsetModel.allowed_symbols_hash == scope_hash,
                    )
                )
            if row is None:
                row = ReplayProjectionOffsetModel(
                    offset_id=offset.offset_id,
                    projection_key=offset.projection_key,
                    product_type=offset.product_type,
                    margin_mode=offset.margin_mode,
                    allowed_symbols_hash=scope_hash,
                    allowed_symbols_json=list(offset.allowed_symbols),
                    last_event_id=offset.last_event_id,
                    last_event_timestamp=offset.last_event_timestamp,
                    baseline_generation_id=offset.baseline_generation_id,
                    exchange_ack_watermark_id=offset.exchange_ack_watermark_id,
                    updated_at=offset.updated_at,
                    payload=offset.model_dump(mode="json"),
                )
                session.add(row)
            else:
                row.offset_id = offset.offset_id
                row.projection_key = offset.projection_key
                row.product_type = offset.product_type
                row.margin_mode = offset.margin_mode
                row.allowed_symbols_hash = scope_hash
                row.allowed_symbols_json = list(offset.allowed_symbols)
                row.last_event_id = offset.last_event_id
                row.last_event_timestamp = offset.last_event_timestamp
                row.baseline_generation_id = offset.baseline_generation_id
                row.exchange_ack_watermark_id = offset.exchange_ack_watermark_id
                row.updated_at = offset.updated_at
                row.payload = offset.model_dump(mode="json")
            session.commit()
        return offset

    def latest_replay_offset(
        self,
        *,
        projection_key: str,
        scope: RuntimeStateScope,
    ) -> ReplayProjectionOffset | None:
        with self.session_factory() as session:
            row = session.scalar(
                select(ReplayProjectionOffsetModel)
                .where(
                    ReplayProjectionOffsetModel.projection_key == projection_key,
                    ReplayProjectionOffsetModel.product_type == scope.product_type,
                    ReplayProjectionOffsetModel.margin_mode == scope.margin_mode,
                    ReplayProjectionOffsetModel.allowed_symbols_hash == self._scope_hash(scope.allowed_symbols),
                )
                .order_by(desc(ReplayProjectionOffsetModel.updated_at))
                .limit(1)
            )
        return None if row is None else ReplayProjectionOffset.model_validate(row.payload)

    @staticmethod
    def _scope_query(query, scope: RuntimeStateScope, model):
        allowed_symbols = tuple(scope.allowed_symbols) if scope.allowed_symbols else (scope.default_symbol,)
        symbol_clause = or_(
            model.symbol.is_(None),
            model.symbol.in_(allowed_symbols),
        )
        strategy_family = model.payload["strategy_family"].as_string()
        regular_clause = and_(
            symbol_clause,
            or_(model.product_type.is_(None), model.product_type == scope.product_type),
            or_(model.margin_mode.is_(None), model.margin_mode == scope.margin_mode),
            or_(strategy_family.is_(None), strategy_family != "smart_arbitrage"),
        )
        if scope.product_type != "derivatives":
            return query.where(regular_clause)
        smart_arbitrage_clause = and_(
            symbol_clause,
            strategy_family == "smart_arbitrage",
            or_(
                and_(
                    model.product_type == "spot",
                    model.margin_mode.in_(tuple(scope.smart_arbitrage_spot_margin_modes)),
                ),
                and_(model.product_type == scope.product_type, model.margin_mode == scope.margin_mode),
            ),
        )
        return query.where(or_(regular_clause, smart_arbitrage_clause))

    @staticmethod
    def _decision_id(envelope: EventEnvelope) -> str | None:
        decision_id = envelope.payload.get("decision_id")
        return decision_id if isinstance(decision_id, str) else None

    @staticmethod
    def _to_schema(row: EventEnvelopeModel) -> EventEnvelope:
        return EventEnvelope(
            schema_version=row.schema_version,
            created_at=row.created_at,
            event_id=row.event_id,
            event_type=row.event_type,
            event_timestamp=row.event_timestamp,
            source_component=row.source_component,
            topic=row.topic,
            key=row.event_key,
            payload=row.payload,
        )

    @staticmethod
    def _scope_hash(allowed_symbols: tuple[str, ...]) -> str:
        normalized = ",".join(sorted(allowed_symbols))
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
