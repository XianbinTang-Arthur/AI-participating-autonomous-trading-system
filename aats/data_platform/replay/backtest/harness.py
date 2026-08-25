"""Backtest integration harness (Phase 3 MVP).

目标
----
把 Phase 1/2 产出的纯计算组件（``FillSimulator`` / ``PositionTracker`` /
``EquityBuilder`` / ``CostValidator``）和 replay core 的 adapter + bar loader
串成一条 end-to-end pipeline（FS-003 ``next_bar_event_v2``）:

    load_gold_bars → validate closed bars → adapter.evaluate_bar(closed bar) →
        queue FillRequest → next bar event → FillSimulator.simulate →
            PositionTracker.apply_fill → EquityBuilder.record (bar-close MtM) →
                CostValidator.record

Boundary
--------
* 只读取 Gold replay bars，只调用 Phase 1/2 公开接口
* 绝不修改 live path（``aats/services/`` / ``configs/``）
* 绝不修改 replay core（``aats/data_platform/replay/core/``）
* 无额外 DB 写入、无消息总线副作用；日志仅通过 stdlib ``logging``

本文件对调用方只暴露 4 个 public symbol：``BacktestConfig``、
``BacktestResult``、``ExecutionTimingRecord``、``run_backtest``。CLI 层
（``aats.cli``）直接消费。
"""

from __future__ import annotations

import copy
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy.orm import Session

from aats.data_platform.replay.adapters.base_adapter import BaseReplayAdapter
from aats.data_platform.replay.adapters.independent_adapter import (
    IndependentReplayAdapter,
)
from aats.data_platform.replay.backtest.cost_validator import (
    CostDiagnostic,
    CostValidationSummary,
    CostValidator,
)
from aats.data_platform.replay.backtest.equity_builder import (
    BacktestSummary,
    EquityBuilder,
    EquityPoint,
)
from aats.data_platform.replay.backtest.fill_simulator import (
    FILL_MODEL_VERSION,
    FillRequest,
    FillSimulator,
)
from aats.data_platform.replay.backtest.position_tracker import (
    Fill,
    PositionTracker,
)
from aats.data_platform.replay.core.replay_context import (
    ReplayBar,
    ReplayBarContext,
    ReplayDecision,
    ReplayParameterOverrides,
    ReplayState,
)
from aats.data_platform.replay.core.replay_runner import load_gold_bars

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config / Result DTOs
# ---------------------------------------------------------------------------


_DELTA_EPSILON = Decimal("0.0000001")
"""|delta| 小于该值视为 no-op（不生成 FillRequest）。

adapter 目前只会输出 -1 / 0 / +1 三档，epsilon 只作防御性判断。
"""

_EXECUTION_MODEL_VERSION = "next_bar_event_v2"


@dataclass(frozen=True)
class ExecutionTimingRecord:
    """一笔 replay 决策的因果时间线。

    ``ReplayDecision.ts`` 继续表示 observation bar 的开始时间；本结构把
    完整 K 线可观察、决策、提交、下一可交易事件、解析与成交时间分开，
    防止调用方重新把 bar identity 当作可成交时间。
    """

    decision_id: str
    action: str
    observation_bar_start_ts: datetime
    observation_completed_at_ts: datetime
    decision_ts: datetime
    submitted_at_ts: datetime | None
    next_tradable_event_ts: datetime | None
    resolved_at_ts: datetime | None
    fill_ts: datetime | None
    status: Literal[
        "no_order",
        "filled",
        "partial_fill",
        "no_fill",
        "expired_no_next_event",
    ]
    price_source: Literal["next_bar_open", "next_bar_close"] | None
    liquidity_source: Literal[
        "observation_bar_volume",
        "next_bar_volume",
    ] | None = None


@dataclass(frozen=True)
class BacktestConfig:
    """Backtest 运行参数。

    所有字段都有默认值，调用方仅需覆盖关心的维度。``order_type`` 对应
    :class:`FillSimulator` 的 3 个分支。三者都受 OHLCV participation cap，
    不能保证全量成交。
    """

    symbol: str = "BTC-USDT-SWAP"
    timeframe: str = "1h"
    dataset_version: str = "v1.0"
    family: str = "independent"
    order_type: Literal["ioc", "post_only", "bounded_limit"] = "ioc"
    contract_multiplier: Decimal = Decimal("0.01")
    # FillSimulator 参数（透传）
    maker_fee_bps: float = 2.0
    taker_fee_bps: float = 5.0
    ioc_slippage_bps: float = 1.0
    max_volume_participation: Decimal = Decimal("0.01")
    # Cost validator 的决策侧假设 cost（用户自行校准 / 对齐生产端）
    assumed_cost_bps: float = 6.0
    # 固定的安全时间模型；不提供 same-bar 兼容开关。
    execution_model_version: Literal["next_bar_event_v2"] = _EXECUTION_MODEL_VERSION
    fill_model_version: Literal["ohlcv_participation_cap_v2"] = FILL_MODEL_VERSION


