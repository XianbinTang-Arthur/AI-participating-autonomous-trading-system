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
import math
from dataclasses import asdict, dataclass, field
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
    REPLAY_RISK_METRIC_POLICY_ID,
    BacktestSummary,
    EquityBuilder,
    EquityPoint,
    recompute_equity_curve_metrics,
)
from aats.data_platform.replay.backtest.fill_simulator import (
    FILL_MODEL_VERSION,
    FillRequest,
    FillResult,
    FillSimulator,
)
from aats.data_platform.replay.backtest.numeric import (
    finite_float,
    validate_finite_numbers,
)
from aats.data_platform.replay.backtest.position_tracker import (
    Fill,
    PositionTracker,
)
from aats.data_platform.replay.core.replay_context import (
    REPLAY_EXECUTION_STYLES,
    ReplayBar,
    ReplayBarContext,
    ReplayDecision,
    ReplayParameterOverrides,
    ReplayState,
    canonicalize_replay_timeframe,
    parse_replay_timeframe as _parse_timeframe,
)
from aats.data_platform.replay.core.replay_runner import load_gold_bars
from aats.domain.instrument_contract import InstrumentContract

log = logging.getLogger(__name__)

BACKTEST_ARTIFACT_SCHEMA_VERSION = "backtest-run/v2"
_EDGE_IDENTITY_TOLERANCE_BPS = Decimal("0.0005")


# ---------------------------------------------------------------------------
# Config / Result DTOs
# ---------------------------------------------------------------------------


_EXECUTION_MODEL_VERSION = "next_bar_event_v2"
_REPLAY_COST_OVERRIDE_KEYS = frozenset(
    {
        "taker_fee_bps",
        "slippage_bps",
        "maker_fee_bps",
        "execution_style",
        "passive_bias",
        "maker_taker_bias",
    }
)
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
    fill_side: Literal["buy", "sell"] | None = None
    decision_intent_exchange_quantity: Decimal | None = None
    requested_exchange_quantity: Decimal | None = None
    liquidity_reference_quantity: Decimal | None = None
    max_volume_participation: Decimal | None = None
    reference_price: Decimal | None = None
    post_fill_position_quantity: Decimal | None = None


@dataclass(frozen=True)
class BacktestConfig:
    """Backtest 运行参数。

    默认值只用于加载/迁移兼容；执行前必须显式提供与 ``symbol`` 一致的
    ``instrument_contract``。``order_type`` 对应 :class:`FillSimulator` 的
    3 个分支，均受 OHLCV participation cap，不能保证全量成交。
    """

    symbol: str = "BTC-USDT-SWAP"
    instrument_contract: InstrumentContract | None = None
    timeframe: str = "1h"
    dataset_version: str = "v1.0"
    family: str = "independent"
    order_type: Literal["ioc", "post_only", "bounded_limit"] = "ioc"
    # FillSimulator 参数（透传）
    maker_fee_bps: float = 2.0
    taker_fee_bps: float = 5.0
    ioc_slippage_bps: float = 1.0
    max_volume_participation: Decimal = Decimal("0.01")
    # 仅保留旧调用方的显式失败迁移路径。v2 运行要求每个
    # ReplayDecision 携带 cost_bps；非 None 会在任何 I/O 前被拒绝。
    assumed_cost_bps: float | None = None
    # 固定的安全时间模型；不提供 same-bar 兼容开关。
    execution_model_version: Literal["next_bar_event_v2"] = _EXECUTION_MODEL_VERSION
    fill_model_version: Literal["ohlcv_participation_cap_contract_v3"] = (
        FILL_MODEL_VERSION
    )
    # Appended for positional compatibility. This is an explicit venue-model
    # assumption, not an inferred exchange rule.
    spot_buy_fee_asset: Literal["base", "quote"] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "timeframe",
            canonicalize_replay_timeframe(self.timeframe),
        )
        # These values are persisted into the versioned run manifest and its
        # semantic fingerprint.  Accept only the documented numeric boundary
        # and canonicalize equivalent int/float spellings before either can be
        # observed; strings and booleans must not create distinct artifacts for
        # the same apparent economics.
        for field_name in (
            "maker_fee_bps",
            "taker_fee_bps",
            "ioc_slippage_bps",
        ):
            raw_value = getattr(self, field_name)
            if isinstance(raw_value, bool) or not isinstance(
                raw_value,
                (int, float),
            ):
                raise ValueError(f"{field_name} must be int or float")
            value = float(raw_value)
            if not math.isfinite(value):
                raise ValueError(f"{field_name} must be finite")
            if value == 0.0:
                value = 0.0
            object.__setattr__(self, field_name, value)


