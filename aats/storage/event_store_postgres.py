from __future__ import annotations

import hashlib
from datetime import datetime

from sqlalchemy import Select, and_, desc, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from aats.schemas.common import EventEnvelope
from aats.schemas.reconciliation import ReplayProjectionOffset
from aats.services.runtime_scope import RuntimeStateScope
from aats.storage.scope_metadata import envelope_scope_metadata
from aats.storage.sqlalchemy_models import EventEnvelopeArchiveModel, EventEnvelopeModel, ReplayProjectionOffsetModel


def _row_sort_seq(row: EventEnvelopeModel | EventEnvelopeArchiveModel) -> int:
    """Return a comparable sequence number for merged hot/archive row sorting.

    EventEnvelopeModel 有 ``sequence_id``，EventEnvelopeArchiveModel 只有
    ``source_sequence_id``（原表 PK 映射）。使用 ``hasattr`` 而非嵌套
    ``getattr`` 避免 Python 即时求值默认参数导致 AttributeError。
    """
    if hasattr(row, "source_sequence_id"):
        return row.source_sequence_id
    return row.sequence_id


class PostgresEventStore:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def append(self, envelope: EventEnvelope) -> None:
        with self.session_factory() as session:
            self.append_in_session(session, envelope)
            session.commit()

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
                # 用 model_dump(mode="json") 而非 envelope.payload:
                # payload 是原始 dict, 可能含 datetime 对象; psycopg 的 JSON
                # 编码器不认 datetime → TypeError "Object of type datetime is
                # not JSON serializable". 走 Pydantic 先序列化成 ISO 字符串.
                # (2026-04-23 诊断: decision service 每小时 191 条 warning)
                payload=envelope.model_dump(mode="json")["payload"],
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
        rows.sort(key=_row_sort_seq)
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
        rows.sort(key=_row_sort_seq)
        return [self._to_schema(row) for row in rows[-limit:]]

    def by_topic_scoped(
        self,
        topic: str,
        *,
        scope: RuntimeStateScope,
        limit: int | None = None,
    ) -> list[EventEnvelope]:
        if limit is not None:
            # SQL-level LIMIT: 只取最新 N 条，避免全表扫描撑爆内存。
            # 修复前：SELECT * ORDER BY ASC (无 LIMIT) → 全量读入 Python →
            # rows[-limit:] 切片。coordinator_snapshots 231 行 × 33KB = 7.7MB
            # 全部拉进 gateway 内存，是 1024M 触顶的直接原因。
            hot_query = select(EventEnvelopeModel).where(
                EventEnvelopeModel.topic == topic,
            )
            hot_query = self._scope_query(hot_query, scope, EventEnvelopeModel)
            hot_query = hot_query.order_by(
                desc(EventEnvelopeModel.sequence_id),
            ).limit(limit)

            with self.session_factory() as session:
                hot_rows = session.scalars(hot_query).all()
                archive_query = select(EventEnvelopeArchiveModel).where(
                    EventEnvelopeArchiveModel.topic == topic,
                )
                archive_query = self._scope_query(
                    archive_query, scope, EventEnvelopeArchiveModel,
                )
                archive_query = archive_query.order_by(
                    desc(EventEnvelopeArchiveModel.source_sequence_id),
                ).limit(limit)
                archive_rows = session.scalars(archive_query).all()

            # 合并两表结果（各最多 limit 条），按时间正序排列，取最新 limit 条
            rows = [*archive_rows, *hot_rows]
            rows.sort(key=_row_sort_seq)
            return [self._to_schema(r) for r in rows[-limit:]]
        else:
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
                archive_query = self._scope_query(
                    archive_query, scope, EventEnvelopeArchiveModel,
                )
                archive_rows = session.scalars(archive_query).all()
            return [self._to_schema(row) for row in [*archive_rows, *hot_rows]]

    def count_by_topic_scoped(
        self,
        topic: str,
        *,
        scope: RuntimeStateScope,
    ) -> int:
        """返回指定 topic + scope 的事件数（hot + archive 两表合计）。

        与 ``by_topic_scoped(..., limit=None)`` 不同，本方法在 SQL 层面
        直接 ``SELECT count(*)`` 避免把 12K+ 行 jsonb payload 拉进 Python
        再 ``len()``。本方法是 gateway_slow_query_systematic_fix_sow.md §S1
        的核心改动：将 ``decision_context_events`` / ``order_intent_events``
        这类 metrics 聚合从"全量拉取再计数"降级为"纯计数"，预期把单路
        时延从 45s 降到 <100ms。

        Scope 过滤语义与 ``by_topic_scoped`` 完全一致（共用 ``_scope_query``
        构造 WHERE 子句），保证 count 结果等于 ``len(by_topic_scoped(...))``。
        """
        hot_query = (
            select(func.count())
            .select_from(EventEnvelopeModel)
            .where(EventEnvelopeModel.topic == topic)
        )
        hot_query = self._scope_query(hot_query, scope, EventEnvelopeModel)

        archive_query = (
            select(func.count())
            .select_from(EventEnvelopeArchiveModel)
            .where(EventEnvelopeArchiveModel.topic == topic)
        )
        archive_query = self._scope_query(
            archive_query, scope, EventEnvelopeArchiveModel
        )

        with self.session_factory() as session:
            hot_count = session.scalar(hot_query) or 0
            archive_count = session.scalar(archive_query) or 0
        return int(hot_count) + int(archive_count)

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
