"""FS-003 adversarial/golden tests for causal replay execution timing."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from aats.data_platform.replay.adapters.base_adapter import BaseReplayAdapter
from aats.data_platform.replay.backtest.fill_simulator import (
    FillResult,
    FillSimulator,
)
from aats.data_platform.replay.backtest.harness import BacktestConfig, run_backtest
from aats.data_platform.replay.core.replay_context import (
    ReplayBar,
    ReplayBarContext,
    ReplayDecision,
    ReplayState,
)


_BASE_TS = datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc)


def _bar(
    *,
    ts: datetime,
    open_price: str,
    close_price: str,
    volume: Decimal | None = Decimal("1000"),
    is_closed: bool = True,
) -> ReplayBar:
    open_value = Decimal(open_price)
    close_value = Decimal(close_price)
    return ReplayBar(
        symbol="BTC-USDT-SWAP",
        ts=ts,
        open=open_value,
        high=max(open_value, close_value),
        low=min(open_value, close_value),
        close=close_value,
        volume=volume,
        quote_volume=None,
        is_closed=is_closed,
        aligned_funding_rate=None,
        funding_source_ts=None,
    )


def _decision(
    ctx: ReplayBarContext,
    *,
    action: str,
    delta: Decimal,
    target: Decimal,
) -> ReplayDecision:
    return ReplayDecision(
        ts=ctx.bar.ts,
        family="independent",
        symbol=ctx.symbol,
        timeframe=ctx.timeframe,
        state="holding" if target else "flat",
        selectable=True,
        execution_compatible=True,
        long_score=0.8,
        short_score=0.1,
        blocking_reasons=[],
        expected_net_edge_bps=10.0,
        target_position_qty=target,
        delta_position_qty=delta,
        cost_bps=6.0,
        action=action,
    )


class _ScriptAdapter(BaseReplayAdapter):
    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = script
        self.cursor = 0
        self.calls = 0

    @property
    def family_name(self) -> str:
        return "independent"

    def reset_state(self) -> ReplayState:
        self.cursor = 0
        self.calls = 0
        return ReplayState()

    def evaluate_bar(self, ctx: ReplayBarContext) -> ReplayDecision:
        self.calls += 1
        entry = (
            self.script[self.cursor]
            if self.cursor < len(self.script)
            else {"action": "hold", "delta": Decimal("0"), "target": Decimal("0")}
        )
        self.cursor += 1
        return _decision(
            ctx,
            action=entry["action"],
            delta=entry["delta"],
            target=entry["target"],
        )


class _StateMutatingAdapter(BaseReplayAdapter):
    """Mimics the real adapter's proposal-state mutation and records next input."""

    def __init__(self) -> None:
        self.seen_states: list[tuple[str, Decimal]] = []
        self.cursor = 0

    @property
    def family_name(self) -> str:
        return "independent"

    def reset_state(self) -> ReplayState:
        self.cursor = 0
        self.seen_states.clear()
        return ReplayState()

    def evaluate_bar(self, ctx: ReplayBarContext) -> ReplayDecision:
        self.seen_states.append((ctx.state.position_side, ctx.state.position_qty))
        if self.cursor == 0:
            ctx.state.position_side = "long"
            ctx.state.position_qty = Decimal("1")
            ctx.state.entry_price = ctx.bar.close
            ctx.state.entry_ts = ctx.bar.ts
            action = "open"
            delta = Decimal("1")
            target = Decimal("1")
        else:
            action = "hold"
            delta = Decimal("0")
            target = ctx.state.position_qty
        self.cursor += 1
        return _decision(ctx, action=action, delta=delta, target=target)


def _run(
    bars: list[ReplayBar],
    adapter: BaseReplayAdapter,
    *,
    config: BacktestConfig | None = None,
):
    with patch(
        "aats.data_platform.replay.backtest.harness.load_gold_bars",
        return_value=bars,
    ):
        return run_backtest(
            MagicMock(),
            config=config or BacktestConfig(),
            start_ts=_BASE_TS,
            end_ts=_BASE_TS + timedelta(days=2),
            adapter=adapter,
        )