@dataclass(frozen=True)
class BacktestResult:
    """一次 backtest 运行的聚合结果。

    * ``summary`` / ``cost_summary`` 已是 frozen dataclass，可直接 JSON 化
    * ``equity_curve`` 以 tuple 形式暴露，保证外部不可变
    """

    config: BacktestConfig
    resolved_parameters: ReplayParameterOverrides
    adapter_identity: str
    adapter_algorithm_version: str
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
    cadence_gap_count: int = 0


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

    (
        instrument_contract,
        bar_duration,
        resolved_adapter,
        params,
    ) = _prepare_backtest_request(
        config=config,
        start_ts=start_ts,
        end_ts=end_ts,
        parameter_overrides=parameter_overrides,
        adapter=adapter,
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

    cadence_gap_count = _validate_bar_sequence(
        bars,
        bar_duration=bar_duration,
        instrument_contract=instrument_contract,
        start_ts=start_ts,
        end_ts=end_ts,
    )

    # -- 2. Adapter + params -------------------------------------------
    adapter = resolved_adapter

    # -- 3. Runtime context --------------------------------------------
    ctx = _RuntimeContext(
        fill_simulator=FillSimulator(
            instrument_contract=instrument_contract,
            maker_fee_bps=config.maker_fee_bps,
            taker_fee_bps=config.taker_fee_bps,
            ioc_slippage_bps=config.ioc_slippage_bps,
            max_volume_participation=config.max_volume_participation,
            spot_buy_fee_asset=config.spot_buy_fee_asset,
        ),
        position_tracker=PositionTracker(
            instrument_contract=instrument_contract,
        ),
        equity_builder=EquityBuilder(
            instrument_contract=instrument_contract,
        ),
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
        observation_completed_at = bar.ts + bar_duration
        bar_ctx = ReplayBarContext(
            bar=bar,
            bar_index=bar_index,
            state=proposed_state,
            params=params,
            family=adapter.family_name,
            symbol=config.symbol,
            timeframe=config.timeframe,
            dataset_version=config.dataset_version,
            observation_completed_at_ts=observation_completed_at,
            decision_ts=observation_completed_at,
        )
        decision: ReplayDecision = adapter.evaluate_bar(bar_ctx)
        _validate_replay_decision(
            decision,
            adapter=adapter,
            config=config,
            contract=instrument_contract,
            current_position_qty=(
                ctx.position_tracker.snapshot.net_qty.copy_abs()
            ),
        )
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

        delta = decision.delta_position_qty
        # 非零 delta 已由 InstrumentContract 校验 lot/min；不能再用与产品
        # 单位无关的固定 epsilon 吞掉合法小额订单或 partial-close 余量。
        if delta != 0:
            side = _resolve_fill_side(decision, ctx.current_position_side)
            fill_request = _build_fill_request(
                decision=decision,
                side=side,
                delta=delta,
                contract=instrument_contract,
                order_type=config.order_type,
                submitted_at_ts=observation_completed_at,
            )
            if fill_request is None:
                # A close intent can leave fee-created SPOT dust below the
                # contract minimum. Preserve exact inventory/value and record
                # that no tradable order exists; never quantize dust away.
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
                        fill_side=side,
                        decision_intent_exchange_quantity=delta.copy_abs(),
                        requested_exchange_quantity=Decimal("0"),
                        post_fill_position_quantity=(
                            ctx.position_tracker.snapshot.net_qty.copy_abs()
                        ),
                    )
                )
            else:
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
                fill_side=pending_order.fill_request.side,
                decision_intent_exchange_quantity=(
                    pending_order.decision.delta_position_qty.copy_abs()
                ),
                requested_exchange_quantity=pending_order.fill_request.target_qty,
                liquidity_reference_quantity=(
                    pending_order.liquidity_reference_volume
                ),
                max_volume_participation=config.max_volume_participation,
            )
        )

    # -- 5. Aggregate ---------------------------------------------------
    return BacktestResult(
        config=config,
        resolved_parameters=params,
        adapter_identity=(
            f"{adapter.__class__.__module__}.{adapter.__class__.__qualname__}"
        ),
        adapter_algorithm_version=adapter.algorithm_version,
        summary=ctx.equity_builder.summary(),
        cost_summary=ctx.cost_validator.summary(),
        equity_curve=ctx.equity_builder.curve,
        decisions_count=ctx.decisions_count,
        fills_count=ctx.fills_count,
        start_ts=start_ts,
        end_ts=end_ts,
        cost_diagnostics=ctx.cost_validator.diagnostics,
        execution_timeline=tuple(ctx.execution_timeline),
        cadence_gap_count=cadence_gap_count,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def validate_backtest_request(
    *,
    config: BacktestConfig,
    start_ts: datetime,
    end_ts: datetime,
    parameter_overrides: dict[str, Any] | None = None,
    adapter: BaseReplayAdapter | None = None,
) -> None:
    """Validate every local replay input before filesystem or database access."""

    _prepare_backtest_request(
        config=config,
        start_ts=start_ts,
        end_ts=end_ts,
        parameter_overrides=parameter_overrides,
        adapter=adapter,
    )


def _prepare_backtest_request(
    *,
    config: BacktestConfig,
    start_ts: datetime,
    end_ts: datetime,
    parameter_overrides: dict[str, Any] | None,
    adapter: BaseReplayAdapter | None,
) -> tuple[
    InstrumentContract,
    timedelta,
    BaseReplayAdapter,
    ReplayParameterOverrides,
]:
    if not isinstance(config, BacktestConfig):
        raise ValueError("backtest_config_type_invalid")
    if not isinstance(start_ts, datetime) or not isinstance(end_ts, datetime):
        raise ValueError("replay_window_datetime_required")
    for name, value in (("start", start_ts), ("end", end_ts)):
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError(f"replay_{name}_must_be_utc")
    if end_ts <= start_ts:
        raise ValueError("replay_end_must_be_after_start")
    if (
        not isinstance(config.dataset_version, str)
        or not config.dataset_version.strip()
    ):
        raise ValueError("replay_dataset_version_required")
    if not isinstance(config.family, str) or not config.family.strip():
        raise ValueError("replay_family_required")
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
    if config.order_type not in {"ioc", "post_only", "bounded_limit"}:
        raise ValueError("unsupported_replay_order_type")
    if not isinstance(config.max_volume_participation, Decimal) or not (
        config.max_volume_participation.is_finite()
        and Decimal("0") < config.max_volume_participation <= Decimal("1")
    ):
        raise ValueError(
            "max_volume_participation must be in (0, 1]: "
            f"{config.max_volume_participation!r}"
        )
    if config.assumed_cost_bps is not None:
        raise ValueError(
            "legacy_assumed_cost_bps_unsupported_use_param_cost_config"
        )

    contract = _resolve_instrument_contract(config)
    if contract.contract_type != "spot":
        raise ValueError("legacy_derivative_replay_contract_lineage_required")
    if contract.instrument_type != "SPOT":
        raise ValueError("legacy_margin_replay_borrow_model_required")
    if config.spot_buy_fee_asset not in {"base", "quote"}:
        raise ValueError("spot_buy_fee_asset_required")

    bar_duration = _parse_timeframe(config.timeframe)
    resolved_adapter = adapter or _build_default_adapter(config.family)
    if resolved_adapter.family_name != config.family:
        raise ValueError("replay_adapter_family_mismatch")
    if (
        not isinstance(resolved_adapter.algorithm_version, str)
        or not resolved_adapter.algorithm_version.strip()
    ):
        raise ValueError("replay_adapter_algorithm_version_required")
    _validate_parameter_override_contract(
        parameter_overrides,
        adapter=resolved_adapter,
    )
    params = _build_replay_params(config.family, parameter_overrides)
    if type(params.extra) is not dict:
        raise ValueError("replay_parameter_extra_must_be_dict")
    if params.extra:
        unknown = ",".join(sorted(params.extra))
        raise ValueError(f"unknown_replay_parameter_keys:{unknown}")
    validate_finite_numbers(
        asdict(params),
        reason="replay_parameters_non_finite",
    )
    _validate_replay_cost_contract(params)
    if params.strategy_short_bias_enabled:
        raise ValueError("spot_replay_short_bias_must_be_disabled")
    inactive_short_keys = set(parameter_overrides or ()) & {
        "short_entry_threshold",
        "short_close_threshold",
    }
    if inactive_short_keys:
        raise ValueError(
            "inactive_spot_short_parameter_keys:"
            + ",".join(sorted(inactive_short_keys))
        )

    # Constructor validation is deliberately performed here so invalid fee,
    # slippage or accounting configuration cannot create an output directory
    # or open a database connection first.
    FillSimulator(
        instrument_contract=contract,
        maker_fee_bps=config.maker_fee_bps,
        taker_fee_bps=config.taker_fee_bps,
        ioc_slippage_bps=config.ioc_slippage_bps,
        max_volume_participation=config.max_volume_participation,
        spot_buy_fee_asset=config.spot_buy_fee_asset,
    )
    PositionTracker(instrument_contract=contract)
    EquityBuilder(instrument_contract=contract)
    return contract, bar_duration, resolved_adapter, params


def validate_backtest_result_units(
    result: BacktestResult,
    *,
    require_complete_artifact: bool = False,
) -> InstrumentContract:
    """Validate unit lineage and the internal contract of one replay result.

    ``require_complete_artifact`` is reserved for versioned artifact writers.  It
    additionally requires the causal execution timeline and explicit per-fill
    attribution introduced with ``backtest-run/v2``.  The default retains a
    read-only migration path for legacy in-memory scorecard inputs, but still
    rejects contradictory counts, windows, summaries and model versions.
    """

    if not isinstance(result, BacktestResult):
        raise ValueError("backtest_result_type_invalid")
    if not isinstance(result.config, BacktestConfig):
        raise ValueError("backtest_config_type_invalid")

    validate_finite_numbers(
        asdict(result),
        reason="backtest_artifact_non_finite",
    )
    config = result.config
    contract = _resolve_instrument_contract(config)
    _validate_result_config(config, contract=contract)
    _validate_result_window(result)
    if not isinstance(result.resolved_parameters, ReplayParameterOverrides):
        raise ValueError("backtest_resolved_parameters_required")
    validate_finite_numbers(
        asdict(result.resolved_parameters),
        reason="backtest_resolved_parameters_non_finite",
    )
    if (
        contract.instrument_type == "SPOT"
        and result.resolved_parameters.strategy_short_bias_enabled
    ):
        raise ValueError("spot_replay_short_bias_must_be_disabled")
    if (
        type(result.resolved_parameters.extra) is not dict
        or result.resolved_parameters.extra
    ):
        raise ValueError("backtest_resolved_parameters_extra_must_be_empty")
    _validate_replay_cost_contract(result.resolved_parameters)
    if not isinstance(result.adapter_identity, str) or not result.adapter_identity.strip():
        raise ValueError("backtest_adapter_identity_required")
    if (
        not isinstance(result.adapter_algorithm_version, str)
        or not result.adapter_algorithm_version.strip()
    ):
        raise ValueError("backtest_adapter_algorithm_version_required")
    if (
        type(result.cadence_gap_count) is not int
        or result.cadence_gap_count < 0
    ):
        raise ValueError("backtest_cadence_gap_count_invalid")
    for name in ("decisions_count", "fills_count"):
        value = getattr(result, name)
        if type(value) is not int or value < 0:
            raise ValueError(f"backtest_{name}_invalid")
    if result.fills_count > result.decisions_count:
        raise ValueError("backtest_fill_count_exceeds_decision_count")

    _validate_result_summary_and_curve(
        result,
        contract=contract,
        require_complete_artifact=require_complete_artifact,
    )
    _validate_result_timeline(
        result,
        require_complete_artifact=require_complete_artifact,
    )
    _validate_result_cost_contract(
        result,
        contract=contract,
        require_complete_artifact=require_complete_artifact,
    )

    expected = contract.settle_currency
    if result.summary.settlement_currency != expected:
        raise ValueError("backtest_summary_settlement_currency_mismatch")
    if result.summary.instrument_symbol != contract.symbol:
        raise ValueError("backtest_summary_instrument_symbol_mismatch")
    if result.summary.instrument_contract_fingerprint != contract.fingerprint:
        raise ValueError("backtest_summary_contract_fingerprint_mismatch")
    if result.summary.risk_metric_policy_id != REPLAY_RISK_METRIC_POLICY_ID:
        raise ValueError("backtest_summary_risk_metric_policy_mismatch")
    for point in result.equity_curve:
        if point.settlement_currency != expected:
            raise ValueError("backtest_equity_settlement_currency_mismatch")
        if point.instrument_symbol != contract.symbol:
            raise ValueError("backtest_equity_instrument_symbol_mismatch")
        if point.instrument_contract_fingerprint != contract.fingerprint:
            raise ValueError("backtest_equity_contract_fingerprint_mismatch")
    return contract


def _validate_result_config(
    config: BacktestConfig,
    *,
    contract: InstrumentContract,
) -> None:
    if config.execution_model_version != _EXECUTION_MODEL_VERSION:
        raise ValueError("backtest_execution_model_version_unsupported")
    if config.fill_model_version != FILL_MODEL_VERSION:
        raise ValueError("backtest_fill_model_version_unsupported")
    if config.order_type not in {"ioc", "post_only", "bounded_limit"}:
        raise ValueError("backtest_order_type_unsupported")
    if not isinstance(config.dataset_version, str) or not config.dataset_version.strip():
        raise ValueError("backtest_dataset_version_invalid")
    if not isinstance(config.family, str) or not config.family.strip():
        raise ValueError("backtest_family_invalid")
    _parse_timeframe(config.timeframe)
    if contract.contract_type != "spot":
        raise ValueError("legacy_derivative_replay_contract_lineage_required")
    if contract.instrument_type != "SPOT":
        raise ValueError("legacy_margin_replay_borrow_model_required")
    if config.spot_buy_fee_asset not in {"base", "quote"}:
        raise ValueError("spot_buy_fee_asset_required")
    if config.assumed_cost_bps is not None:
        raise ValueError(
            "legacy_assumed_cost_bps_unsupported_use_param_cost_config"
        )
    FillSimulator(
        instrument_contract=contract,
        maker_fee_bps=config.maker_fee_bps,
        taker_fee_bps=config.taker_fee_bps,
        ioc_slippage_bps=config.ioc_slippage_bps,
        max_volume_participation=config.max_volume_participation,
        spot_buy_fee_asset=config.spot_buy_fee_asset,
    )


def _validate_result_window(result: BacktestResult) -> None:
    for name in ("start_ts", "end_ts"):
        value = getattr(result, name)
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() != timedelta(0)
        ):
            raise ValueError(f"backtest_{name}_must_be_utc")
    if result.end_ts <= result.start_ts:
        raise ValueError("backtest_result_window_invalid")


