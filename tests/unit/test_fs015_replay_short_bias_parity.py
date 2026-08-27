"""FS-015: independent replay 与生产 short-bias gate 一致性回归。"""

from __future__ import annotations

import math
from dataclasses import fields, replace
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
from aats.data_platform.replay.adapters.directional_adapter import DirectionalReplayAdapter
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


def _decision_ts(bar: ReplayBar) -> datetime:
    return bar.ts + timedelta(minutes=15)


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
        observation_completed_at_ts=_decision_ts(bar),
        decision_ts=_decision_ts(bar),
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


def test_direct_numeric_dataclasses_reject_boolean_financial_values() -> None:
    with pytest.raises(ValueError, match="entry_threshold must be numeric"):
        ReplayParameterOverrides(entry_threshold=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="entry_threshold must be numeric"):
        ReplayParameterOverrides.from_dict({"entry_threshold": True})
    with pytest.raises(ValueError, match="taker_fee_bps must be numeric"):
        ReplayCostConfig(taker_fee_bps=True)  # type: ignore[arg-type]


def test_replay_parameter_numeric_fields_are_canonical_floats() -> None:
    params = ReplayParameterOverrides(
        score_stability_threshold=5,
        min_safe_net_edge_bps=2,
        signal_edge_scale_bps=12,
        directional_trend_weight=1,
        directional_return_clamp_bps=20,
        entry_threshold=1,
        close_threshold=0,
        scale_in_threshold=1,
        short_entry_threshold=1,
        short_close_threshold=0,
        min_hold_seconds=300,
        rebalance_cooldown_seconds=-0.0,
        max_thesis_age_seconds=1800,
        de_risk_net_edge_bps=2,
        failed_thesis_net_edge_bps=-1,
        catastrophic_failed_thesis_buffer_bps=3,
        expected_slippage_buffer_bps=1,
        expected_execution_buffer_bps=0,
        max_acceptable_cost_bps=7,
        min_score_drawdown_bps=6,
        min_liquidity_quality=1,
        limit_offset_bps_entry=Decimal("1"),
        noise_buffer_bps=2,
    )
    numeric_names = {
        field.name
        for field in fields(params)
        if field.name
        not in {
            "min_confirm_ticks",
            "strategy_short_bias_enabled",
            "cost_config",
            "extra",
        }
    }
    round_trip = ReplayParameterOverrides.from_dict(params.to_dict())

    assert all(type(getattr(params, name)) is float for name in numeric_names)
    assert all(
        type(getattr(round_trip, name)) is float for name in numeric_names
    )
    assert math.copysign(1.0, params.rebalance_cooldown_seconds) == 1.0


@pytest.mark.parametrize("invalid", ["0.3", [], {}, object()])
def test_replay_parameter_numeric_strings_and_non_float_types_fail_closed(
    invalid: object,
) -> None:
    with pytest.raises(ValueError, match="entry_threshold must be numeric"):
        ReplayParameterOverrides(entry_threshold=invalid)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="entry_threshold must be numeric"):
        ReplayParameterOverrides.from_dict({"entry_threshold": invalid})


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -float("inf")])
def test_replay_parameter_non_finite_values_fail_closed(invalid: float) -> None:
    with pytest.raises(ValueError, match="min_hold_seconds must be finite"):
        ReplayParameterOverrides(min_hold_seconds=invalid)
    with pytest.raises(ValueError, match="min_hold_seconds must be finite"):
        ReplayParameterOverrides.from_dict({"min_hold_seconds": invalid})


@pytest.mark.parametrize("invalid", [2.0, "2", True, None])
def test_min_confirm_ticks_is_a_strict_positive_integer(invalid: object) -> None:
    with pytest.raises(ValueError, match="min_confirm_ticks"):
        ReplayParameterOverrides(min_confirm_ticks=invalid)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="min_confirm_ticks"):
        ReplayParameterOverrides.from_dict({"min_confirm_ticks": invalid})


def test_optional_replay_parameters_are_none_or_canonical_float() -> None:
    params = ReplayParameterOverrides(
        short_entry_threshold=1,
        short_close_threshold=0,
        min_score_drawdown_bps=None,
    )

    assert type(params.short_entry_threshold) is float
    assert type(params.short_close_threshold) is float
    assert params.min_score_drawdown_bps is None
    with pytest.raises(ValueError, match="short_entry_threshold must be numeric"):
        ReplayParameterOverrides.from_dict({"short_entry_threshold": "0.3"})


def test_non_optional_replay_numeric_parameter_rejects_null() -> None:
    with pytest.raises(ValueError, match="min_hold_seconds must not be null"):
        ReplayParameterOverrides(min_hold_seconds=None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="min_hold_seconds must not be null"):
        ReplayParameterOverrides.from_dict({"min_hold_seconds": None})


@pytest.mark.parametrize("invalid", [None, [], ""])
def test_replay_parameter_extra_requires_exact_dict(invalid: object) -> None:
    with pytest.raises(ValueError, match="extra must be a dict"):
        ReplayParameterOverrides(extra=invalid)  # type: ignore[arg-type]


def test_replay_parameter_extra_rejects_dict_subclasses() -> None:
    class _CustomDict(dict[str, object]):
        pass

    with pytest.raises(ValueError, match="extra must be a dict"):
        ReplayParameterOverrides(extra=_CustomDict())


@pytest.mark.parametrize("extra", [{1: "value"}, {"": "value"}])
def test_replay_parameter_extra_requires_nonempty_exact_string_keys(
    extra: dict[object, object],
) -> None:
    with pytest.raises(ValueError, match="extra keys must be non-empty strings"):
        ReplayParameterOverrides(extra=extra)  # type: ignore[arg-type]