def test_completed_bar_decision_fills_only_at_gap_next_bar_open() -> None:
    bars = [
        _bar(ts=_BASE_TS, open_price="100", close_price="110"),
        _bar(
            ts=_BASE_TS + timedelta(hours=2),
            open_price="150",
            close_price="140",
        ),
    ]
    adapter = _ScriptAdapter(
        [
            {"action": "open", "delta": Decimal("1"), "target": Decimal("1")},
            {"action": "hold", "delta": Decimal("0"), "target": Decimal("1")},
        ]
    )
    observed_prices: list[Decimal] = []
    observed_submit_ts: list[int] = []
    original = FillSimulator.simulate

    def _spy(self, request, bar_close_price, bar_volume, **kwargs):
        observed_prices.append(bar_close_price)
        observed_submit_ts.append(request.submitted_at_ts)
        return original(self, request, bar_close_price, bar_volume, **kwargs)

    with patch.object(FillSimulator, "simulate", new=_spy):
        result = _run(bars, adapter)

    record = result.execution_timeline[0]
    observation_end = _BASE_TS + timedelta(hours=1)
    next_event = _BASE_TS + timedelta(hours=2)
    assert observed_prices == [Decimal("150")]
    assert observed_submit_ts == [int(observation_end.timestamp() * 1000)]
    assert result.fills_count == 1
    assert record.observation_completed_at_ts == observation_end
    assert record.decision_ts == observation_end
    assert record.submitted_at_ts == observation_end
    assert record.next_tradable_event_ts == next_event
    assert record.fill_ts == next_event
    assert record.price_source == "next_bar_open"
    assert record.liquidity_source == "observation_bar_volume"
    assert record.status == "filled"


def test_terminal_trade_expires_instead_of_same_close_fill() -> None:
    adapter = _ScriptAdapter(
        [{"action": "open", "delta": Decimal("1"), "target": Decimal("1")}]
    )
    result = _run(
        [_bar(ts=_BASE_TS, open_price="100", close_price="110")],
        adapter,
    )

    assert result.fills_count == 0
    assert result.execution_timeline[0].status == "expired_no_next_event"
    assert result.execution_timeline[0].fill_ts is None


def test_unfinished_bar_is_rejected_before_adapter_observes_it() -> None:
    adapter = _ScriptAdapter([])
    unfinished = _bar(
        ts=_BASE_TS,
        open_price="100",
        close_price="999",
        is_closed=False,
    )

    with pytest.raises(ValueError, match="unfinished bar"):
        _run([unfinished], adapter)
    assert adapter.calls == 0


@pytest.mark.parametrize(
    "second_ts,error",
    [
        (_BASE_TS, "strictly increasing"),
        (_BASE_TS + timedelta(minutes=30), "overlap"),
    ],
)
def test_duplicate_or_overlapping_bars_fail_closed(
    second_ts: datetime,
    error: str,
) -> None:
    adapter = _ScriptAdapter([])
    first = _bar(ts=_BASE_TS, open_price="100", close_price="101")
    second = replace(first, ts=second_ts)

    with pytest.raises(ValueError, match=error):
        _run([first, second], adapter)
    assert adapter.calls == 0


def test_post_only_missing_liquidity_is_no_fill_and_rejects_proposed_state() -> None:
    adapter = _StateMutatingAdapter()
    bars = [
        _bar(ts=_BASE_TS, open_price="100", close_price="110"),
        _bar(
            ts=_BASE_TS + timedelta(hours=1),
            open_price="111",
            close_price="112",
            volume=None,
        ),
    ]
    result = _run(bars, adapter, config=BacktestConfig(order_type="post_only"))

    assert result.fills_count == 0
    assert result.execution_timeline[0].status == "no_fill"
    assert result.execution_timeline[0].price_source == "next_bar_close"
    assert result.execution_timeline[0].liquidity_source == "next_bar_volume"
    assert adapter.seen_states == [
        ("flat", Decimal("0")),
        ("flat", Decimal("0")),
    ]