def _validate_result_summary_and_curve(
    result: BacktestResult,
    *,
    contract: InstrumentContract,
    require_complete_artifact: bool,
) -> None:
    summary = result.summary
    if not isinstance(summary, BacktestSummary):
        raise ValueError("backtest_summary_type_invalid")
    for name in ("fill_count", "bar_count", "start_ts_ms", "end_ts_ms"):
        value = getattr(summary, name)
        if type(value) is not int or value < 0:
            raise ValueError(f"backtest_summary_{name}_invalid")
    for name in (
        "initial_equity",
        "final_equity",
        "cumulative_pnl",
        "max_drawdown_bps",
        "fee_total",
    ):
        value = getattr(summary, name)
        if not isinstance(value, Decimal) or not value.is_finite():
            raise ValueError(f"backtest_summary_{name}_invalid")
    if type(summary.sharpe_ratio) is not float or not math.isfinite(
        summary.sharpe_ratio
    ):
        raise ValueError("backtest_summary_sharpe_ratio_invalid")
    if summary.initial_equity != 0:
        raise ValueError("backtest_summary_initial_equity_unsupported")
    if summary.fill_count != result.fills_count:
        raise ValueError("backtest_summary_fill_count_mismatch")
    if summary.bar_count != len(result.equity_curve):
        raise ValueError("backtest_summary_bar_count_mismatch")
    if result.decisions_count != len(result.equity_curve):
        raise ValueError("backtest_decision_curve_count_mismatch")

    timeframe_ms = int(_parse_timeframe(result.config.timeframe).total_seconds() * 1000)
    start_ms = int(result.start_ts.timestamp() * 1000)
    end_ms = int(result.end_ts.timestamp() * 1000)
    previous_ts: int | None = None
    actual_gap_count = 0
    for index, point in enumerate(result.equity_curve):
        if not isinstance(point, EquityPoint):
            raise ValueError("backtest_equity_point_type_invalid")
        if type(point.ts_ms) is not int:
            raise ValueError("backtest_equity_timestamp_invalid")
        for name in (
            "equity",
            "cumulative_pnl",
            "drawdown_bps",
            "daily_return_bps",
        ):
            value = getattr(point, name)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError(f"backtest_equity_{name}_invalid")
        inside_window = (
            start_ms < point.ts_ms <= end_ms
            if require_complete_artifact
            else start_ms <= point.ts_ms <= end_ms
        )
        if not inside_window:
            raise ValueError("backtest_equity_timestamp_outside_window")
        if previous_ts is not None:
            if point.ts_ms <= previous_ts:
                raise ValueError("backtest_equity_timestamp_not_strictly_increasing")
            if point.ts_ms - previous_ts != timeframe_ms:
                actual_gap_count += 1
        previous_ts = point.ts_ms
        if point.equity != point.cumulative_pnl:
            raise ValueError("backtest_equity_cumulative_pnl_mismatch")
        if require_complete_artifact:
            for name in (
                "realized_pnl",
                "unrealized_pnl",
                "net_qty",
                "avg_entry_price",
                "mark_price",
                "accumulated_fees",
            ):
                value = getattr(point, name)
                if not isinstance(value, Decimal) or not value.is_finite():
                    raise ValueError("backtest_equity_position_ledger_incomplete")
            if type(point.fill_count) is not int or point.fill_count < 0:
                raise ValueError("backtest_equity_position_ledger_incomplete")
            assert point.mark_price is not None
            try:
                contract.validate_exchange_price(point.mark_price)
            except ValueError as exc:
                raise ValueError("backtest_equity_mark_price_tick_misaligned") from exc
            expected_net_equity = contract.add_settlement_amounts(
                point.realized_pnl,
                point.unrealized_pnl,
                point.accumulated_fees.copy_negate(),
            )
            if point.equity != expected_net_equity:
                raise ValueError("backtest_equity_net_fee_identity_mismatch")
        if point.drawdown_bps < 0:
            raise ValueError("backtest_equity_drawdown_negative")
        if index == 0 and point.daily_return_bps != 0:
            raise ValueError("backtest_initial_daily_return_must_be_zero")

    if actual_gap_count != result.cadence_gap_count:
        raise ValueError("backtest_cadence_gap_count_mismatch")
    if require_complete_artifact:
        expected_metrics, expected_sharpe = recompute_equity_curve_metrics(
            result.equity_curve,
            instrument_contract=contract,
        )
        for point, (expected_drawdown, expected_daily) in zip(
            result.equity_curve,
            expected_metrics,
            strict=True,
        ):
            if point.drawdown_bps != expected_drawdown:
                raise ValueError("backtest_equity_drawdown_recalculation_mismatch")
            if point.daily_return_bps != expected_daily:
                raise ValueError(
                    "backtest_equity_daily_return_recalculation_mismatch"
                )
        if summary.sharpe_ratio != expected_sharpe:
            raise ValueError("backtest_summary_sharpe_recalculation_mismatch")
    if result.equity_curve:
        first = result.equity_curve[0]
        last = result.equity_curve[-1]
        if summary.start_ts_ms != first.ts_ms or summary.end_ts_ms != last.ts_ms:
            raise ValueError("backtest_summary_timestamp_mismatch")
        if summary.final_equity != last.equity:
            raise ValueError("backtest_summary_final_equity_mismatch")
        if summary.cumulative_pnl != last.cumulative_pnl:
            raise ValueError("backtest_summary_cumulative_pnl_mismatch")
        if summary.max_drawdown_bps != max(
            point.drawdown_bps for point in result.equity_curve
        ):
            raise ValueError("backtest_summary_max_drawdown_mismatch")
    elif any(
        value != 0
        for value in (
            summary.start_ts_ms,
            summary.end_ts_ms,
            summary.final_equity,
            summary.cumulative_pnl,
            summary.max_drawdown_bps,
            summary.sharpe_ratio,
            summary.fill_count,
            summary.fee_total,
        )
    ):
        raise ValueError("backtest_empty_summary_must_be_zero")

    if summary.settlement_currency != contract.settle_currency:
        raise ValueError("backtest_summary_settlement_currency_mismatch")


