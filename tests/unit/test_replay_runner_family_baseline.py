from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from aats.data_platform.replay.adapters.base_adapter import BaseReplayAdapter
from aats.data_platform.replay.core.replay_context import ReplayBar, ReplayState
from aats.data_platform.replay.core.replay_runner import run_replay


class _CapturingDirectionalAdapter(BaseReplayAdapter):
    def __init__(self) -> None:
        self.params = None
        self.observation_completed_at_ts = None
        self.decision_ts = None
        self.timeframe = None

    @property
    def family_name(self) -> str:
        return "directional"

    @property
    def algorithm_version(self) -> str:
        return "test-directional/v1"

    @property
    def accepted_parameter_keys(self) -> frozenset[str]:
        return frozenset()

    def reset_state(self) -> ReplayState:
        return ReplayState()

    def evaluate_bar(self, ctx):
        self.params = ctx.params
        self.observation_completed_at_ts = ctx.observation_completed_at_ts
        self.decision_ts = ctx.decision_ts
        self.timeframe = ctx.timeframe
        return SimpleNamespace(long_score=0.0, short_score=0.0, state="flat")


def test_run_replay_none_params_uses_adapter_family_baseline() -> None:
    ts = datetime(2026, 8, 27, tzinfo=timezone.utc)
    bar = ReplayBar(
        symbol="BTC-USDT",
        ts=ts,
        open=Decimal("1"),
        high=Decimal("1"),
        low=Decimal("1"),
        close=Decimal("1"),
        volume=Decimal("1"),
        quote_volume=Decimal("1"),
        is_closed=True,
        aligned_funding_rate=None,
        funding_source_ts=None,
    )
    adapter = _CapturingDirectionalAdapter()

    with patch(
        "aats.data_platform.replay.core.replay_runner.load_gold_bars",
        return_value=[bar],
    ):
        run_replay(
            MagicMock(),
            adapter=adapter,
            symbol=bar.symbol,
            timeframe="1h",
            dataset_version="v1",
            start_ts=ts,
            end_ts=datetime(2026, 8, 28, tzinfo=timezone.utc),
        )

    assert adapter.params is not None
    assert adapter.params.entry_threshold == 0.45
    assert adapter.params.close_threshold == 0.20
    assert adapter.params.scale_in_threshold == 0.55
    assert adapter.observation_completed_at_ts == ts + timedelta(hours=1)
    assert adapter.decision_ts == ts + timedelta(hours=1)


def test_run_replay_canonicalizes_timeframe_before_gold_and_adapter() -> None:
    ts = datetime(2026, 8, 27, tzinfo=timezone.utc)
    bar = ReplayBar(
        symbol="BTC-USDT",
        ts=ts,
        open=Decimal("1"),
        high=Decimal("1"),
        low=Decimal("1"),
        close=Decimal("1"),
        volume=Decimal("1"),
        quote_volume=Decimal("1"),
        is_closed=True,
        aligned_funding_rate=None,
        funding_source_ts=None,
    )
    adapter = _CapturingDirectionalAdapter()

    with patch(
        "aats.data_platform.replay.core.replay_runner.load_gold_bars",
        return_value=[bar],
    ) as load_gold_bars:
        run_replay(
            MagicMock(),
            adapter=adapter,
            symbol=bar.symbol,
            timeframe=" 1H ",
            dataset_version="v1",
            start_ts=ts,
            end_ts=ts + timedelta(days=1),
        )

    assert load_gold_bars.call_args.kwargs["timeframe"] == "1h"
    assert adapter.timeframe == "1h"


def test_run_replay_rejects_invalid_timeframe_before_loading_gold() -> None:
    ts = datetime(2026, 8, 27, tzinfo=timezone.utc)
    adapter = _CapturingDirectionalAdapter()

    with (
        patch(
            "aats.data_platform.replay.core.replay_runner.load_gold_bars"
        ) as load_gold_bars,
        pytest.raises(ValueError, match="Unsupported replay timeframe"),
    ):
        run_replay(
            MagicMock(),
            adapter=adapter,
            symbol="BTC-USDT",
            timeframe="calendar-month",
            dataset_version="v1",
            start_ts=ts,
            end_ts=ts + timedelta(days=1),
        )

    load_gold_bars.assert_not_called()


def test_run_replay_rejects_unfinished_bar_before_adapter_evaluation() -> None:
    ts = datetime(2026, 8, 27, tzinfo=timezone.utc)
    bar = ReplayBar(
        symbol="BTC-USDT",
        ts=ts,
        open=Decimal("1"),
        high=Decimal("1"),
        low=Decimal("1"),
        close=Decimal("1"),
        volume=Decimal("1"),
        quote_volume=Decimal("1"),
        is_closed=True,
        aligned_funding_rate=None,
        funding_source_ts=None,
    )
    adapter = _CapturingDirectionalAdapter()

    with (
        patch(
            "aats.data_platform.replay.core.replay_runner.load_gold_bars",
            return_value=[replace(bar, is_closed=False)],
        ),
        pytest.raises(ValueError, match="requires closed Gold bars"),
    ):
        run_replay(
            MagicMock(),
            adapter=adapter,
            symbol=bar.symbol,
            timeframe="1h",
            dataset_version="v1",
            start_ts=ts,
            end_ts=ts + timedelta(days=1),
        )

    assert adapter.params is None