def test_ioc_liquidity_cap_uses_observation_volume_not_future_volume() -> None:
    adapter = _StateMutatingAdapter()
    bars = [
        _bar(
            ts=_BASE_TS,
            open_price="100",
            close_price="110",
            volume=Decimal("50"),
        ),
        _bar(
            ts=_BASE_TS + timedelta(hours=1),
            open_price="120",
            close_price="121",
            volume=Decimal("1000000"),
        ),
    ]
    observed_volumes: list[Decimal] = []
    original = FillSimulator.simulate

    def _spy(self, request, bar_close_price, bar_volume, **kwargs):
        observed_volumes.append(bar_volume)
        return original(self, request, bar_close_price, bar_volume, **kwargs)

    with patch.object(FillSimulator, "simulate", new=_spy):
        result = _run(bars, adapter)

    assert observed_volumes == [Decimal("50")]
    assert result.execution_timeline[0].status == "partial_fill"
    assert result.execution_timeline[0].liquidity_source == (
        "observation_bar_volume"
    )
    assert adapter.seen_states == [
        ("flat", Decimal("0")),
        ("long", Decimal("0.50")),
    ]


def test_partial_fill_commits_only_actual_position_quantity() -> None:
    adapter = _StateMutatingAdapter()
    bars = [
        _bar(ts=_BASE_TS, open_price="100", close_price="110"),
        _bar(
            ts=_BASE_TS + timedelta(hours=1),
            open_price="120",
            close_price="121",
        ),
    ]

    def _partial_fill(_self, request, bar_close_price, bar_volume, **_kwargs):
        del bar_volume
        return FillResult(
            order_id=request.order_id,
            side=request.side,
            filled_qty=Decimal("0.4"),
            avg_fill_price=bar_close_price,
            fee_bps=5.0,
            fee_notional=Decimal("0.024"),
            fill_kind="taker",
            notes="adversarial partial fill",
        )

    with patch.object(FillSimulator, "simulate", new=_partial_fill):
        result = _run(bars, adapter)

    assert result.fills_count == 1
    assert result.execution_timeline[0].status == "partial_fill"
    assert adapter.seen_states == [
        ("flat", Decimal("0")),
        ("long", Decimal("0.4")),
    ]


def test_bar_close_mark_to_market_uses_bar_end_timestamp() -> None:
    adapter = _ScriptAdapter(
        [{"action": "hold", "delta": Decimal("0"), "target": Decimal("0")}]
    )
    result = _run(
        [_bar(ts=_BASE_TS, open_price="100", close_price="110")],
        adapter,
    )

    assert result.equity_curve[0].ts_ms == int(
        (_BASE_TS + timedelta(hours=1)).timestamp() * 1000
    )
    assert result.config.execution_model_version == "next_bar_event_v2"


def test_invalid_timeframe_fails_before_adapter_evaluation() -> None:
    adapter = _ScriptAdapter([])
    with pytest.raises(ValueError, match="Unsupported replay timeframe"):
        _run(
            [_bar(ts=_BASE_TS, open_price="100", close_price="101")],
            adapter,
            config=BacktestConfig(timeframe="calendar-month"),
        )
    assert adapter.calls == 0


def test_legacy_same_bar_model_cannot_be_reenabled() -> None:
    adapter = _ScriptAdapter([])
    legacy_config = BacktestConfig(execution_model_version="same_bar_v1")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Unsupported execution_model_version"):
        _run(
            [_bar(ts=_BASE_TS, open_price="100", close_price="101")],
            adapter,
            config=legacy_config,
        )
    assert adapter.calls == 0