def _validate_result_cost_contract(
    result: BacktestResult,
    *,
    contract: InstrumentContract,
    require_complete_artifact: bool,
) -> None:
    diagnostics = result.cost_diagnostics
    summary = result.cost_summary
    if not isinstance(summary, CostValidationSummary):
        raise ValueError("backtest_cost_summary_type_invalid")
    for name in (
        "total_decisions",
        "decisions_with_fills",
        "flipped_negative_count",
        "flipped_positive_count",
        "stable_sign_count",
    ):
        value = getattr(summary, name)
        if type(value) is not int or value < 0:
            raise ValueError(f"backtest_cost_summary_{name}_invalid")
    for name in (
        "avg_cost_diff_bps",
        "max_cost_diff_bps",
        "p50_cost_diff_bps",
        "p95_cost_diff_bps",
    ):
        if type(getattr(summary, name)) is not float or not math.isfinite(
            getattr(summary, name)
        ):
            raise ValueError(f"backtest_cost_summary_{name}_invalid")

    filled_timeline: dict[str, ExecutionTimingRecord] = {}
    if require_complete_artifact:
        if len(diagnostics) != result.fills_count:
            raise ValueError("backtest_cost_diagnostic_count_mismatch")
        if summary.total_decisions != len(diagnostics):
            raise ValueError("backtest_cost_summary_total_mismatch")
        if summary.decisions_with_fills != len(diagnostics):
            raise ValueError("backtest_cost_summary_fill_count_mismatch")
        if (
            summary.flipped_negative_count
            + summary.flipped_positive_count
            + summary.stable_sign_count
            != len(diagnostics)
        ):
            raise ValueError("backtest_cost_summary_sign_partition_mismatch")
        filled_timeline = {
            record.decision_id: record
            for record in result.execution_timeline
            if record.status in {"filled", "partial_fill"}
        }
        if len(filled_timeline) != result.fills_count:
            raise ValueError("backtest_filled_timeline_identity_not_unique")

    recomputed = CostValidator()
    seen_diagnostic_ids: set[str] = set()
    for diagnostic in diagnostics:
        if not isinstance(diagnostic, CostDiagnostic):
            raise ValueError("backtest_cost_diagnostic_type_invalid")
        if not isinstance(diagnostic.decision_id, str) or not diagnostic.decision_id:
            raise ValueError("backtest_cost_diagnostic_id_invalid")
        if not isinstance(diagnostic.notes, str):
            raise ValueError("backtest_cost_diagnostic_notes_invalid")
        for name in (
            "assumed_cost_bps",
            "actual_cost_bps",
            "cost_diff_bps",
            "assumed_net_edge_bps",
            "actual_net_edge_bps",
        ):
            value = getattr(diagnostic, name)
            if type(value) is not float or not math.isfinite(value):
                raise ValueError(f"backtest_cost_diagnostic_{name}_invalid")
        if require_complete_artifact:
            if diagnostic.decision_id in seen_diagnostic_ids:
                raise ValueError("backtest_cost_diagnostic_id_duplicate")
            seen_diagnostic_ids.add(diagnostic.decision_id)
            timing = filled_timeline.get(diagnostic.decision_id)
            if timing is None:
                raise ValueError("backtest_cost_diagnostic_timeline_mismatch")
            if (
                diagnostic.resolved_at_ts_ms is None
                or diagnostic.fill_ts_ms is None
                or diagnostic.equity_attribution_ts_ms is None
                or diagnostic.actual_fee_bps is None
                or diagnostic.actual_slippage_bps is None
                or diagnostic.filled_exchange_quantity is None
                or diagnostic.average_fill_price is None
                or diagnostic.actual_fee_notional is None
                or diagnostic.fee_currency is None
                or diagnostic.fee_asset is None
                or diagnostic.fee_asset_quantity is None
            ):
                raise ValueError("backtest_cost_attribution_incomplete")
            if diagnostic.fill_ts_ms != diagnostic.resolved_at_ts_ms:
                raise ValueError("backtest_fill_resolution_timestamp_mismatch")
            if (
                timing.resolved_at_ts is None
                or timing.fill_ts is None
                or diagnostic.resolved_at_ts_ms != _ts_to_ms(timing.resolved_at_ts)
                or diagnostic.fill_ts_ms != _ts_to_ms(timing.fill_ts)
            ):
                raise ValueError("backtest_cost_diagnostic_timeline_mismatch")

            assert timing.next_tradable_event_ts is not None
            expected_attribution_ts = timing.next_tradable_event_ts + _parse_timeframe(
                result.config.timeframe
            )
            if diagnostic.equity_attribution_ts_ms != _ts_to_ms(
                expected_attribution_ts
            ):
                raise ValueError("backtest_cost_attribution_timestamp_mismatch")
            if diagnostic.equity_attribution_ts_ms not in {
                point.ts_ms for point in result.equity_curve
            }:
                raise ValueError("backtest_cost_attribution_missing_equity_point")
            if type(diagnostic.edge_flipped_negative) is not bool:
                raise ValueError("backtest_cost_diagnostic_flip_invalid")
            for name in ("actual_fee_bps", "actual_slippage_bps"):
                value = getattr(diagnostic, name)
                if type(value) is not float or not math.isfinite(value):
                    raise ValueError(f"backtest_cost_diagnostic_{name}_invalid")

            actual_fee = finite_float(
                diagnostic.actual_fee_bps,
                reason="backtest_cost_diagnostic_non_finite",
            )
            actual_slippage = finite_float(
                diagnostic.actual_slippage_bps,
                reason="backtest_cost_diagnostic_non_finite",
            )
            expected_fee = (
                result.config.maker_fee_bps
                if result.config.order_type == "post_only"
                else result.config.taker_fee_bps
            )
            if Decimal(str(actual_fee)) != Decimal(str(expected_fee)):
                raise ValueError("backtest_cost_diagnostic_fee_rate_config_mismatch")
            if actual_slippage < 0:
                raise ValueError("backtest_cost_diagnostic_slippage_negative")
            if result.config.order_type == "post_only" and actual_slippage != 0:
                raise ValueError("backtest_post_only_slippage_must_be_zero")
            if diagnostic.fee_currency != contract.settle_currency:
                raise ValueError("backtest_cost_diagnostic_fee_currency_mismatch")
            if diagnostic.fee_asset not in {
                contract.base_currency,
                contract.settle_currency,
            }:
                raise ValueError("backtest_cost_diagnostic_fee_asset_mismatch")
            expected_fee_asset = contract.settle_currency
            if (
                timing.action == "open"
                and result.config.spot_buy_fee_asset == "base"
            ):
                expected_fee_asset = contract.base_currency
            if diagnostic.fee_asset != expected_fee_asset:
                raise ValueError("backtest_cost_diagnostic_fee_asset_action_mismatch")
            if (
                not isinstance(diagnostic.filled_exchange_quantity, Decimal)
                or not diagnostic.filled_exchange_quantity.is_finite()
                or diagnostic.filled_exchange_quantity <= 0
                or not isinstance(diagnostic.average_fill_price, Decimal)
                or not diagnostic.average_fill_price.is_finite()
                or diagnostic.average_fill_price <= 0
                or not isinstance(diagnostic.actual_fee_notional, Decimal)
                or not diagnostic.actual_fee_notional.is_finite()
                or not isinstance(diagnostic.fee_asset_quantity, Decimal)
                or not diagnostic.fee_asset_quantity.is_finite()
            ):
                raise ValueError("backtest_cost_diagnostic_fill_basis_invalid")
            contract.validate_exchange_quantity(
                diagnostic.filled_exchange_quantity
            )
            replayed_fill = _replay_timeline_fill(
                timing,
                config=result.config,
                contract=contract,
            )
            if (
                replayed_fill.filled_qty != diagnostic.filled_exchange_quantity
                or replayed_fill.avg_fill_price != diagnostic.average_fill_price
                or replayed_fill.fee_bps != actual_fee
                or replayed_fill.slippage_bps != actual_slippage
                or replayed_fill.fee_notional != diagnostic.actual_fee_notional
                or replayed_fill.fee_currency != diagnostic.fee_currency
                or replayed_fill.fee_asset != diagnostic.fee_asset
                or replayed_fill.fee_asset_quantity
                != diagnostic.fee_asset_quantity
            ):
                raise ValueError("backtest_cost_diagnostic_fill_replay_mismatch")
            expected_fee_notional = contract.settlement_fee(
                diagnostic.filled_exchange_quantity,
                price=diagnostic.average_fill_price,
                fee_bps=Decimal(str(actual_fee)),
            )
            if diagnostic.actual_fee_notional != expected_fee_notional:
                raise ValueError("backtest_cost_diagnostic_fee_notional_mismatch")
            expected_fee_asset_quantity = contract.fee_asset_amount(
                diagnostic.filled_exchange_quantity,
                price=diagnostic.average_fill_price,
                fee_bps=Decimal(str(actual_fee)),
                fee_asset=diagnostic.fee_asset,
            )
            if diagnostic.fee_asset_quantity != expected_fee_asset_quantity:
                raise ValueError("backtest_cost_diagnostic_fee_asset_quantity_mismatch")
            if contract.fee_settlement_value(
                diagnostic.fee_asset_quantity,
                fee_asset=diagnostic.fee_asset,
                price=diagnostic.average_fill_price,
            ) != diagnostic.actual_fee_notional:
                raise ValueError("backtest_cost_diagnostic_fee_asset_value_mismatch")
            if finite_float(
                diagnostic.actual_cost_bps,
                reason="backtest_cost_diagnostic_non_finite",
            ) != finite_float(
                actual_fee + actual_slippage,
                reason="backtest_cost_diagnostic_non_finite",
            ):
                raise ValueError("backtest_cost_diagnostic_components_mismatch")

            expected = recomputed.record(
                decision_id=diagnostic.decision_id,
                assumed_cost_bps=diagnostic.assumed_cost_bps,
                actual_cost_bps=diagnostic.actual_cost_bps,
                assumed_net_edge_bps=diagnostic.assumed_net_edge_bps,
                notes=diagnostic.notes,
                actual_fee_bps=diagnostic.actual_fee_bps,
                actual_slippage_bps=diagnostic.actual_slippage_bps,
                resolved_at_ts_ms=diagnostic.resolved_at_ts_ms,
                fill_ts_ms=diagnostic.fill_ts_ms,
                equity_attribution_ts_ms=diagnostic.equity_attribution_ts_ms,
                filled_exchange_quantity=diagnostic.filled_exchange_quantity,
                average_fill_price=diagnostic.average_fill_price,
                actual_fee_notional=diagnostic.actual_fee_notional,
                fee_currency=diagnostic.fee_currency,
                fee_asset=diagnostic.fee_asset,
                fee_asset_quantity=diagnostic.fee_asset_quantity,
            )
            if expected != diagnostic:
                raise ValueError("backtest_cost_diagnostic_arithmetic_mismatch")

    if require_complete_artifact:
        if seen_diagnostic_ids != set(filled_timeline):
            raise ValueError("backtest_cost_diagnostic_timeline_mismatch")
        if recomputed.summary() != summary:
            raise ValueError("backtest_cost_summary_recalculation_mismatch")
        fee_total = contract.add_settlement_amounts(
            *(
                diagnostic.actual_fee_notional
                for diagnostic in diagnostics
                if diagnostic.actual_fee_notional is not None
            )
        )
        if result.summary.fee_total != fee_total:
            raise ValueError("backtest_summary_fee_total_mismatch")
        diagnostics_by_equity_ts: dict[int, list[CostDiagnostic]] = {}
        for diagnostic in diagnostics:
            assert diagnostic.equity_attribution_ts_ms is not None
            diagnostics_by_equity_ts.setdefault(
                diagnostic.equity_attribution_ts_ms,
                [],
            ).append(diagnostic)
        replay_tracker = PositionTracker(instrument_contract=contract)
        for point_index, point in enumerate(result.equity_curve):
            for diagnostic in sorted(
                diagnostics_by_equity_ts.get(point.ts_ms, []),
                key=lambda item: item.fill_ts_ms or -1,
            ):
                timing = filled_timeline[diagnostic.decision_id]
                assert diagnostic.filled_exchange_quantity is not None
                assert diagnostic.average_fill_price is not None
                assert diagnostic.actual_fee_notional is not None
                assert diagnostic.fee_currency is not None
                assert diagnostic.fee_asset is not None
                assert diagnostic.fee_asset_quantity is not None
                assert diagnostic.fill_ts_ms is not None
                replayed_snapshot = replay_tracker.apply_fill(
                    Fill(
                        side="buy" if timing.action == "open" else "sell",
                        filled_qty=diagnostic.filled_exchange_quantity,
                        avg_fill_price=diagnostic.average_fill_price,
                        fee_notional=diagnostic.actual_fee_notional,
                        fee_currency=diagnostic.fee_currency,
                        instrument_symbol=contract.symbol,
                        instrument_contract_fingerprint=contract.fingerprint,
                        ts_ms=diagnostic.fill_ts_ms,
                        fee_asset=diagnostic.fee_asset,
                        fee_asset_quantity=diagnostic.fee_asset_quantity,
                    )
                )
                if (
                    timing.post_fill_position_quantity
                    != replayed_snapshot.net_qty.copy_abs()
                ):
                    raise ValueError(
                        "backtest_execution_post_fill_position_mismatch"
                    )
            assert point.mark_price is not None
            snapshot = replay_tracker.mark_to_market(
                point.mark_price,
                point.ts_ms,
            )
            expected_fields = {
                "realized_pnl": snapshot.realized_pnl,
                "unrealized_pnl": snapshot.unrealized_pnl,
                "net_qty": snapshot.net_qty,
                "avg_entry_price": snapshot.avg_entry_price,
                "mark_price": snapshot.last_mark_price,
                "fill_count": snapshot.fill_count,
                "accumulated_fees": snapshot.accumulated_fees,
            }
            if any(
                getattr(point, name) != expected
                for name, expected in expected_fields.items()
            ):
                raise ValueError("backtest_equity_position_replay_mismatch")
            decision_timing = result.execution_timeline[point_index]
            if (
                decision_timing.action == "close"
                and decision_timing.decision_intent_exchange_quantity
                != snapshot.net_qty.copy_abs()
            ):
                raise ValueError("backtest_close_intent_position_mismatch")
        if replay_tracker.snapshot.accumulated_fees != result.summary.fee_total:
            raise ValueError("backtest_summary_equity_fee_ledger_mismatch")