@dataclass(frozen=True)
class BacktestResult:
    """一次 backtest 运行的聚合结果。

    * ``summary`` / ``cost_summary`` 已是 frozen dataclass，可直接 JSON 化
    * ``equity_curve`` 以 tuple 形式暴露，保证外部不可变
    """

    config: BacktestConfig
    summary: BacktestSummary
    cost_summary: CostValidationSummary
    equity_curve: tuple[EquityPoint, ...]
    decisions_count: int
    fills_count: int
    start_ts: datetime
    end_ts: datetime
    # 2026-04-23 P3-scorecard: 透出 per-decision cost diagnostics, 供下游
    # evidence_scorecard 消费; 不改变既有字段语义。
    cost_diagnostics: tuple[CostDiagnostic, ...] = ()
    execution_timeline: tuple[ExecutionTimingRecord, ...] = ()


# ---------------------------------------------------------------------------
# Runtime context (internal)
# ---------------------------------------------------------------------------


@dataclass
class _RuntimeContext:
    """Harness 内部运行态，集中管理 4 个协作者实例。"""

    fill_simulator: FillSimulator
    position_tracker: PositionTracker
    equity_builder: EquityBuilder
    cost_validator: CostValidator
    fills_count: int = 0
    decisions_count: int = 0
    # 跟踪仓位方向（adapter 只给 |delta|，direction 从 long/short score 派生）
    current_position_side: Literal["flat", "long", "short"] = "flat"
    score_history: list[float] = field(default_factory=list)
    execution_timeline: list[ExecutionTimingRecord] = field(default_factory=list)


@dataclass(frozen=True)
class _PendingOrder:
    """由已闭合 observation bar 产生、等待下一事件解析的订单。"""

    decision: ReplayDecision
    fill_request: FillRequest
    proposed_state: ReplayState
    observation_bar_start_ts: datetime
    observation_completed_at_ts: datetime
    liquidity_reference_volume: Decimal


# ---------------------------------------------------------------------------
# Public orchestrator
# ---------------------------------------------------------------------------