def test_replay_parameter_extra_is_detached_from_source_mutation() -> None:
    source = {"legacy": {"thresholds": [1]}}
    params = ReplayParameterOverrides(extra=source)

    source["legacy"]["thresholds"].append(2)
    source["new"] = "mutated"

    assert params.extra == {"legacy": {"thresholds": [1]}}
    assert params.extra is not source


def test_replay_cost_config_canonicalizes_every_signed_zero_field() -> None:
    config = ReplayCostConfig(
        taker_fee_bps=-0.0,
        slippage_bps=-0.0,
        maker_fee_bps=-0.0,
        passive_bias=-0.0,
        maker_taker_bias=-0.0,
    )

    for field_name in (
        "taker_fee_bps",
        "slippage_bps",
        "maker_fee_bps",
        "passive_bias",
        "maker_taker_bias",
    ):
        assert repr(getattr(config, field_name)) == "0.0"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"taker_fee_bps": -0.1}, "replay_taker_fee_bps_out_of_range"),
        ({"slippage_bps": -0.1}, "replay_slippage_bps_out_of_range"),
        ({"maker_fee_bps": -10_000}, "replay_maker_fee_bps_out_of_range"),
        ({"passive_bias": 1.01}, "replay_passive_bias_out_of_range"),
        ({"maker_taker_bias": -1.01}, "replay_maker_taker_bias_out_of_range"),
        ({"execution_style": "unknown"}, "replay_execution_style_unsupported"),
    ],
)
def test_direct_cost_config_rejects_invalid_financial_contract(
    overrides: dict[str, object],
    reason: str,
) -> None:
    with pytest.raises(ValueError, match=reason):
        ReplayCostConfig(**overrides)  # type: ignore[arg-type]


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


def test_directional_adapter_cannot_select_short_when_gate_is_disabled() -> None:
    adapter = DirectionalReplayAdapter()
    state = adapter.reset_state()
    bar = _bar(minute=0)
    ctx = ReplayBarContext(
        bar=bar,
        bar_index=0,
        state=state,
        params=_params(short_enabled=False),
        family="directional",
        symbol=bar.symbol,
        timeframe="15m",
        dataset_version="fs015-golden-vector",
        observation_completed_at_ts=_decision_ts(bar),
        decision_ts=_decision_ts(bar),
    )

    with patch.object(adapter, "_compute_scores", return_value=(0.2, 0.99)):
        decision = adapter.evaluate_bar(ctx)

    assert decision.short_score == 0.0
    assert state.position_side != "short"


@pytest.mark.parametrize(
    "adapter",
    [IndependentReplayAdapter(), DirectionalReplayAdapter()],
)
def test_close_decision_uses_actual_partial_position_quantity(adapter) -> None:
    state = adapter.reset_state()
    bar = _bar(minute=60)
    state.position_side = "long"
    state.position_qty = Decimal("0.25")
    state.entry_price = Decimal("100")
    state.entry_ts = bar.ts - timedelta(minutes=30)
    params = replace(_params(short_enabled=False), min_hold_seconds=0.0)
    ctx = ReplayBarContext(
        bar=bar,
        bar_index=1,
        state=state,
        params=params,
        family=adapter.family_name,
        symbol=bar.symbol,
        timeframe="15m",
        dataset_version="fs015-golden-vector",
        observation_completed_at_ts=_decision_ts(bar),
        decision_ts=_decision_ts(bar),
    )

    score_method = (
        "_compute_scores"
        if isinstance(adapter, DirectionalReplayAdapter)
        else "_compute_book_score"
    )
    score_value = (0.0, 0.0) if score_method == "_compute_scores" else 0.0
    with patch.object(adapter, score_method, return_value=score_value):
        decision = adapter.evaluate_bar(ctx)

    assert decision.action == "close"
    assert decision.target_position_qty == Decimal("0")
    assert decision.delta_position_qty == Decimal("-0.25")


@pytest.mark.parametrize(
    "adapter_cls",
    [IndependentReplayAdapter, DirectionalReplayAdapter],
)
def test_rebalance_cooldown_uses_decision_time(adapter_cls) -> None:
    adapter = adapter_cls()
    state = adapter.reset_state()
    bar = _bar(minute=60)
    state.last_close_ts = bar.ts + timedelta(minutes=10)
    params = replace(
        _params(short_enabled=False),
        rebalance_cooldown_seconds=300.0,
    )
    ctx = ReplayBarContext(
        bar=bar,
        bar_index=1,
        state=state,
        params=params,
        family=adapter.family_name,
        symbol=bar.symbol,
        timeframe="15m",
        dataset_version="fs015-golden-vector",
        observation_completed_at_ts=_decision_ts(bar),
        decision_ts=_decision_ts(bar),
    )

    score_method = (
        "_compute_scores"
        if isinstance(adapter, DirectionalReplayAdapter)
        else "_compute_book_score"
    )
    score_value = (0.9, 0.0) if score_method == "_compute_scores" else 0.9
    with patch.object(adapter, score_method, return_value=score_value):
        decision = adapter.evaluate_bar(ctx)

    assert decision.action == "open"
    assert "rebalance_cooldown" not in decision.blocking_reasons
    assert state.entry_ts == _decision_ts(bar)


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
            observation_completed_at_ts=_decision_ts(first),
            decision_ts=_decision_ts(first),
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
            observation_completed_at_ts=_decision_ts(bearish),
            decision_ts=_decision_ts(bearish),
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
            observation_completed_at_ts=_decision_ts(first),
            decision_ts=_decision_ts(first),
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
            observation_completed_at_ts=_decision_ts(bearish),
            decision_ts=_decision_ts(bearish),
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