def _validate_result_timeline(
    result: BacktestResult,
    *,
    require_complete_artifact: bool,
) -> None:
    timeline = result.execution_timeline
    if not timeline and not require_complete_artifact:
        return
    if len(timeline) != result.decisions_count:
        raise ValueError("backtest_execution_timeline_count_mismatch")
    filled_records = 0
    previous_observation: datetime | None = None
    bar_duration = _parse_timeframe(result.config.timeframe)
    seen_decision_ids: set[str] = set()
    contract = _resolve_instrument_contract(result.config)
    for index, record in enumerate(timeline):
        if not isinstance(record, ExecutionTimingRecord):
            raise ValueError("backtest_execution_timeline_record_invalid")
        if not isinstance(record.decision_id, str) or not record.decision_id:
            raise ValueError("backtest_execution_decision_id_invalid")
        if record.decision_id in seen_decision_ids:
            raise ValueError("backtest_execution_decision_id_duplicate")
        seen_decision_ids.add(record.decision_id)
        if record.action not in {"open", "hold", "close", "blocked"}:
            raise ValueError("backtest_execution_action_invalid")
        for name in (
            "observation_bar_start_ts",
            "observation_completed_at_ts",
            "decision_ts",
        ):
            value = getattr(record, name)
            if (
                not isinstance(value, datetime)
                or value.tzinfo is None
                or value.utcoffset() != timedelta(0)
            ):
                raise ValueError(f"backtest_execution_{name}_must_be_utc")
        for name in (
            "submitted_at_ts",
            "next_tradable_event_ts",
            "resolved_at_ts",
            "fill_ts",
        ):
            value = getattr(record, name)
            if value is not None and (
                not isinstance(value, datetime)
                or value.tzinfo is None
                or value.utcoffset() != timedelta(0)
            ):
                raise ValueError(f"backtest_execution_{name}_must_be_utc")
        if not result.start_ts <= record.observation_bar_start_ts < result.end_ts:
            raise ValueError("backtest_execution_observation_outside_window")
        if previous_observation is not None and (
            record.observation_bar_start_ts <= previous_observation
        ):
            raise ValueError("backtest_execution_observation_not_ordered")
        previous_observation = record.observation_bar_start_ts
        expected_completed = record.observation_bar_start_ts + bar_duration
        if (
            record.observation_completed_at_ts != expected_completed
            or record.decision_ts != expected_completed
        ):
            raise ValueError("backtest_execution_observation_causality_mismatch")
        if record.decision_id != (
            f"{record.observation_bar_start_ts.isoformat()}_{record.action}"
        ):
            raise ValueError("backtest_execution_decision_identity_mismatch")
        if result.equity_curve[index].ts_ms != _ts_to_ms(expected_completed):
            raise ValueError("backtest_execution_equity_timeline_mismatch")
        if record.status in {"filled", "partial_fill"}:
            filled_records += 1
            if (
                record.action not in {"open", "close"}
                or record.submitted_at_ts != record.decision_ts
                or record.next_tradable_event_ts is None
                or record.resolved_at_ts is None
                or record.fill_ts != record.resolved_at_ts
                or record.price_source not in {"next_bar_open", "next_bar_close"}
                or record.liquidity_source
                not in {"observation_bar_volume", "next_bar_volume"}
            ):
                raise ValueError("backtest_filled_timeline_causality_invalid")
        elif record.status == "no_fill":
            if (
                record.submitted_at_ts != record.decision_ts
                or record.next_tradable_event_ts is None
                or record.resolved_at_ts is None
                or record.fill_ts is not None
                or record.price_source not in {"next_bar_open", "next_bar_close"}
                or record.liquidity_source
                not in {"observation_bar_volume", "next_bar_volume"}
            ):
                raise ValueError("backtest_no_fill_timeline_causality_invalid")
        elif record.status == "no_order":
            if any(
                value is not None
                for value in (
                    record.submitted_at_ts,
                    record.next_tradable_event_ts,
                    record.fill_ts,
                    record.price_source,
                    record.liquidity_source,
                )
            ) or record.resolved_at_ts != record.decision_ts:
                raise ValueError("backtest_no_order_timeline_causality_invalid")
        elif record.status == "expired_no_next_event":
            if (
                record.submitted_at_ts != record.decision_ts
                or record.next_tradable_event_ts is not None
                or record.fill_ts is not None
                or record.price_source is not None
                or record.liquidity_source is not None
                or record.resolved_at_ts != record.decision_ts
                or index != len(timeline) - 1
            ):
                raise ValueError("backtest_expired_timeline_causality_invalid")
        else:
            raise ValueError("backtest_execution_status_invalid")

        if record.status in {"filled", "partial_fill", "no_fill"}:
            if record.action not in {"open", "close"}:
                raise ValueError("backtest_execution_order_action_invalid")
            replayed_fill = _replay_timeline_fill(
                record,
                config=result.config,
                contract=contract,
            )
            expected_status = "no_fill"
            if replayed_fill.filled_qty > 0:
                expected_status = (
                    "partial_fill"
                    if (
                        replayed_fill.filled_qty
                        != record.requested_exchange_quantity
                        or (
                            record.action == "close"
                            and record.post_fill_position_quantity != 0
                        )
                    )
                    else "filled"
                )
            if record.status != expected_status:
                raise ValueError("backtest_execution_fill_status_mismatch")
        elif record.status == "expired_no_next_event":
            if record.action not in {"open", "close"}:
                raise ValueError("backtest_execution_order_action_invalid")
            _validate_unresolved_order_basis(
                record,
                config=result.config,
                contract=contract,
            )
        elif record.status == "no_order":
            _validate_no_order_basis(record, contract=contract)

        if record.status in {"filled", "partial_fill", "no_fill"}:
            if index + 1 >= len(timeline):
                raise ValueError("backtest_execution_next_event_missing")
            expected_next_event = timeline[index + 1].observation_bar_start_ts
            if record.next_tradable_event_ts != expected_next_event:
                raise ValueError("backtest_execution_next_event_mismatch")
            assert record.next_tradable_event_ts is not None
            if record.next_tradable_event_ts < record.decision_ts:
                raise ValueError("backtest_execution_next_event_precedes_decision")
            expected_resolution = (
                record.next_tradable_event_ts
                if record.price_source == "next_bar_open"
                else record.next_tradable_event_ts + bar_duration
            )
            if record.resolved_at_ts != expected_resolution:
                raise ValueError("backtest_execution_resolution_mismatch")
            expected_liquidity = (
                "observation_bar_volume"
                if record.price_source == "next_bar_open"
                else "next_bar_volume"
            )
            if record.liquidity_source != expected_liquidity:
                raise ValueError("backtest_execution_liquidity_source_mismatch")
            expected_price_source = (
                "next_bar_close"
                if result.config.order_type == "post_only"
                else "next_bar_open"
            )
            if record.price_source != expected_price_source:
                raise ValueError("backtest_execution_order_type_mismatch")
    if filled_records != result.fills_count:
        raise ValueError("backtest_execution_fill_count_mismatch")


