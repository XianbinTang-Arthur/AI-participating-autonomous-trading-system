from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.events import topics
from aats.schemas.audit import DecisionAuditRecord
from aats.schemas.common import EventEnvelope, utc_now
from aats.schemas.decision import DecisionContext
from aats.schemas.exchange import ExchangeAccountSnapshot
from aats.schemas.market import MarketSnapshot
from aats.schemas.execution import FillEvent
from aats.schemas.portfolio import InstrumentPositionState, PortfolioSnapshot
from aats.schemas.system import HealthSnapshot
from aats.services.fill_ordering import fill_processing_sort_key
from aats.services.governance_engine.health import SystemHealthService
from aats.services.governance_engine.mode import RuntimeModeController
from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12, to_decimal
from aats.services.portfolio_service.instrument_states import (
    instrument_position_state_for_symbol,
    instrument_position_states_from_snapshot_positions,
    spot_balance_position_state,
)
from aats.services.portfolio_service.position_keys import symbol_from_position_key
from aats.services.runtime_scope import (
    latest_matching_snapshot,
    runtime_state_scope,
    scoped_portfolio_event,
    snapshots_for_scope,
)
from aats.services.strategy_execution_guard_filters import (
    decision_ids_for_guard_exclusions,
    guard_excluded_fill_ids_for_independent_residual_exits,
)
from aats.services.strategy_execution_health import (
    compute_leg_strategy_execution_health,
    compute_strategy_execution_health,
)
from aats.services.execution_engine.fill_event_cache import FillEventHotCache
from aats.services.execution_engine.order_state_cache import OrderStateHotCache
from aats.services.portfolio_service.snapshot_cache import PortfolioSnapshotCache
from aats.storage.base import EventStore, ExecutionRepository, PortfolioRepository
from aats.storage.stream_snapshot_cache import StreamSnapshotCache


@dataclass(slots=True)
class LegLifecycleState:
    """Tracks lifecycle timestamps for a single position leg (long or short)."""

    qty: Decimal = Decimal("0")
    current_leg_opened_at: datetime | None = None
    last_leg_closed_at: datetime | None = None
    latest_fill_timestamp: datetime | None = None


