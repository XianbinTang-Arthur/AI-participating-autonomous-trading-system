"""FS-015: independent replay 与生产 short-bias gate 一致性回归。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest

from aats.cli import _parse_param_overrides
from aats.bootstrap.active_parameters import (
    _RDP_REPLAY_ONLY_PARAMS,
    PARAMETER_MAPPING_DIRECTIONAL,
    PARAMETER_MAPPING_INDEPENDENT,
)
from aats.data_platform.replay.adapters.independent_adapter import IndependentReplayAdapter
from aats.data_platform.replay.core.replay_context import (
    ReplayBar,
    ReplayBarContext,
    ReplayCostConfig,
    ReplayParameterOverrides,
)
from aats.services.strategy_engines.independent.scoring import compute_raw_book_score
from tests.support.strategy_family import make_baseline, make_derivatives_hedge_settings


def _bar(*, minute: int, open_: str = "100", close: str = "100") -> ReplayBar:
    open_value = Decimal(open_)
    close_value = Decimal(close)
    return ReplayBar(
        symbol="BTC-USDT-SWAP",
        ts=datetime(2026, 8, 25, tzinfo=timezone.utc) + timedelta(minutes=minute),
        open=open_value,
        high=max(open_value, close_value) + Decimal("1"),
        low=min(open_value, close_value) - Decimal("1"),
        close=close_value,
        volume=Decimal("1000"),
        quote_volume=Decimal("100000"),
        is_closed=True,
        aligned_funding_rate=None,
        funding_source_ts=None,
    )


def _params(*, short_enabled: bool) -> ReplayParameterOverrides:
    return ReplayParameterOverrides(
        strategy_short_bias_enabled=short_enabled,
        min_confirm_ticks=1,
        entry_threshold=0.10,
        close_threshold=0.05,
        scale_in_threshold=0.40,
        min_safe_net_edge_bps=0.0,
        de_risk_net_edge_bps=-1.0,
        failed_thesis_net_edge_bps=-2.0,
        expected_slippage_buffer_bps=0.0,
        expected_execution_buffer_bps=0.0,
        noise_buffer_bps=0.0,
        cost_config=ReplayCostConfig(
            taker_fee_bps=0.0,
            slippage_bps=0.0,
            maker_fee_bps=0.0,
        ),
    )


def _context(
    adapter: IndependentReplayAdapter,
    *,
    params: ReplayParameterOverrides,
    bar: ReplayBar,
    index: int,
) -> ReplayBarContext:
    state = adapter.reset_state()
    return ReplayBarContext(
        bar=bar,
        bar_index=index,
        state=state,
        params=params,
        family="independent",
        symbol=bar.symbol,
        timeframe="15m",
        dataset_version="fs015-golden-vector",
    )


def test_short_bias_parameter_default_and_round_trip_are_explicit() -> None:
    params = ReplayParameterOverrides()

    assert params.strategy_short_bias_enabled is True
    assert params.to_dict()["strategy_short_bias_enabled"] is True
    assert (
        ReplayParameterOverrides.from_dict(params.to_dict()).strategy_short_bias_enabled
        is True
    )


def test_short_bias_false_round_trips_into_replay_artifact_parameters() -> None:
    params = ReplayParameterOverrides.from_dict({"strategy_short_bias_enabled": False})

    assert params.strategy_short_bias_enabled is False
    assert params.to_dict()["strategy_short_bias_enabled"] is False


@pytest.mark.parametrize("invalid", [0, 1, "false", [], {}])
def test_direct_short_bias_non_boolean_values_fail_closed(invalid: object) -> None:
    with pytest.raises(ValueError, match="strategy_short_bias_enabled 必须是 bool"):
        ReplayParameterOverrides(strategy_short_bias_enabled=invalid)  # type: ignore[arg-type]


@pytest.mark.parametrize("invalid", [0, 1, "false", [], {}])
def test_deserialized_short_bias_non_boolean_values_fail_closed(invalid: object) -> None:
    with pytest.raises(ValueError, match="必须是 JSON boolean"):
        ReplayParameterOverrides.from_dict({"strategy_short_bias_enabled": invalid})


def test_cli_json_false_reaches_typed_replay_parameter() -> None:
    raw = _parse_param_overrides(["strategy_short_bias_enabled=false"])
    params = ReplayParameterOverrides.from_dict(raw)

    assert raw == {"strategy_short_bias_enabled": False}
    assert params.strategy_short_bias_enabled is False


def test_short_bias_is_replay_context_not_active_parameter_mapping() -> None:
    key = "strategy_short_bias_enabled"

    assert key in _RDP_REPLAY_ONLY_PARAMS
    assert key not in PARAMETER_MAPPING_INDEPENDENT
    assert key not in PARAMETER_MAPPING_DIRECTIONAL


def test_disabled_gate_skips_short_scoring_before_history_and_dominance() -> None:
    adapter = IndependentReplayAdapter()
    ctx = _context(
        adapter,
        params=_params(short_enabled=False),
        bar=_bar(minute=0),
        index=0,
    )

    with patch.object(
        adapter,
        "_compute_book_score",
        side_effect=lambda _bar_value, *, leg: 0.25 if leg == "long" else 0.95,
    ) as score:
        decision = adapter.evaluate_bar(ctx)

    assert score.call_count == 1
    assert score.call_args.kwargs == {"leg": "long"}
    assert decision.long_score == 0.25
    assert decision.short_score == 0.0
    assert list(adapter._short_score_history) == [0.0]
    assert ctx.state.position_side == "long"


def test_bearish_vector_can_select_short_only_when_gate_is_enabled() -> None:
    first = _bar(minute=0, open_="100", close="99")
    bearish = _bar(minute=15, open_="99", close="85")

    enabled_adapter = IndependentReplayAdapter()
    enabled_state = enabled_adapter.reset_state()
    enabled_adapter.evaluate_bar(
        ReplayBarContext(
            bar=first,
            bar_index=0,
            state=enabled_state,
            params=_params(short_enabled=True),
            family="independent",
            symbol=first.symbol,
            timeframe="15m",
            dataset_version="fs015-golden-vector",
        )
    )
    enabled_decision = enabled_adapter.evaluate_bar(
        ReplayBarContext(
            bar=bearish,
            bar_index=1,
            state=enabled_state,
            params=_params(short_enabled=True),
            family="independent",
            symbol=bearish.symbol,
            timeframe="15m",
            dataset_version="fs015-golden-vector",
        )
    )

    disabled_adapter = IndependentReplayAdapter()
    disabled_state = disabled_adapter.reset_state()
    disabled_adapter.evaluate_bar(
        ReplayBarContext(
            bar=first,
            bar_index=0,
            state=disabled_state,
            params=_params(short_enabled=False),
            family="independent",
            symbol=first.symbol,
            timeframe="15m",
            dataset_version="fs015-golden-vector",
        )
    )
    disabled_decision = disabled_adapter.evaluate_bar(
        ReplayBarContext(
            bar=bearish,
            bar_index=1,
            state=disabled_state,
            params=_params(short_enabled=False),
            family="independent",
            symbol=bearish.symbol,
            timeframe="15m",
            dataset_version="fs015-golden-vector",
        )
    )

    assert enabled_decision.short_score > enabled_decision.long_score
    assert enabled_state.position_side == "short"
    assert disabled_decision.short_score == 0.0
    assert disabled_state.position_side != "short"


def test_production_and_replay_share_disabled_short_score_contract() -> None:
    settings = make_derivatives_hedge_settings(
        strategy_short_bias_enabled=False,
        ai_operating_mode="baseline_only",
    )
    baseline = make_baseline(
        direction_bias="short",
        confidence=0.90,
        suggested_position_scale=1.0,
        volatility_target_scale=1.0,
        factor_scores={
            "momentum_alpha": -0.70,
            "trend_alpha": -0.65,
            "microstructure_alpha": -0.55,
        },
    ).model_copy(update={"regime": "trend", "composite_alpha_score": -0.75})

    production_score = compute_raw_book_score(
        settings=settings,
        leg="short",
        baseline=baseline,
        ai_assessment=None,
    )

    adapter = IndependentReplayAdapter()
    ctx = _context(
        adapter,
        params=_params(short_enabled=settings.strategy_short_bias_enabled),
        bar=_bar(minute=0),
        index=0,
    )
    with patch.object(
        adapter,
        "_compute_book_score",
        side_effect=lambda _bar_value, *, leg: 0.20 if leg == "long" else 0.99,
    ):
        replay_decision = adapter.evaluate_bar(ctx)

    assert production_score == 0.0
    assert replay_decision.short_score == production_score