def _validate_unresolved_order_basis(
    record: ExecutionTimingRecord,
    *,
    config: BacktestConfig,
    contract: InstrumentContract,
) -> None:
    expected_side = "buy" if record.action == "open" else "sell"
    if record.fill_side != expected_side:
        raise ValueError("backtest_execution_fill_side_mismatch")
    for name in (
        "decision_intent_exchange_quantity",
        "requested_exchange_quantity",
    ):
        value = getattr(record, name)
        if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
            raise ValueError("backtest_execution_order_basis_incomplete")
    if (
        not isinstance(record.liquidity_reference_quantity, Decimal)
        or not record.liquidity_reference_quantity.is_finite()
        or record.liquidity_reference_quantity < 0
        or not isinstance(record.max_volume_participation, Decimal)
        or not record.max_volume_participation.is_finite()
        or not Decimal("0") < record.max_volume_participation <= Decimal("1")
        or record.max_volume_participation != config.max_volume_participation
    ):
        raise ValueError("backtest_execution_order_basis_incomplete")
    expected_requested = record.decision_intent_exchange_quantity
    if record.action == "open":
        contract.validate_exchange_quantity(expected_requested)
    else:
        expected_requested = contract.fillable_exchange_quantity(
            expected_requested,
            available_quantity=expected_requested,
            max_participation=Decimal("1"),
        )
    if record.requested_exchange_quantity != expected_requested:
        raise ValueError("backtest_execution_requested_quantity_mismatch")
    if record.requested_exchange_quantity > 0:
        contract.validate_exchange_quantity(record.requested_exchange_quantity)
    if record.status == "expired_no_next_event" and (
        record.reference_price is not None
        or record.post_fill_position_quantity is not None
    ):
        raise ValueError("backtest_execution_expired_basis_invalid")


def _replay_timeline_fill(
    record: ExecutionTimingRecord,
    *,
    config: BacktestConfig,
    contract: InstrumentContract,
) -> FillResult:
    _validate_unresolved_order_basis(record, config=config, contract=contract)
    if (
        not isinstance(record.reference_price, Decimal)
        or not record.reference_price.is_finite()
        or record.reference_price <= 0
    ):
        raise ValueError("backtest_execution_reference_price_invalid")
    try:
        contract.validate_exchange_price(record.reference_price)
    except ValueError as exc:
        raise ValueError("backtest_execution_reference_price_tick_misaligned") from exc
    if record.status in {"filled", "partial_fill"}:
        if (
            not isinstance(record.post_fill_position_quantity, Decimal)
            or not record.post_fill_position_quantity.is_finite()
            or record.post_fill_position_quantity < 0
        ):
            raise ValueError("backtest_execution_post_fill_position_invalid")
    elif record.post_fill_position_quantity is not None:
        raise ValueError("backtest_execution_post_fill_position_invalid")
    assert record.requested_exchange_quantity is not None
    assert record.liquidity_reference_quantity is not None
    assert record.submitted_at_ts is not None
    return FillSimulator(
        instrument_contract=contract,
        maker_fee_bps=config.maker_fee_bps,
        taker_fee_bps=config.taker_fee_bps,
        ioc_slippage_bps=config.ioc_slippage_bps,
        max_volume_participation=config.max_volume_participation,
        spot_buy_fee_asset=config.spot_buy_fee_asset,
    ).simulate(
        FillRequest(
            order_id=record.decision_id,
            side=record.fill_side,  # type: ignore[arg-type]
            order_type=config.order_type,
            target_qty=record.requested_exchange_quantity,
            submitted_at_ts=_ts_to_ms(record.submitted_at_ts),
        ),
        record.reference_price,
        record.liquidity_reference_quantity,
    )


def _validate_no_order_basis(
    record: ExecutionTimingRecord,
    *,
    contract: InstrumentContract,
) -> None:
    if record.action in {"hold", "blocked"}:
        if any(
            value is not None
            for value in (
                record.fill_side,
                record.decision_intent_exchange_quantity,
                record.requested_exchange_quantity,
                record.liquidity_reference_quantity,
                record.max_volume_participation,
                record.reference_price,
                record.post_fill_position_quantity,
            )
        ):
            raise ValueError("backtest_no_order_basis_invalid")
        return
    if record.action != "close":
        raise ValueError("backtest_no_order_action_invalid")
    intent = record.decision_intent_exchange_quantity
    if (
        record.fill_side != "sell"
        or not isinstance(intent, Decimal)
        or not intent.is_finite()
        or intent <= 0
        or not isinstance(record.requested_exchange_quantity, Decimal)
        or not record.requested_exchange_quantity.is_finite()
        or record.requested_exchange_quantity != Decimal("0")
        or not isinstance(record.post_fill_position_quantity, Decimal)
        or not record.post_fill_position_quantity.is_finite()
        or record.post_fill_position_quantity <= 0
        or record.post_fill_position_quantity != intent
        or any(
            value is not None
            for value in (
                record.liquidity_reference_quantity,
                record.max_volume_participation,
                record.reference_price,
            )
        )
        or contract.fillable_exchange_quantity(
            intent,
            available_quantity=intent,
            max_participation=Decimal("1"),
        )
        != 0
    ):
        raise ValueError("backtest_no_order_dust_close_invalid")


