"""Backtest integration harness (Phase 3 MVP).

目标
----
把 Phase 1/2 产出的纯计算组件（``FillSimulator`` / ``PositionTracker`` /
``EquityBuilder`` / ``CostValidator``）和 replay core 的 adapter + bar loader
串成一条 end-to-end pipeline:

    load_gold_bars → adapter.evaluate_bar → map to FillRequest →
        FillSimulator.simulate → PositionTracker.apply_fill →
            EquityBuilder.record (bar-close MtM) → CostValidator.record

Boundary
--------
* 只读取 Gold replay bars，只调用 Phase 1/2 公开接口
* 绝不修改 live path（``aats/services/`` / ``configs/``）
* 绝不修改 replay core（``aats/data_platform/replay/core/``）
* 无额外 DB 写入、无消息总线副作用；日志仅通过 stdlib ``logging``

本文件对调用方只暴露 3 个 public symbol：``BacktestConfig``、
``BacktestResult``、``run_backtest``。CLI 层（``aats.cli``）直接消费。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy.orm import Session

from aats.data_platform.replay.adapters.base_adapter import BaseReplayAdapter
from aats.data_platform.replay.adapters.independent_adapter import (
    IndependentReplayAdapter,
)
from aats.data_platform.replay.backtest.cost_validator import (
    CostValidationSummary,
    CostValidator,
)
from aats.data_platform.replay.backtest.equity_builder import (
    BacktestSummary,
    EquityBuilder,
    EquityPoint,
)
from aats.data_platform.replay.backtest.fill_simulator import (
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
)
from aats.data_platform.replay.core.replay_runner import load_gold_bars

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config / Result DTOs
# ---------------------------------------------------------------------------


_FALLBACK_BAR_VOLUME = Decimal("1000")
"""`post_only` 分支在 bar.volume 为 None 时退化到这个值作为分母。

MVP 里我们优先跑 IOC（完整成交），该 fallback 仅服务 post_only 研究场景。
"""

_DELTA_EPSILON = Decimal("0.0000001")
"""|delta| 小于该值视为 no-op（不生成 FillRequest）。

adapter 目前只会输出 -1 / 0 / +1 三档，epsilon 只作防御性判断。
"""


@dataclass(frozen=True)
class BacktestConfig:
    """Backtest 运行参数。

    所有字段都有默认值，调用方仅需覆盖关心的维度。``order_type`` 对应
    :class:`FillSimulator` 的 3 个分支；MVP 默认 ``"ioc"`` 以保证 100%
    成交，post_only / bounded_limit 供后续敏感性分析用。
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
    # Cost validator 的决策侧假设 cost（用户自行校准 / 对齐生产端）
    assumed_cost_bps: float = 6.0


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

    流程（对齐任务单）::

        1. load Gold bars
        2. adapter.evaluate_bar  -> ReplayDecision
        3. map decision.delta    -> FillRequest
        4. fill_simulator        -> FillResult
        5. position_tracker      -> PositionSnapshot
        6. tracker.mark_to_market(bar.close)  (bar-close MtM)
        7. equity_builder.record
        8. cost_validator.record

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
        "run_backtest: loaded %d Gold bars (symbol=%s timeframe=%s dv=%s)",
        len(bars),
        config.symbol,
        config.timeframe,
        config.dataset_version,
    )

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
    for bar_index, bar in enumerate(bars):
        state.bar_index = bar_index
        bar_ctx = ReplayBarContext(
            bar=bar,
            bar_index=bar_index,
            state=state,
            params=params,
            family=adapter.family_name,
            symbol=config.symbol,
            timeframe=config.timeframe,
            dataset_version=config.dataset_version,
        )
        decision: ReplayDecision = adapter.evaluate_bar(bar_ctx)
        ctx.decisions_count += 1
        # 对齐 replay_runner：维护 runner-side score history
        ctx.score_history.append(max(decision.long_score, decision.short_score))
        state.score_history = ctx.score_history

        _process_decision(decision, bar, config, ctx)

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


def _process_decision(
    decision: ReplayDecision,
    bar: ReplayBar,
    config: BacktestConfig,
    ctx: _RuntimeContext,
) -> None:
    """单根 bar 的 "decision → fill → position → equity → cost" 小闭环。"""
    # a) 是否要下单？
    delta = decision.delta_position_qty
    has_trade = abs(delta) >= _DELTA_EPSILON

    if has_trade:
        side = _resolve_fill_side(decision, ctx.current_position_side)
        fill_request = _build_fill_request(
            decision=decision,
            side=side,
            delta=delta,
            order_type=config.order_type,
        )
        bar_volume = bar.volume if bar.volume is not None else _FALLBACK_BAR_VOLUME
        fill_result = ctx.fill_simulator.simulate(
            fill_request,
            bar_close_price=bar.close,
            bar_volume=bar_volume,
        )

        if fill_result.filled_qty > 0:
            ts_ms = _ts_to_ms(bar.ts)
            fill = Fill(
                side=fill_result.side,
                filled_qty=fill_result.filled_qty,
                avg_fill_price=fill_result.avg_fill_price,
                fee_notional=fill_result.fee_notional,
                ts_ms=ts_ms,
            )
            ctx.position_tracker.apply_fill(fill)
            ctx.fills_count += 1
            ctx.current_position_side = _update_side_after_fill(
                previous_side=ctx.current_position_side,
                action=decision.action,
                long_dominant=decision.long_score >= decision.short_score,
            )

            # Cost 校准：实际 cost ≈ fill fee + 模拟 slippage (bps)
            # 对 MVP，我们只使用 fee_bps 作 actual_cost proxy；更精细的
            # slippage 归因留给 Phase 4（需要 orderbook 数据）。
            ctx.cost_validator.record(
                decision_id=decision.ts.isoformat(),
                assumed_cost_bps=config.assumed_cost_bps,
                actual_cost_bps=float(fill_result.fee_bps),
                assumed_net_edge_bps=float(decision.expected_net_edge_bps),
                notes=f"fill_kind={fill_result.fill_kind}",
            )

    # b) bar-close MtM（有无成交都要）
    ts_ms = _ts_to_ms(bar.ts)
    snapshot = ctx.position_tracker.mark_to_market(bar.close, ts_ms)
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
) -> FillRequest:
    """构造一笔 FillRequest。

    ``order_id`` 以 ``{ts}_{action}`` 为基，保证同一 bar 同一 action 复现。
    """
    order_id = f"{decision.ts.isoformat()}_{decision.action}"
    target_qty = abs(delta)
    return FillRequest(
        order_id=order_id,
        side=side,
        order_type=order_type,
        target_qty=target_qty,
        submitted_at_ts=_ts_to_ms(decision.ts),
    )


def _update_side_after_fill(
    *,
    previous_side: Literal["flat", "long", "short"],
    action: str,
    long_dominant: bool,
) -> Literal["flat", "long", "short"]:
    """在 fill 成功后更新 harness 自持的仓位方向。"""
    if action == "open":
        return "long" if long_dominant else "short"
    if action == "close":
        return "flat"
    return previous_side


def _ts_to_ms(ts: datetime) -> int:
    """datetime → epoch ms。

    对 naive datetime，我们假设其已经是 UTC（Gold 表的约定）。
    """
    return int(ts.timestamp() * 1000)


__all__ = [
    "BacktestConfig",
    "BacktestResult",
    "run_backtest",
]