def run_backtest(
    session: Session,
    *,
    config: BacktestConfig,
    start_ts: datetime,
    end_ts: datetime,
    parameter_overrides: dict[str, Any] | None = None,
    adapter: BaseReplayAdapter | None = None,
) -> BacktestResult:
    """Execute end-to-end backtest over ``[start_ts, end_ts)``.

    流程（FS-003 因果时间契约）::

        1. load Gold bars
        2. validate closed/ordered bars and derive bar-end timestamps
        3. resolve prior decision at the next bar event
        4. adapter.evaluate_bar(closed bar) -> ReplayDecision
        5. queue decision.delta -> FillRequest for a later event
        6. tracker.mark_to_market(bar.close, bar_end)
        7. equity_builder.record / cost_validator.record

    Args:
        session: SQLAlchemy 会话（用于 ``load_gold_bars``）。
        config: Backtest 参数（见 :class:`BacktestConfig`）。
        start_ts: 起始时间（inclusive）。
        end_ts: 结束时间（exclusive）。
        parameter_overrides: 透传给 adapter 的策略参数覆盖（平坦 dict，
            被 ``ReplayParameterOverrides.from_dict`` 消化）。``None`` 时
            走 ``for_family(config.family)`` 的默认值。
        adapter: 依赖注入口子，便于单测塞 fake adapter。缺省时按
            ``config.family`` 构造。

    Returns:
        BacktestResult，包含 summary / cost_summary / equity curve 等。
    """

    if config.execution_model_version != _EXECUTION_MODEL_VERSION:
        raise ValueError(
            "Unsupported execution_model_version: "
            f"{config.execution_model_version!r}; "
            f"required={_EXECUTION_MODEL_VERSION!r}"
        )
    if config.fill_model_version != FILL_MODEL_VERSION:
        raise ValueError(
            "Unsupported fill_model_version: "
            f"{config.fill_model_version!r}; required={FILL_MODEL_VERSION!r}"
        )
    if not isinstance(config.max_volume_participation, Decimal) or not (
        config.max_volume_participation.is_finite()
        and Decimal("0") < config.max_volume_participation <= Decimal("1")
    ):
        raise ValueError(
            "max_volume_participation must be in (0, 1]: "
            f"{config.max_volume_participation!r}"
        )

    # -- 1. Bars --------------------------------------------------------
    bars: list[ReplayBar] = load_gold_bars(
        session,
        symbol=config.symbol,
        timeframe=config.timeframe,
        start_ts=start_ts,
        end_ts=end_ts,
        dataset_version=config.dataset_version,
    )
    log.info(
        "run_backtest: loaded %d Gold bars "
        "(symbol=%s timeframe=%s dv=%s execution_model=%s fill_model=%s)",
        len(bars),
        config.symbol,
        config.timeframe,
        config.dataset_version,
        config.execution_model_version,
        config.fill_model_version,
    )

    bar_duration = _parse_timeframe(config.timeframe)
    _validate_bar_sequence(bars, bar_duration=bar_duration)

    # -- 2. Adapter + params -------------------------------------------
    if adapter is None:
        adapter = _build_default_adapter(config.family)
    params = _build_replay_params(config.family, parameter_overrides)

    # -- 3. Runtime context --------------------------------------------
    ctx = _RuntimeContext(
        fill_simulator=FillSimulator(
            maker_fee_bps=config.maker_fee_bps,
            taker_fee_bps=config.taker_fee_bps,
            ioc_slippage_bps=config.ioc_slippage_bps,
            max_volume_participation=config.max_volume_participation,
        ),
        position_tracker=PositionTracker(
            symbol=config.symbol,
            contract_multiplier=config.contract_multiplier,
        ),
        equity_builder=EquityBuilder(),
        cost_validator=CostValidator(),
    )

    # -- 4. Bar-by-bar loop --------------------------------------------
    state = adapter.reset_state()
    pending_order: _PendingOrder | None = None
    for bar_index, bar in enumerate(bars):
        # 先解析上一根已闭合 K 线产生的订单。IOC/bounded 使用本 bar open；
        # post-only 需要本 bar 完整 volume，只在本 bar close 时解析。
        if pending_order is not None:
            state = _execute_pending_order(
                pending=pending_order,
                execution_bar=bar,
                bar_duration=bar_duration,
                config=config,
                ctx=ctx,
                current_state=state,
            )
            pending_order = None

        # adapter 会原地推进 ReplayState。交易动作必须等到下一事件真实 fill
        # 后才能提交，所以在副本上评估；no-fill/terminal expiry 丢弃提议状态。
        proposed_state = copy.deepcopy(state)
        proposed_state.bar_index = bar_index
        proposed_state.score_history = list(ctx.score_history)
        bar_ctx = ReplayBarContext(
            bar=bar,
            bar_index=bar_index,
            state=proposed_state,
            params=params,
            family=adapter.family_name,
            symbol=config.symbol,
            timeframe=config.timeframe,
            dataset_version=config.dataset_version,
        )
        decision: ReplayDecision = adapter.evaluate_bar(bar_ctx)
        if decision.ts != bar.ts:
            raise ValueError(
                "ReplayDecision.ts must equal observation bar start: "
                f"bar_index={bar_index} bar_ts={bar.ts.isoformat()} "
                f"decision_ts={decision.ts.isoformat()}"
            )
        ctx.decisions_count += 1
        # 对齐 replay_runner：维护 runner-side score history
        ctx.score_history.append(max(decision.long_score, decision.short_score))
        proposed_state.score_history = list(ctx.score_history)

        observation_completed_at = bar.ts + bar_duration
        delta = decision.delta_position_qty
        if abs(delta) >= _DELTA_EPSILON:
            side = _resolve_fill_side(decision, ctx.current_position_side)
            fill_request = _build_fill_request(
                decision=decision,
                side=side,
                delta=delta,
                order_type=config.order_type,
                submitted_at_ts=observation_completed_at,
            )
            pending_order = _PendingOrder(
                decision=decision,
                fill_request=fill_request,
                proposed_state=proposed_state,
                observation_bar_start_ts=bar.ts,
                observation_completed_at_ts=observation_completed_at,
                liquidity_reference_volume=bar.volume or Decimal("0"),
            )
        else:
            state = proposed_state
            ctx.execution_timeline.append(
                ExecutionTimingRecord(
                    decision_id=_decision_id(decision),
                    action=decision.action,
                    observation_bar_start_ts=bar.ts,
                    observation_completed_at_ts=observation_completed_at,
                    decision_ts=observation_completed_at,
                    submitted_at_ts=None,
                    next_tradable_event_ts=None,
                    resolved_at_ts=observation_completed_at,
                    fill_ts=None,
                    status="no_order",
                    price_source=None,
                )
            )

        _record_bar_close_mtm(bar, bar_duration=bar_duration, ctx=ctx)

    if pending_order is not None:
        # 数据集末端没有下一条可交易事件，禁止在 observation close 补成交。
        ctx.execution_timeline.append(
            ExecutionTimingRecord(
                decision_id=_decision_id(pending_order.decision),
                action=pending_order.decision.action,
                observation_bar_start_ts=pending_order.observation_bar_start_ts,
                observation_completed_at_ts=(
                    pending_order.observation_completed_at_ts
                ),
                decision_ts=pending_order.observation_completed_at_ts,
                submitted_at_ts=pending_order.observation_completed_at_ts,
                next_tradable_event_ts=None,
                resolved_at_ts=pending_order.observation_completed_at_ts,
                fill_ts=None,
                status="expired_no_next_event",
                price_source=None,
            )
        )

    # -- 5. Aggregate ---------------------------------------------------
    return BacktestResult(
        config=config,
        summary=ctx.equity_builder.summary(),
        cost_summary=ctx.cost_validator.summary(),
        equity_curve=ctx.equity_builder.curve,
        decisions_count=ctx.decisions_count,
        fills_count=ctx.fills_count,
        start_ts=start_ts,
        end_ts=end_ts,
        cost_diagnostics=ctx.cost_validator.diagnostics,
        execution_timeline=tuple(ctx.execution_timeline),
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _build_default_adapter(family: str) -> BaseReplayAdapter:
    """按 family 名返回默认 adapter 实例。

    MVP 只接 ``independent`` — 其他 family 会在 Phase 4 加入。
    """
    if family == "independent":
        return IndependentReplayAdapter()
    raise ValueError(
        f"Unsupported family={family!r}. MVP 仅支持 'independent'；"
        f"其他 family 需要自行注入 adapter 参数。"
    )


def _build_replay_params(
    family: str,
    overrides: dict[str, Any] | None,
) -> ReplayParameterOverrides:
    """从 dict 构造 ``ReplayParameterOverrides``。

    * ``None`` / 空 dict → family-specific 默认
    * 非空 → ``from_dict`` 消化（支持平坦 cost keys）
    """
    if not overrides:
        return ReplayParameterOverrides.for_family(family)
    return ReplayParameterOverrides.from_dict(overrides)


def _execute_pending_order(
    *,
    pending: _PendingOrder,
    execution_bar: ReplayBar,
    bar_duration: timedelta,
    config: BacktestConfig,
    ctx: _RuntimeContext,
    current_state: ReplayState,
) -> ReplayState:
    """在 observation bar 之后的第一根 bar 上解析 pending order。

    IOC/bounded-limit 以 next bar open 作为第一可观察报价事件；post-only
    依赖 next bar 完整 volume，因此只在 next bar close 解析，且绝不使用
    缺失 volume 的虚构 fallback。
    """
    decision = pending.decision
    next_tradable_event_ts = execution_bar.ts
    if config.order_type == "post_only":
        reference_price = execution_bar.close
        resolution_ts = execution_bar.ts + bar_duration
        price_source: Literal["next_bar_open", "next_bar_close"] = (
            "next_bar_close"
        )
        bar_volume = execution_bar.volume or Decimal("0")
        liquidity_source: Literal[
            "observation_bar_volume",
            "next_bar_volume",
        ] = "next_bar_volume"
    else:
        reference_price = execution_bar.open
        resolution_ts = execution_bar.ts
        price_source = "next_bar_open"
        # IOC/bounded-limit 在 next-bar open 解析，但下一根 bar 的完整 volume
        # 此时尚不可观察；因此只能使用下单前已经闭合的 observation bar
        # volume 作为保守流动性代理，绝不偷看未来成交量。
        bar_volume = pending.liquidity_reference_volume
        liquidity_source = "observation_bar_volume"

    fill_result = ctx.fill_simulator.simulate(
        pending.fill_request,
        bar_close_price=reference_price,
        bar_volume=bar_volume,
    )

    if fill_result.filled_qty > pending.fill_request.target_qty:
        raise ValueError(
            "FillSimulator returned filled_qty above target_qty: "
            f"order_id={fill_result.order_id} filled={fill_result.filled_qty} "
            f"target={pending.fill_request.target_qty}"
        )
    filled = fill_result.filled_qty > 0
    committed_state = current_state
    if filled:
        fill = Fill(
            side=fill_result.side,
            filled_qty=fill_result.filled_qty,
            avg_fill_price=fill_result.avg_fill_price,
            fee_notional=fill_result.fee_notional,
            ts_ms=_ts_to_ms(resolution_ts),
        )
        position_snapshot = ctx.position_tracker.apply_fill(fill)
        ctx.fills_count += 1
        committed_state = _synchronize_replay_state_after_fill(
            proposed_state=pending.proposed_state,
            previous_state=current_state,
            net_qty=position_snapshot.net_qty,
            avg_entry_price=position_snapshot.avg_entry_price,
            fill_ts=resolution_ts,
        )
        ctx.current_position_side = committed_state.position_side

        # Cost 校准仅记录本模型明确施加的 fee + fixed slippage。spread、queue
        # position 与 market impact 仍未知，不在 OHLCV harness 中伪造。
        per_decision_cost = float(decision.cost_bps)
        if per_decision_cost == 0.0:
            per_decision_cost = config.assumed_cost_bps
        ctx.cost_validator.record(
            decision_id=decision.ts.isoformat(),
            assumed_cost_bps=per_decision_cost,
            actual_cost_bps=float(
                fill_result.fee_bps + fill_result.slippage_bps
            ),
            assumed_net_edge_bps=float(decision.expected_net_edge_bps),
            actual_fee_bps=float(fill_result.fee_bps),
            actual_slippage_bps=float(fill_result.slippage_bps),
            notes=(
                f"fill_kind={fill_result.fill_kind};"
                f"execution_model={config.execution_model_version};"
                f"fill_model={config.fill_model_version};"
                f"price_source={price_source};"
                f"liquidity_source={liquidity_source}"
            ),
        )

    ctx.execution_timeline.append(
        ExecutionTimingRecord(
            decision_id=_decision_id(decision),
            action=decision.action,
            observation_bar_start_ts=pending.observation_bar_start_ts,
            observation_completed_at_ts=pending.observation_completed_at_ts,
            decision_ts=pending.observation_completed_at_ts,
            submitted_at_ts=pending.observation_completed_at_ts,
            next_tradable_event_ts=next_tradable_event_ts,
            resolved_at_ts=resolution_ts,
            fill_ts=resolution_ts if filled else None,
            status=(
                "filled"
                if fill_result.filled_qty == pending.fill_request.target_qty
                else "partial_fill"
                if filled
                else "no_fill"
            ),
            price_source=price_source,
            liquidity_source=liquidity_source,
        )
    )
    return committed_state


def _synchronize_replay_state_after_fill(
    *,
    proposed_state: ReplayState,
    previous_state: ReplayState,
    net_qty: Decimal,
    avg_entry_price: Decimal,
    fill_ts: datetime,
) -> ReplayState:
    """用 PositionTracker 的实际成交后仓位校正 adapter 提议状态。

    这使 partial fill 不会被误记为完整 open/close，也使未来成交模型扩展
    后仍由实际 fill 而不是 decision target 决定下一轮持仓状态。
    """
    synchronized = copy.deepcopy(proposed_state)
    if net_qty == 0:
        synchronized.position_qty = Decimal("0")
        synchronized.position_side = "flat"
        synchronized.entry_price = None
        synchronized.entry_ts = None
        synchronized.last_close_ts = fill_ts
        return synchronized

    synchronized.position_qty = abs(net_qty)
    synchronized.position_side = "long" if net_qty > 0 else "short"
    synchronized.entry_price = avg_entry_price
    synchronized.entry_ts = previous_state.entry_ts or fill_ts
    synchronized.last_close_ts = previous_state.last_close_ts
    return synchronized


def _record_bar_close_mtm(
    bar: ReplayBar,
    *,
    bar_duration: timedelta,
    ctx: _RuntimeContext,
) -> None:
    """以 bar end 作为 close MtM 时间，不能再用 bar start 冒充。"""
    snapshot = ctx.position_tracker.mark_to_market(
        bar.close,
        _ts_to_ms(bar.ts + bar_duration),
    )
    ctx.equity_builder.record(snapshot)


def _resolve_fill_side(
    decision: ReplayDecision,
    current_side: Literal["flat", "long", "short"],
) -> Literal["buy", "sell"]:
    """把 ReplayDecision 的抽象动作映射到 fill side。

    adapter 的 ``delta_position_qty`` 总是 ±1 / 0（见
    ``IndependentReplayAdapter._compute_position_delta``），不直接携带方向。
    我们按以下规则派生 buy/sell：

    * ``action == "open"`` → long_score >= short_score ? buy : sell
    * ``action == "close"`` → 根据当前持仓方向取反 (long→sell, short→buy)
    * 其他（保守退化）→ 按 delta 正负（正为 buy，负为 sell）
    """
    if decision.action == "open":
        return "buy" if decision.long_score >= decision.short_score else "sell"
    if decision.action == "close":
        if current_side == "long":
            return "sell"
        if current_side == "short":
            return "buy"
        # flat + close 属异常组合，退化到 delta 符号
    return "buy" if decision.delta_position_qty >= 0 else "sell"


def _build_fill_request(
    *,
    decision: ReplayDecision,
    side: Literal["buy", "sell"],
    delta: Decimal,
    order_type: Literal["ioc", "post_only", "bounded_limit"],
    submitted_at_ts: datetime,
) -> FillRequest:
    """构造一笔 FillRequest。

    ``order_id`` 以 observation bar identity 和 action 为基，保证复现；
    ``submitted_at_ts`` 则是完整 observation bar 可见之后的 bar end。
    """
    order_id = _decision_id(decision)
    target_qty = abs(delta)
    return FillRequest(
        order_id=order_id,
        side=side,
        order_type=order_type,
        target_qty=target_qty,
        submitted_at_ts=_ts_to_ms(submitted_at_ts),
    )


def _ts_to_ms(ts: datetime) -> int:
    """datetime → epoch ms。

    对 naive datetime，我们显式附加 UTC，避免受宿主本地时区影响。
    """
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return int(ts.timestamp() * 1000)


def _decision_id(decision: ReplayDecision) -> str:
    """稳定的 observation decision identity。"""
    return f"{decision.ts.isoformat()}_{decision.action}"


def _parse_timeframe(timeframe: str) -> timedelta:
    """把 replay 固定周期解析为 timedelta。

    Gold replay 当前只映射 minute/hour 周期，但支持 d 便于 DTO 防御性复用；
    calendar month 等非固定周期必须由更明确的事件时间契约实现。
    """
    match = re.fullmatch(r"([1-9][0-9]*)([mhd])", timeframe.strip().lower())
    if match is None:
        raise ValueError(
            "Unsupported replay timeframe for causal timing: "
            f"{timeframe!r}; expected <positive integer>[m|h|d]"
        )
    amount = int(match.group(1))
    unit = match.group(2)
    if unit == "m":
        return timedelta(minutes=amount)
    if unit == "h":
        return timedelta(hours=amount)
    return timedelta(days=amount)


def _validate_bar_sequence(
    bars: list[ReplayBar],
    *,
    bar_duration: timedelta,
) -> None:
    """在任何 adapter 观察前验证完整、严格递增且不重叠的 bars。"""
    previous: ReplayBar | None = None
    for index, bar in enumerate(bars):
        if not bar.is_closed:
            raise ValueError(
                "Replay lookahead guard rejected unfinished bar: "
                f"bar_index={index} ts={bar.ts.isoformat()}"
            )
        if previous is not None:
            try:
                strictly_increasing = bar.ts > previous.ts
                non_overlapping = bar.ts >= previous.ts + bar_duration
            except TypeError as exc:
                raise ValueError(
                    "Replay bars must use consistent timezone awareness: "
                    f"previous_ts={previous.ts!r} current_ts={bar.ts!r}"
                ) from exc
            if not strictly_increasing:
                raise ValueError(
                    "Replay bars must be strictly increasing: "
                    f"bar_index={index} previous_ts={previous.ts.isoformat()} "
                    f"current_ts={bar.ts.isoformat()}"
                )
            if not non_overlapping:
                raise ValueError(
                    "Replay bars overlap the configured timeframe: "
                    f"bar_index={index} previous_ts={previous.ts.isoformat()} "
                    f"current_ts={bar.ts.isoformat()} "
                    f"timeframe_duration={bar_duration}"
                )
        previous = bar


__all__ = [
    "BacktestConfig",
    "BacktestResult",
    "ExecutionTimingRecord",
    "run_backtest",
]