def _resolve_instrument_contract(config: BacktestConfig) -> InstrumentContract:
    """Return the explicit arithmetic contract before any database access."""

    contract = config.instrument_contract
    if not isinstance(contract, InstrumentContract):
        raise ValueError("replay_instrument_contract_required")
    raw_symbol = config.symbol
    if (
        not isinstance(raw_symbol, str)
        or not raw_symbol
        or raw_symbol != raw_symbol.strip().upper()
    ):
        raise ValueError("replay_symbol_must_be_canonical")
    if contract.symbol != raw_symbol:
        raise ValueError("replay_instrument_contract_symbol_mismatch")
    return contract


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
    * 非空 → 在 family-specific baseline 上精确 patch（支持平坦 cost keys）
    """
    if not overrides:
        return ReplayParameterOverrides.for_family(family)
    baseline = ReplayParameterOverrides.for_family(family)
    return ReplayParameterOverrides.from_dict(overrides, base=baseline)


def _validate_parameter_override_contract(
    overrides: dict[str, Any] | None,
    *,
    adapter: BaseReplayAdapter,
) -> None:
    """Reject unknown or behaviorally unconsumed experiment parameters."""

    if overrides is None:
        return
    if not isinstance(overrides, dict) or any(
        not isinstance(key, str) for key in overrides
    ):
        raise ValueError("replay_parameter_overrides_must_be_string_mapping")
    unknown = set(overrides) - adapter.accepted_parameter_keys
    if unknown:
        raise ValueError(
            "unconsumed_replay_parameter_keys:" + ",".join(sorted(unknown))
        )
    nested_cost = overrides.get("cost_config")
    flat_cost = set(overrides) & _REPLAY_COST_OVERRIDE_KEYS
    if "cost_config" in overrides:
        if not isinstance(nested_cost, dict) or any(
            not isinstance(key, str) for key in nested_cost
        ):
            raise ValueError("replay_cost_config_must_be_string_mapping")
        unknown_cost = set(nested_cost) - _REPLAY_COST_OVERRIDE_KEYS
        if unknown_cost:
            raise ValueError(
                "unknown_replay_cost_parameter_keys:"
                + ",".join(sorted(unknown_cost))
            )
        if flat_cost:
            raise ValueError("conflicting_flat_and_nested_replay_cost_parameters")


def _validate_replay_cost_contract(params: ReplayParameterOverrides) -> None:
    cost = params.cost_config
    maker = finite_float(
        cost.maker_fee_bps,
        reason="replay_cost_parameter_non_finite",
    )
    taker = finite_float(
        cost.taker_fee_bps,
        reason="replay_cost_parameter_non_finite",
    )
    slippage = finite_float(
        cost.slippage_bps,
        reason="replay_cost_parameter_non_finite",
    )
    passive_bias = finite_float(
        cost.passive_bias,
        reason="replay_cost_parameter_non_finite",
    )
    maker_taker_bias = finite_float(
        cost.maker_taker_bias,
        reason="replay_cost_parameter_non_finite",
    )
    if not -10_000.0 < maker < 10_000.0:
        raise ValueError("replay_maker_fee_bps_out_of_range")
    if not 0.0 <= taker < 10_000.0:
        raise ValueError("replay_taker_fee_bps_out_of_range")
    if not 0.0 <= slippage < 10_000.0:
        raise ValueError("replay_slippage_bps_out_of_range")
    if not 0.0 <= passive_bias <= 1.0:
        raise ValueError("replay_passive_bias_out_of_range")
    if not -1.0 <= maker_taker_bias <= 1.0:
        raise ValueError("replay_maker_taker_bias_out_of_range")
    if str(cost.execution_style).strip().lower() not in REPLAY_EXECUTION_STYLES:
        raise ValueError("replay_execution_style_unsupported")


def _validate_replay_decision(
    decision: ReplayDecision,
    *,
    adapter: BaseReplayAdapter,
    config: BacktestConfig,
    contract: InstrumentContract,
    current_position_qty: Decimal,
) -> None:
    """Reject malformed adapter output before it can alter replay state."""

    if not isinstance(decision, ReplayDecision):
        raise ValueError("replay_decision_type_invalid")
    if decision.family != adapter.family_name:
        raise ValueError("replay_decision_family_mismatch")
    if decision.symbol != contract.symbol:
        raise ValueError("replay_decision_symbol_mismatch")
    if decision.timeframe != config.timeframe:
        raise ValueError("replay_decision_timeframe_mismatch")
    for name in (
        "selectable",
        "execution_compatible",
        "score_stable",
        "cost_bps_is_explicit",
    ):
        if type(getattr(decision, name)) is not bool:
            raise ValueError(f"replay_decision_{name}_invalid")
    scores = {
        "long_score": finite_float(
            decision.long_score,
            reason="replay_decision_long_score_invalid",
        ),
        "short_score": finite_float(
            decision.short_score,
            reason="replay_decision_short_score_invalid",
        ),
    }
    if any(not 0.0 <= value <= 1.0 for value in scores.values()):
        raise ValueError("replay_decision_score_out_of_range")
    edge_components: dict[str, float] = {}
    for name in (
        "expected_net_edge_bps",
        "signal_edge_proxy_bps",
        "funding_adjustment_bps",
        "cost_bps",
        "noise_buffer_bps",
    ):
        edge_components[name] = finite_float(
            getattr(decision, name),
            reason=f"replay_decision_{name}_invalid",
        )
    if not decision.cost_bps_is_explicit:
        raise ValueError("replay_decision_cost_must_be_explicit")
    reported_edge = Decimal(str(edge_components["expected_net_edge_bps"]))
    recomputed_edge = (
        Decimal(str(edge_components["signal_edge_proxy_bps"]))
        + Decimal(str(edge_components["funding_adjustment_bps"]))
        - Decimal(str(edge_components["cost_bps"]))
        - Decimal(str(edge_components["noise_buffer_bps"]))
    )
    if (reported_edge - recomputed_edge).copy_abs() > _EDGE_IDENTITY_TOLERANCE_BPS:
        raise ValueError("replay_decision_edge_identity_mismatch")
    for name in ("funding_rate", "close_price"):
        value = getattr(decision, name)
        if value is not None:
            resolved = finite_float(
                value,
                reason=f"replay_decision_{name}_invalid",
            )
            if name == "close_price" and resolved <= 0:
                raise ValueError("replay_decision_close_price_invalid")
    if (
        not isinstance(decision.target_position_qty, Decimal)
        or not decision.target_position_qty.is_finite()
        or decision.target_position_qty < 0
        or not isinstance(decision.delta_position_qty, Decimal)
        or not decision.delta_position_qty.is_finite()
    ):
        raise ValueError("replay_decision_position_quantity_invalid")
    # target_position_qty is account inventory and may contain fee-created
    # SPOT dust. Only an actual open order delta must be lot/min aligned here;
    # an exact close intent is lot-floored when constructing FillRequest.
    if decision.delta_position_qty != 0 and decision.action != "close":
        contract.validate_exchange_quantity(
            decision.delta_position_qty.copy_abs()
        )
    if not isinstance(decision.blocking_reasons, list) or any(
        not isinstance(reason, str) for reason in decision.blocking_reasons
    ):
        raise ValueError("replay_decision_blocking_reasons_invalid")
    if decision.action not in {"open", "hold", "close", "blocked"}:
        raise ValueError("replay_decision_action_invalid")
    if decision.action == "open" and decision.delta_position_qty <= 0:
        raise ValueError("spot_replay_open_delta_must_be_positive")
    if decision.action == "open" and (
        not decision.selectable
        or not decision.execution_compatible
        or not decision.score_stable
        or bool(decision.blocking_reasons)
    ):
        raise ValueError("replay_open_not_execution_eligible")
    if decision.action == "open" and decision.short_score > decision.long_score:
        raise ValueError("spot_replay_short_open_unavailable")
    if decision.action == "close" and (
        decision.target_position_qty != 0
        or decision.delta_position_qty >= 0
    ):
        raise ValueError("spot_replay_close_delta_invalid")
    if decision.action in {"hold", "blocked"} and (
        decision.delta_position_qty != 0
        or decision.target_position_qty != current_position_qty
    ):
        raise ValueError("replay_non_order_action_position_mismatch")
    if (
        not isinstance(current_position_qty, Decimal)
        or not current_position_qty.is_finite()
        or current_position_qty < 0
    ):
        raise ValueError("replay_current_position_quantity_invalid")
    expected_target_qty = contract.add_exchange_quantities(
        current_position_qty,
        decision.delta_position_qty,
    )
    if expected_target_qty != decision.target_position_qty:
        raise ValueError("replay_decision_position_identity_mismatch")
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
    current_exchange_qty = ctx.position_tracker.snapshot.net_qty.copy_abs()
    if (
        decision.action == "close"
        and pending.fill_request.target_qty > current_exchange_qty
    ):
        raise ValueError("replay_close_quantity_exceeds_position")
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
    residual_position_qty = current_exchange_qty
    if filled:
        fill = Fill(
            side=fill_result.side,
            filled_qty=fill_result.filled_qty,
            avg_fill_price=fill_result.avg_fill_price,
            fee_notional=fill_result.fee_notional,
            fee_currency=fill_result.fee_currency,
            instrument_symbol=fill_result.instrument_symbol,
            instrument_contract_fingerprint=(
                fill_result.instrument_contract_fingerprint
            ),
            ts_ms=_ts_to_ms(resolution_ts),
            fee_asset=fill_result.fee_asset,
            fee_asset_quantity=fill_result.fee_asset_quantity,
        )
        position_snapshot = ctx.position_tracker.apply_fill(fill)
        residual_position_qty = position_snapshot.net_qty.copy_abs()
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
        per_decision_cost = finite_float(
            decision.cost_bps,
            reason="replay_decision_cost_non_finite",
        )
        ctx.cost_validator.record(
            decision_id=_decision_id(decision),
            assumed_cost_bps=per_decision_cost,
            actual_cost_bps=finite_float(
                fill_result.fee_bps + fill_result.slippage_bps,
                reason="fill_cost_non_finite",
            ),
            assumed_net_edge_bps=finite_float(
                decision.expected_net_edge_bps,
                reason="replay_decision_edge_non_finite",
            ),
            actual_fee_bps=finite_float(
                fill_result.fee_bps,
                reason="fill_fee_non_finite",
            ),
            actual_slippage_bps=finite_float(
                fill_result.slippage_bps,
                reason="fill_slippage_bps_non_finite",
            ),
            filled_exchange_quantity=fill_result.filled_qty,
            average_fill_price=fill_result.avg_fill_price,
            actual_fee_notional=fill_result.fee_notional,
            fee_currency=fill_result.fee_currency,
            fee_asset=fill_result.fee_asset,
            fee_asset_quantity=fill_result.fee_asset_quantity,
            notes=(
                f"fill_kind={fill_result.fill_kind};"
                f"execution_model={config.execution_model_version};"
                f"fill_model={config.fill_model_version};"
                f"price_source={price_source};"
                f"liquidity_source={liquidity_source}"
            ),
            resolved_at_ts_ms=_ts_to_ms(resolution_ts),
            fill_ts_ms=_ts_to_ms(resolution_ts),
            equity_attribution_ts_ms=_ts_to_ms(
                execution_bar.ts + bar_duration
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
                if (
                    fill_result.filled_qty == pending.fill_request.target_qty
                    and not (
                        decision.action == "close"
                        and residual_position_qty != 0
                    )
                )
                else "partial_fill"
                if filled
                else "no_fill"
            ),
            price_source=price_source,
            liquidity_source=liquidity_source,
            fill_side=pending.fill_request.side,
            decision_intent_exchange_quantity=(
                pending.decision.delta_position_qty.copy_abs()
            ),
            requested_exchange_quantity=pending.fill_request.target_qty,
            liquidity_reference_quantity=bar_volume,
            max_volume_participation=config.max_volume_participation,
            reference_price=reference_price,
            post_fill_position_quantity=(
                residual_position_qty if filled else None
            ),
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

    synchronized.position_qty = net_qty.copy_abs()
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
    contract: InstrumentContract,
    order_type: Literal["ioc", "post_only", "bounded_limit"],
    submitted_at_ts: datetime,
) -> FillRequest | None:
    """构造一笔 FillRequest。

    ``order_id`` 以 observation bar identity 和 action 为基，保证复现；
    ``submitted_at_ts`` 则是完整 observation bar 可见之后的 bar end。
    """
    order_id = _decision_id(decision)
    target_qty = delta.copy_abs()
    if decision.action == "close":
        target_qty = contract.fillable_exchange_quantity(
            target_qty,
            available_quantity=target_qty,
            max_participation=Decimal("1"),
        )
        if target_qty == 0:
            return None
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


def _validate_bar_sequence(
    bars: list[ReplayBar],
    *,
    bar_duration: timedelta,
    instrument_contract: InstrumentContract,
    start_ts: datetime,
    end_ts: datetime,
) -> int:
    """在任何 adapter 观察前验证身份、数值、窗口与连续 cadence。"""

    def _finite_decimal_field(
        value: object,
        *,
        field_name: str,
        index: int,
        positive: bool = False,
        non_negative: bool = False,
    ) -> Decimal:
        if not isinstance(value, Decimal) or not value.is_finite():
            raise ValueError(
                f"replay_bar_{field_name}_invalid:bar_index={index}"
            )
        if positive and value <= 0:
            raise ValueError(
                f"replay_bar_{field_name}_invalid:bar_index={index}"
            )
        if non_negative and value < 0:
            raise ValueError(
                f"replay_bar_{field_name}_invalid:bar_index={index}"
            )
        return value

    previous: ReplayBar | None = None
    cadence_gap_count = 0
    for index, bar in enumerate(bars):
        if bar.symbol != instrument_contract.symbol:
            raise ValueError(
                "replay_bar_symbol_mismatch:"
                f"bar_index={index} expected={instrument_contract.symbol} "
                f"actual={bar.symbol}"
            )
        if not isinstance(bar.ts, datetime):
            raise ValueError(f"replay_bar_timestamp_invalid:bar_index={index}")
        if bar.ts.tzinfo is None or bar.ts.utcoffset() != timedelta(0):
            raise ValueError(
                f"replay_bar_timestamp_must_be_utc:bar_index={index}"
            )
        try:
            inside_window = start_ts <= bar.ts and bar.ts + bar_duration <= end_ts
        except TypeError as exc:
            raise ValueError(
                "Replay bars must use the request timezone awareness: "
                f"bar_index={index} bar_ts={bar.ts!r}"
            ) from exc
        if not inside_window:
            raise ValueError(
                "replay_bar_outside_requested_window:"
                f"bar_index={index} ts={bar.ts.isoformat()}"
            )
        if not bar.is_closed:
            raise ValueError(
                "Replay lookahead guard rejected unfinished bar: "
                f"bar_index={index} ts={bar.ts.isoformat()}"
            )
        open_price = _finite_decimal_field(
            bar.open,
            field_name="open",
            index=index,
            positive=True,
        )
        high_price = _finite_decimal_field(
            bar.high,
            field_name="high",
            index=index,
            positive=True,
        )
        low_price = _finite_decimal_field(
            bar.low,
            field_name="low",
            index=index,
            positive=True,
        )
        close_price = _finite_decimal_field(
            bar.close,
            field_name="close",
            index=index,
            positive=True,
        )
        for field_name, price in (
            ("open", open_price),
            ("high", high_price),
            ("low", low_price),
            ("close", close_price),
        ):
            try:
                instrument_contract.validate_exchange_price(price)
            except ValueError as exc:
                raise ValueError(
                    f"replay_bar_{field_name}_tick_misaligned:bar_index={index}"
                ) from exc
        if (
            low_price > min(open_price, close_price)
            or high_price < max(open_price, close_price)
            or low_price > high_price
        ):
            raise ValueError(f"replay_bar_ohlc_inconsistent:bar_index={index}")
        for field_name in ("volume", "quote_volume"):
            value = getattr(bar, field_name)
            if value is not None:
                _finite_decimal_field(
                    value,
                    field_name=field_name,
                    index=index,
                    non_negative=True,
                )
        if bar.aligned_funding_rate is not None:
            _finite_decimal_field(
                bar.aligned_funding_rate,
                field_name="funding_rate",
                index=index,
            )
        if instrument_contract.contract_type == "spot":
            if (
                bar.aligned_funding_rate is not None
                or bar.funding_source_ts is not None
            ):
                raise ValueError(
                    f"spot_replay_funding_must_be_absent:bar_index={index}"
                )
        elif (bar.aligned_funding_rate is None) != (
            bar.funding_source_ts is None
        ):
            raise ValueError(
                f"replay_bar_funding_lineage_incomplete:bar_index={index}"
            )
        elif bar.funding_source_ts is not None:
            source_ts = bar.funding_source_ts
            if (
                not isinstance(source_ts, datetime)
                or source_ts.tzinfo is None
                or source_ts.utcoffset() != timedelta(0)
            ):
                raise ValueError(
                    f"replay_bar_funding_source_ts_invalid:bar_index={index}"
                )
            if source_ts > bar.ts + bar_duration:
                raise ValueError(
                    f"replay_bar_funding_source_is_future:bar_index={index}"
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
            if bar.ts != previous.ts + bar_duration:
                cadence_gap_count += 1
        previous = bar
    return cadence_gap_count


__all__ = [
    "BACKTEST_ARTIFACT_SCHEMA_VERSION",
    "BacktestConfig",
    "BacktestResult",
    "ExecutionTimingRecord",
    "run_backtest",
    "validate_backtest_request",
    "validate_backtest_result_units",
]