class DecisionContextBuilder:
    def __init__(
        self,
        *,
        settings: AATSSettings,
        event_store: EventStore,
        portfolio_repo: PortfolioRepository,
        execution_repo: ExecutionRepository,
        mode_controller: RuntimeModeController,
        health_service: SystemHealthService,
        account_service: object | None = None,
        stream_snapshot_cache: StreamSnapshotCache | None = None,
        portfolio_snapshot_cache: PortfolioSnapshotCache | None = None,
        order_state_cache: OrderStateHotCache | None = None,
        fill_event_cache: FillEventHotCache | None = None,
    ) -> None:
        self.settings = settings
        self.event_store = event_store
        self.portfolio_repo = portfolio_repo
        self.execution_repo = execution_repo
        self.mode_controller = mode_controller
        self.health_service = health_service
        self.account_service = account_service
        self._stream_cache = stream_snapshot_cache
        self._portfolio_snapshot_cache = portfolio_snapshot_cache
        self._order_state_cache = order_state_cache
        self._fill_event_cache = fill_event_cache
        self.state_scope = runtime_state_scope(settings)

    def build_health_snapshot(self, *, decision_id: str) -> HealthSnapshot:
        snapshot = self.health_service.snapshot()
        return HealthSnapshot(
            decision_id=decision_id,
            mode=snapshot.mode,
            operating_state=snapshot.operating_state,
            status=snapshot.status,
            halted=snapshot.halted,
            blockers=list(snapshot.blockers),
            components=list(snapshot.components),
        )

    def build(
        self,
        symbol: str,
        timeframe: str,
        *,
        decision_id: str,
        health_snapshot_ref: str,
        feature_snapshot_hint: EventEnvelope | None = None,
        market_snapshot_hint: MarketSnapshot | None = None,
    ) -> DecisionContext:
        if timeframe not in self.settings.supported_timeframes:
            raise ValueError(f"Unsupported timeframe: {timeframe}")
        if not self.settings.symbol_allowed_for_decision_cycle(symbol):
            raise ValueError(f"symbol_not_enabled_for_decision_cycle:{symbol}")

        # 2026-04-23 P1-a：market_snapshot_hint 透传。对等于 feature_snapshot_hint，
        # 消除 trigger 评估 -> build 读取之间新 market snapshot 抢跑的 ref 漂移。
        #
        # 选择策略（与 feature 路径不完全对称，因为 market_gateway.latest_snapshot()
        # 只返回 MarketSnapshot 不含 EventEnvelope，而 trigger 里的 pending.market_snapshot
        # 也是 MarketSnapshot 类型）：
        #   1. 如果 hint 给了，**优先** 从 stream_cache 取 market envelope，若其 payload
        #      的 snapshot_ts 与 hint.snapshot_ts 相等 → 用该 envelope（保留规范 event_id
        #      供 audit/ replay 关联）。
        #   2. 若 stream_cache 已被新 snapshot 覆盖（hint 与 cache 不一致）→ 构造 inline
        #      envelope 承载 hint 数据，event_id 用 deterministic `inline_market_<sym>_<ts>`
        #      格式，audit 能看出这是来自 trigger 内联、非 event_store 规范 envelope。
        #   3. 若 hint 没给（集成测试 / operator API 等不走 trigger 的调用路径）→ 原始
        #      stream_cache / event_store latest 逻辑不变。
        market_event: EventEnvelope | None = None
        if market_snapshot_hint is not None:
            _cached_market = (
                self._stream_cache.latest(topics.MARKET_SNAPSHOTS, key=symbol)
                if self._stream_cache is not None
                else None
            )
            if _cached_market is not None:
                try:
                    _cached_snap = MarketSnapshot.model_validate(_cached_market.payload)
                    if _cached_snap.snapshot_ts == market_snapshot_hint.snapshot_ts:
                        market_event = _cached_market
                except Exception:
                    # cache 里的 envelope 格式异常不应 block 决策；走 inline 分支
                    market_event = None
            if market_event is None:
                # cache overwrite 或格式异常 → inline envelope 承载 hint
                _hint_ts_iso = market_snapshot_hint.snapshot_ts.isoformat()
                market_event = EventEnvelope(
                    event_id=f"inline_market_{symbol}_{_hint_ts_iso}",
                    event_type=type(market_snapshot_hint).__name__,
                    event_timestamp=market_snapshot_hint.snapshot_ts,
                    source_component="decision_engine_inline_from_trigger",
                    topic=topics.MARKET_SNAPSHOTS,
                    key=symbol,
                    payload=market_snapshot_hint.model_dump(mode="python"),
                )
        if market_event is None:
            market_event = (
                self._stream_cache.latest(topics.MARKET_SNAPSHOTS, key=symbol)
                if self._stream_cache is not None
                else None
            )
        if market_event is None:
            market_event = self.event_store.latest(topics.MARKET_SNAPSHOTS, key=symbol)
        # R3-P1-U-A：trigger 路径优先使用触发本次 run_cycle 的 feature envelope；
        # 这样 DecisionContext.feature_snapshot_ref 与 trigger_policy 评估依据的
        # snapshot 完全一致，消除 trigger 评估 -> build 读取之间新 snapshot
        # 抢跑导致的 ref 漂移（决策对应的 feature 与触发时评估的 feature 不是
        # 同一个 envelope，审计/回放时难以还原触发因果）。
        # 没传 hint 的调用路径（集成测试 / operator API 驱动的直接调用）继续
        # 走 latest() fallback，保留原有行为。
        feature_event: EventEnvelope | None = feature_snapshot_hint
        if feature_event is None:
            feature_event = (
                self._stream_cache.latest(topics.FEATURE_SNAPSHOTS, key=symbol)
                if self._stream_cache is not None
                else None
            )
        if feature_event is None:
            feature_event = self.event_store.latest(topics.FEATURE_SNAPSHOTS, key=symbol)

        # P1 fix: persist decision-referenced snapshots so replay / audit can
        # resolve market_snapshot_ref / feature_snapshot_ref.
        # High-frequency topics normally bypass Postgres (stream_cache only).
        # Here we persist ONLY the specific envelopes selected for this decision
        # cycle — max 2 appends per cycle, well below tick rate.
        # Both EventStore implementations handle duplicate event_ids idempotently.
        if self._stream_cache is not None:
            _persist_log = logging.getLogger("aats.decision_engine.context_builder")
            for _ref_envelope in (market_event, feature_event):
                if _ref_envelope is not None:
                    try:
                        self.event_store.append(_ref_envelope)
                    except Exception:
                        _persist_log.warning(
                            "audit_snapshot_persist_degraded "
                            "topic=%s event_id=%s symbol=%s decision_id=%s",
                            _ref_envelope.topic,
                            _ref_envelope.event_id,
                            symbol,
                            decision_id,
                            exc_info=True,
                        )

        portfolio_event = scoped_portfolio_event(
            self.event_store.by_topic(topics.PORTFOLIO_SNAPSHOTS),
            self.state_scope,
        )
        portfolio_snapshots = snapshots_for_scope(self.portfolio_repo, self.state_scope)
        # P1-3 优化：latest snapshot 优先走 PortfolioSnapshotCache（内存），
        # 仅在 cache miss 时 fallback 到 PG 查询。
        # portfolio_snapshots（history）仍走 PG，供 strategy_health / leg_lifecycle 使用。
        _cached_portfolio_snapshot = (
            self._portfolio_snapshot_cache.get_sync(self.state_scope)
            if self._portfolio_snapshot_cache is not None
            else None
        )
        portfolio_snapshot = (
            _cached_portfolio_snapshot
            if _cached_portfolio_snapshot is not None
            else latest_matching_snapshot(portfolio_snapshots, self.state_scope)
        )

        if market_event is None:
            raise RuntimeError("Market snapshot is required before building decision context")
        if feature_event is None:
            raise RuntimeError("Feature snapshot is required before building decision context")
        if portfolio_event is None and portfolio_snapshot is None:
            raise RuntimeError("Portfolio snapshot is required before building decision context")

        decision_as_of = utc_now()
        market_snapshot = MarketSnapshot.model_validate(market_event.payload)
        # P1-10：market_event 新鲜度检查。对齐行情 gateway 的 stale 阈值
        # (market_data_stale_after_seconds, 默认 45s)。超阈值说明 NATS 背压
        # 或上游断流，用陈旧价格做决策在极端行情下会方向错误 → 拒绝决策。
        _snapshot_ts = market_snapshot.snapshot_ts
        if _snapshot_ts is not None:
            _stale_seconds = (decision_as_of - _snapshot_ts).total_seconds()
            _stale_limit = float(self.settings.market_data_stale_after_seconds)
            if _stale_seconds > _stale_limit:
                raise RuntimeError(
                    f"Market snapshot is stale: symbol={symbol} "
                    f"age={_stale_seconds:.1f}s limit={_stale_limit:.1f}s "
                    f"snapshot_ts={_snapshot_ts.isoformat()}"
                )
        account_snapshot = self._account_snapshot()
        current_position_state = self._position_state(portfolio_snapshot, symbol, self.settings.trading_product_type)
        # Extract position fields once to eliminate repeated None-checks.
        _ZERO = Decimal("0")
        _ps = current_position_state
        current_position_qty = _ZERO if _ps is None else _ps.net_position_qty
        current_long_qty = _ZERO if _ps is None else _ps.long_position_qty
        current_short_qty = _ZERO if _ps is None else _ps.short_position_qty
        current_gross_qty = _ZERO if _ps is None else _ps.gross_position_qty
        current_net_notional = _ZERO if _ps is None else _ps.net_position_notional
        current_gross_notional = _ZERO if _ps is None else _ps.gross_position_notional
        current_long_notional = _ZERO if _ps is None else _ps.long_position_notional
        current_short_notional = _ZERO if _ps is None else _ps.short_position_notional
        current_legs = [] if _ps is None else list(_ps.legs)
        current_exposure_side = self._exposure_side(current_position_qty)
        # P1-1 优化：open orders 优先走 OrderStateHotCache（内存），
        # 仅在 cache miss 时 fallback 到 PG 查询。
        _cached_open_orders = (
            self._order_state_cache.open_orders_for_scope_sync(self.state_scope)
            if self._order_state_cache is not None
            else None
        )
        if _cached_open_orders is not None:
            open_orders = [
                o.client_order_id for o in _cached_open_orders
                if o.symbol == symbol
            ]
        else:
            open_orders = [
                order.client_order_id
                for order in self.execution_repo.order_states_for_scope(
                    scope=self.state_scope,
                    open_only=True,
                )
                if order.symbol == symbol
            ]
        # P1-2 优化：fills 优先走 FillEventHotCache（内存），
        # 仅在 cache miss 时 fallback 到 PG 查询。
        # Cache fills once to avoid repeated queries and ensure data consistency
        # across strategy_health, leg_strategy_health, and leg_lifecycle.
        _cached_fills = (
            self._fill_event_cache.fills_for_scope_sync(self.state_scope)
            if self._fill_event_cache is not None
            else None
        )
        scoped_fills = (
            _cached_fills
            if _cached_fills is not None
            else self.execution_repo.fills_for_scope(scope=self.state_scope)
        )
        symbol_fills = [fill for fill in scoped_fills if fill.symbol == symbol]
        guard_excluded_fill_ids = guard_excluded_fill_ids_for_independent_residual_exits(
            fills=symbol_fills,
            audits=self._audit_records_for_decision_ids(
                decision_ids_for_guard_exclusions(fills=symbol_fills)
            ),
            payload_by_ref=self._payload_by_ref,
        )
        strategy_health = compute_strategy_execution_health(
            settings=self.settings,
            symbol=symbol,
            fills=scoped_fills,
            snapshots=portfolio_snapshots,
            current_position_qty=current_position_qty,
            current_long_position_qty=current_long_qty,
            current_short_position_qty=current_short_qty,
            guard_excluded_fill_ids=guard_excluded_fill_ids,
            as_of=decision_as_of,
        )
        leg_strategy_health = compute_leg_strategy_execution_health(
            settings=self.settings,
            symbol=symbol,
            fills=scoped_fills,
            snapshots=portfolio_snapshots,
            current_long_position_qty=current_long_qty,
            current_short_position_qty=current_short_qty,
            guard_excluded_fill_ids=guard_excluded_fill_ids,
            as_of=decision_as_of,
        )
        leg_lifecycle = self._leg_lifecycle(
            symbol=symbol,
            fills=scoped_fills,
            current_position_state=current_position_state,
            snapshot_ts=None if portfolio_snapshot is None else portfolio_snapshot.snapshot_ts,
            snapshots=portfolio_snapshots,
        )
        current_leg_qty_by_side = {"long": current_long_qty, "short": current_short_qty}
        guardrails = strategy_health.active_guardrails(
            settings=self.settings,
            as_of=decision_as_of,
            current_position_qty=current_position_qty,
        )
        return DecisionContext(
            decision_id=decision_id,
            symbol=symbol,
            timeframe=timeframe,
            as_of_ts=decision_as_of,
            market_snapshot_ref=market_event.event_id,
            feature_snapshot_ref=feature_event.event_id,
            portfolio_snapshot_ref=(
                portfolio_event.event_id
                if portfolio_event is not None
                else f"portfolio_snapshot:{portfolio_snapshot.created_at.isoformat()}"
            ),
            health_snapshot_ref=health_snapshot_ref,
            mode=self.mode_controller.mode,
            policy_flags=[],
            risk_budget_state={
                "max_abs_position_qty": to_decimal(self.settings.max_abs_position_qty),
                "max_target_leverage": to_decimal(self.settings.max_target_leverage),
            },
            current_position_qty=current_position_qty,
            current_position_state=current_position_state,
            current_position_legs=current_legs,
            current_net_position_qty=current_position_qty,
            current_gross_position_qty=current_gross_qty,
            current_long_position_qty=current_long_qty,
            current_short_position_qty=current_short_qty,
            current_net_position_notional=current_net_notional,
            current_gross_position_notional=current_gross_notional,
            current_long_position_notional=current_long_notional,
            current_short_position_notional=current_short_notional,
            current_long_leg_opened_at=leg_lifecycle["long"].current_leg_opened_at,
            current_short_leg_opened_at=leg_lifecycle["short"].current_leg_opened_at,
            last_long_leg_closed_at=leg_lifecycle["long"].last_leg_closed_at,
            last_short_leg_closed_at=leg_lifecycle["short"].last_leg_closed_at,
            latest_long_leg_fill_timestamp=leg_lifecycle["long"].latest_fill_timestamp,
            latest_short_leg_fill_timestamp=leg_lifecycle["short"].latest_fill_timestamp,
            current_open_orders=open_orders,
            product_type=self.settings.trading_product_type,
            current_exposure_side=current_exposure_side,
            current_target_leverage=self._current_target_leverage(portfolio_snapshot, symbol),
            current_position_opened_at=strategy_health.current_position_opened_at,
            last_position_closed_at=strategy_health.last_position_closed_at,
            latest_fill_timestamp=strategy_health.latest_fill_timestamp,
            recent_closed_trade_count=strategy_health.recent_closed_trade_count,
            recent_win_rate=strategy_health.recent_win_rate,
            recent_fee_drag_ratio=strategy_health.recent_fee_drag_ratio,
            recent_churn_ratio=strategy_health.recent_churn_ratio,
            recent_low_edge_trade_streak=strategy_health.recent_low_edge_trade_streak,
            recent_low_edge_trade_at=strategy_health.recent_low_edge_trade_at,
            recent_guard_eligible_closed_trade_count=(
                strategy_health.recent_guard_eligible_closed_trade_count
                or 0
            ),
            recent_guard_eligible_win_rate=(
                strategy_health.recent_guard_eligible_win_rate
                or 0.0
            ),
            recent_guard_eligible_fee_drag_ratio=(
                strategy_health.recent_guard_eligible_fee_drag_ratio
                or 0.0
            ),
            recent_guard_eligible_churn_ratio=(
                strategy_health.recent_guard_eligible_churn_ratio
                or 0.0
            ),
            recent_guard_eligible_low_edge_trade_streak=(
                strategy_health.recent_guard_eligible_low_edge_trade_streak
                or 0
            ),
            recent_guard_eligible_low_edge_trade_at=(
                strategy_health.recent_guard_eligible_low_edge_trade_at
            ),
            recent_guard_eligible_net_realized_pnl=(
                strategy_health.recent_guard_eligible_net_realized_pnl
                or Decimal("0")
            ),
            leg_strategy_health={
                leg: snapshot.as_payload(
                    settings=self.settings,
                    as_of=decision_as_of,
                    current_position_qty=current_leg_qty_by_side.get(leg, Decimal("0")),
                )
                for leg, snapshot in leg_strategy_health.items()
            },
            strategy_guardrail_flags=list(guardrails["flags"]),
            strategy_cooldowns=dict(guardrails["cooldowns"]),
            market_last_price=to_decimal(market_snapshot.last_price),
            available_trading_equity=self._available_trading_equity(
                account_snapshot=account_snapshot,
                portfolio_snapshot=portfolio_snapshot,
            ),
            market_snapshot=market_snapshot,
        )

    def _payload_by_ref(self, ref: str | None) -> dict[str, object] | None:
        if ref is None:
            return None
        envelope = self.event_store.get(ref)
        if envelope is None:
            return None
        payload = dict(envelope.payload)
        payload["_event_id"] = envelope.event_id
        payload["_topic"] = envelope.topic
        return payload

    def _audit_records_for_decision_ids(self, decision_ids: set[str]) -> list[DecisionAuditRecord]:
        records: dict[str, DecisionAuditRecord] = {}
        for decision_id in decision_ids:
            for envelope in self.event_store.by_decision(decision_id):
                if envelope.topic != topics.AUDIT_RECORDS:
                    continue
                try:
                    record = DecisionAuditRecord.model_validate(envelope.payload)
                except Exception:
                    continue
                records[record.decision_id] = record
        return list(records.values())

    def _account_snapshot(self) -> ExchangeAccountSnapshot | None:
        loader = getattr(self.account_service, "latest_snapshot", None)
        if not callable(loader):
            return None
        snapshot = loader()
        return snapshot if isinstance(snapshot, ExchangeAccountSnapshot) else None

    @staticmethod
    def _available_trading_equity(
        *,
        account_snapshot: ExchangeAccountSnapshot | None,
        portfolio_snapshot: PortfolioSnapshot | None,
    ) -> Decimal:
        """Resolve the best available equity for position sizing.

        Fallback chain (first positive value wins):
        1. ``risk_snapshot.available_equity`` — OKX ``availEq`` (ideal, but OKX
           does not return it in the ``account-position-risk`` endpoint for all
           account modes).
        2. ``risk_snapshot.total_equity`` — OKX ``totalEq`` or summed stable-coin
           balance equity from the same endpoint.
        3. ``portfolio_snapshot.total_equity`` — locally-tracked equity from
           the portfolio service.
        """
        risk = (
            account_snapshot.risk_snapshot
            if account_snapshot is not None
            else None
        )
        # 1. Prefer exchange-reported available equity
        if risk is not None:
            avail = to_decimal(risk.available_equity or 0)
            if avail > EPSILON_DECIMAL_12:
                return avail
        # 2. Fallback: exchange-reported total equity
        if risk is not None:
            total = to_decimal(risk.total_equity or 0)
            if total > EPSILON_DECIMAL_12:
                return total
        # 3. Fallback: portfolio snapshot equity
        if portfolio_snapshot is not None:
            portfolio_eq = to_decimal(portfolio_snapshot.total_equity or 0)
            if portfolio_eq > EPSILON_DECIMAL_12:
                return portfolio_eq
        # R4-D4：三级回退全部为空/非正时必须喊出来。
        # 返回 0 会污染 resolve_balance_aware_reference_qty 的
        # 余额感知下限逻辑（balance * ratio → 0），
        # 让 P0-D4 的零余额 guard 必须是唯一的托底。
        # 如果 equity 源真的全坏了，运营侧需要立刻知道，
        # 而不是等到决策被 guard 挡掉之后再事后定位。
        _equity_log = logging.getLogger("aats.decision_engine.context_builder")
        _equity_log.critical(
            "available_trading_equity_all_fallbacks_exhausted "
            "account_snapshot_present=%s risk_present=%s "
            "avail_eq=%s total_eq=%s "
            "portfolio_snapshot_present=%s portfolio_total_equity=%s",
            account_snapshot is not None,
            risk is not None,
            getattr(risk, "available_equity", None) if risk is not None else None,
            getattr(risk, "total_equity", None) if risk is not None else None,
            portfolio_snapshot is not None,
            getattr(portfolio_snapshot, "total_equity", None)
            if portfolio_snapshot is not None
            else None,
        )
        return Decimal("0")

    @staticmethod
    def _position_state(
        snapshot: PortfolioSnapshot | None,
        symbol: str,
        product_type: str = "spot",
    ) -> InstrumentPositionState | None:
        if snapshot is None:
            return None
        states = instrument_position_states_from_snapshot_positions(
            position
            for position in snapshot.positions
            if position.symbol == symbol
        )
        state = instrument_position_state_for_symbol(states, symbol)
        if state is not None:
            return state
        if product_type == "spot" and "-" in symbol:
            base_currency, _quote_currency = symbol.split("-", 1)
            return spot_balance_position_state(
                symbol=symbol,
                quantity=snapshot.balances.get(base_currency, Decimal("0")),
            )
        return None

    @staticmethod
    def _current_target_leverage(snapshot: PortfolioSnapshot | None, symbol: str) -> float:
        if snapshot is None:
            return 1.0
        direct = snapshot.leverage_profile.get(symbol)
        if direct is not None:
            return float(direct)
        leverages = [
            float(snapshot.leverage_profile.get(position.position_key or position.symbol, position.target_leverage))
            for position in snapshot.positions
            if position.symbol == symbol
        ]
        if leverages:
            return max(leverages)
        for key, value in snapshot.leverage_profile.items():
            if symbol_from_position_key(key) == symbol:
                leverages.append(float(value))
        return max(leverages) if leverages else 1.0

    @staticmethod
    def _leg_lifecycle(
        *,
        symbol: str,
        fills: list[FillEvent],
        current_position_state: InstrumentPositionState | None,
        snapshot_ts: datetime | None = None,
        snapshots: list[PortfolioSnapshot] | None = None,
    ) -> dict[str, LegLifecycleState]:
        lifecycle = {"long": LegLifecycleState(), "short": LegLifecycleState()}
        ordered_fills = sorted(
            [
                fill
                for fill in fills
                if fill.symbol == symbol and fill.pos_side in {"long", "short"}
            ],
            key=fill_processing_sort_key,
        )
        for fill in ordered_fills:
            side = str(fill.pos_side)
            leg_state = lifecycle[side]
            previous_qty = to_decimal(leg_state.qty)
            fill_qty = abs(to_decimal(fill.fill_qty))
            delta_qty = (
                fill_qty if (side == "long" and fill.side == "buy") or (side == "short" and fill.side == "sell")
                else -fill_qty
            )
            next_qty = max(previous_qty + delta_qty, Decimal("0"))
            leg_state.qty = next_qty
            leg_state.latest_fill_timestamp = fill.ingestion_timestamp
            if previous_qty <= EPSILON_DECIMAL_12 and next_qty > EPSILON_DECIMAL_12:
                leg_state.current_leg_opened_at = fill.ingestion_timestamp
            elif previous_qty > EPSILON_DECIMAL_12 and next_qty <= EPSILON_DECIMAL_12:
                leg_state.last_leg_closed_at = fill.ingestion_timestamp
                leg_state.current_leg_opened_at = None

        current_long_qty = Decimal("0") if current_position_state is None else current_position_state.long_position_qty
        current_short_qty = Decimal("0") if current_position_state is None else current_position_state.short_position_qty
        current_by_side = {
            "long": to_decimal(current_long_qty),
            "short": to_decimal(current_short_qty),
        }
        for side, current_qty in current_by_side.items():
            leg_state = lifecycle[side]
            reconstructed_qty = to_decimal(leg_state.qty)
            if current_qty <= EPSILON_DECIMAL_12:
                leg_state.qty = Decimal("0")
                leg_state.current_leg_opened_at = None
            elif abs(reconstructed_qty - current_qty) > EPSILON_DECIMAL_12:
                leg_state.qty = current_qty
                conservative_anchor = (
                    leg_state.latest_fill_timestamp
                    or DecisionContextBuilder._continuous_open_anchor_from_snapshot_history(
                        snapshots=snapshots or [],
                        symbol=symbol,
                        side=side,
                    )
                    or snapshot_ts
                )
                leg_state.latest_fill_timestamp = conservative_anchor
                leg_state.current_leg_opened_at = conservative_anchor
        return lifecycle

    @staticmethod
    def _continuous_open_anchor_from_snapshot_history(
        *,
        snapshots: list[PortfolioSnapshot],
        symbol: str,
        side: str,
    ) -> datetime | None:
        anchor = None
        ordered_snapshots = sorted(snapshots, key=lambda snapshot: snapshot.snapshot_ts)
        for snapshot in ordered_snapshots:
            state = DecisionContextBuilder._position_state(snapshot, symbol, "derivatives")
            leg_qty = Decimal("0")
            if state is not None:
                leg_qty = (
                    to_decimal(state.long_position_qty)
                    if side == "long"
                    else to_decimal(state.short_position_qty)
                )
            if leg_qty > EPSILON_DECIMAL_12:
                if anchor is None:
                    anchor = snapshot.snapshot_ts
            else:
                anchor = None
        return anchor

    @staticmethod
    def _exposure_side(quantity: Decimal) -> str:
        if quantity > EPSILON_DECIMAL_12:
            return "long"
        if quantity < -EPSILON_DECIMAL_12:
            return "short"
        return "flat"
