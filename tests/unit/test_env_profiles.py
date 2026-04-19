from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from aats.bootstrap.env_profiles import (
    load_profiled_dotenv_into_process,
    reset_profiled_dotenv_state,
    resolve_profile_dotenv_path,
)
from aats.bootstrap.managed_profiles import (
    MANAGED_PROFILE_DEFINITIONS,
    MANAGED_PROFILE_DERIVED_ENV_KEYS,
    load_managed_profile_values,
)
from aats.bootstrap.settings import AATSSettings
from tests.support.postgres import bootstrap_postgres_test_env, postgres_example_url


def test_resolve_profile_dotenv_path_uses_named_profile(tmp_path: Path) -> None:
    assert resolve_profile_dotenv_path(tmp_path, "spot") == tmp_path / ".env.spot"
    assert resolve_profile_dotenv_path(tmp_path, "derivatives") == tmp_path / ".env.derivatives"
    assert resolve_profile_dotenv_path(tmp_path, "spot_live") == tmp_path / ".env.spot.live"
    assert resolve_profile_dotenv_path(tmp_path, "derivatives_live") == tmp_path / ".env.derivatives.live"


def test_resolve_profile_dotenv_path_requires_profile(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="startup_profile_required"):
        resolve_profile_dotenv_path(tmp_path, None)


def test_load_profiled_dotenv_into_process_preserves_external_aats_values(tmp_path: Path) -> None:
    dotenv_path = tmp_path / ".env.spot.live"
    dotenv_path.write_text(
        "AATS_MODE=guarded_live\n"
        "AATS_OKX_API_KEY=placeholder-key\n"
        "AATS_DEFAULT_SYMBOL=BTC-USDT\n",
        encoding="utf-8",
    )
    reset_profiled_dotenv_state()
    with patch.dict(
        os.environ,
        {
            "AATS_MODE": "paper_live",
            "AATS_OKX_API_KEY": "external-secret",
            "UNRELATED_ENV": "keep",
        },
        clear=True,
    ):
        loaded = load_profiled_dotenv_into_process(tmp_path, "spot_live")

        assert loaded == dotenv_path
        assert os.environ["AATS_MODE"] == "paper_live"
        assert os.environ["AATS_OKX_API_KEY"] == "external-secret"
        assert os.environ["AATS_DEFAULT_SYMBOL"] == "BTC-USDT"
        assert os.environ["AATS_STARTUP_PROFILE"] == "spot"
        assert os.environ["AATS_ENV_TEMPLATE_PROFILE"] == "spot_live"
        assert os.environ["UNRELATED_ENV"] == "keep"


def test_load_profiled_dotenv_into_process_replaces_prior_profile_managed_values(tmp_path: Path) -> None:
    (tmp_path / ".env.spot.live").write_text(
        "AATS_MODE=guarded_live\nAATS_DEFAULT_SYMBOL=BTC-USDT\nAATS_ALLOWED_SYMBOLS=[\"BTC-USDT\"]\n",
        encoding="utf-8",
    )
    (tmp_path / ".env.derivatives.live").write_text(
        "AATS_MODE=guarded_live\nAATS_DEFAULT_SYMBOL=BTC-USDT-SWAP\n",
        encoding="utf-8",
    )
    reset_profiled_dotenv_state()
    with patch.dict(os.environ, {}, clear=True):
        load_profiled_dotenv_into_process(tmp_path, "spot_live")
        assert os.environ["AATS_DEFAULT_SYMBOL"] == "BTC-USDT"
        assert os.environ["AATS_ALLOWED_SYMBOLS"] == "[\"BTC-USDT\"]"

        load_profiled_dotenv_into_process(tmp_path, "derivatives_live")

        assert os.environ["AATS_DEFAULT_SYMBOL"] == "BTC-USDT-SWAP"
        assert "AATS_ALLOWED_SYMBOLS" not in os.environ
        assert os.environ["AATS_STARTUP_PROFILE"] == "derivatives"
        assert os.environ["AATS_ENV_TEMPLATE_PROFILE"] == "derivatives_live"


def test_bootstrap_postgres_test_env_loads_database_url_from_explicit_profile_when_missing(tmp_path: Path) -> None:
    expected = postgres_example_url(database_name="aats_live_derivatives")
    dotenv_path = tmp_path / ".env.derivatives.live"
    dotenv_path.write_text(
        f"AATS_DATABASE_URL={expected}\n",
        encoding="utf-8",
    )
    with patch.dict(os.environ, {}, clear=True):
        loaded = bootstrap_postgres_test_env(project_root=tmp_path, profile="derivatives_live")

        assert loaded == expected
        assert os.environ["AATS_DATABASE_URL"] == loaded
        assert os.environ["AATS_ENV_TEMPLATE_PROFILE"] == "derivatives_live"
        assert os.environ["AATS_STARTUP_PROFILE"] == "derivatives"


def test_bootstrap_postgres_test_env_defaults_to_local_derivatives_live_profile(tmp_path: Path) -> None:
    expected = postgres_example_url(database_name="aats_local_derivatives_live")
    (tmp_path / ".env.derivatives.live").write_text(
        f"AATS_DATABASE_URL={expected}\n",
        encoding="utf-8",
    )
    with patch.dict(os.environ, {}, clear=True):
        loaded = bootstrap_postgres_test_env(project_root=tmp_path)

        assert loaded == expected
        assert os.environ["AATS_DATABASE_URL"] == loaded
        assert os.environ["AATS_ENV_TEMPLATE_PROFILE"] == "derivatives_live"
        assert os.environ["AATS_STARTUP_PROFILE"] == "derivatives"


def test_bootstrap_postgres_test_env_prefers_explicit_test_database_url(tmp_path: Path) -> None:
    expected = postgres_example_url(database_name="test_only")
    with patch.dict(
        os.environ,
        {"AATS_TEST_DATABASE_URL": expected},
        clear=True,
    ):
        loaded = bootstrap_postgres_test_env(project_root=tmp_path)

        assert loaded == expected
        assert os.environ["AATS_DATABASE_URL"] == loaded
        assert "AATS_ENV_TEMPLATE_PROFILE" not in os.environ
        assert "AATS_STARTUP_PROFILE" not in os.environ


def test_bootstrap_postgres_test_env_loads_database_url_from_dedicated_test_dotenv(tmp_path: Path) -> None:
    expected = postgres_example_url(database_name="aats_test_runtime")
    (tmp_path / ".env.test.postgres").write_text(
        f"AATS_DATABASE_URL={expected}\n",
        encoding="utf-8",
    )
    with patch.dict(os.environ, {}, clear=True):
        loaded = bootstrap_postgres_test_env(project_root=tmp_path)

        assert loaded == expected
        assert os.environ["AATS_DATABASE_URL"] == loaded
        assert "AATS_ENV_TEMPLATE_PROFILE" not in os.environ
        assert "AATS_STARTUP_PROFILE" not in os.environ


def test_bootstrap_postgres_test_env_preserves_existing_database_url(tmp_path: Path) -> None:
    profile_url = postgres_example_url(database_name="aats_live_derivatives")
    expected = postgres_example_url(database_name="external")
    (tmp_path / ".env.derivatives.live").write_text(
        f"AATS_DATABASE_URL={profile_url}\n",
        encoding="utf-8",
    )
    with patch.dict(
        os.environ,
        {"AATS_DATABASE_URL": expected},
        clear=True,
    ):
        loaded = bootstrap_postgres_test_env(project_root=tmp_path)

        assert loaded == expected
        assert os.environ["AATS_DATABASE_URL"] == loaded
        assert "AATS_ENV_TEMPLATE_PROFILE" not in os.environ
        assert "AATS_STARTUP_PROFILE" not in os.environ


def _load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key] = value
    return values


def test_managed_profile_local_env_templates_are_minimal_utf8_overrides() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    supported_keys = {f"AATS_{name.upper()}" for name in AATSSettings.model_fields}
    common_required_keys = {
        "AATS_DEFAULT_SYMBOL",
        "AATS_ALLOWED_SYMBOLS",
        "AATS_INITIAL_USDT_BALANCE",
        # AATS_DATABASE_URL 已由 9918e48 移除，改为 AATS_DB_NAME +
        # docker-compose 内部组装（参见 deploy/wsl2-dev/docker-compose.aats.yml）。
        "AATS_DATABASE_RUNTIME_LOCK_KEY",
        "AATS_API_PORT",
        "AATS_LOG_DIR",
        "AATS_OPENAI_API_KEY",
        "AATS_OKX_API_KEY",
        "AATS_OKX_API_SECRET",
        "AATS_OKX_API_PASSPHRASE",
        "AATS_OPERATOR_SESSION_SECRET",
        "AATS_OPERATOR_SESSION_COOKIE_NAME",
    }
    deprecated_strategy_keys = {
        "AATS_AI_OPERATING_MODE",
        "AATS_TRIAL_GUARD_ENABLED",
        "AATS_STRATEGY_PROFILE_AUTO_CONTROL_ENABLED",
        "AATS_PRIMARY_TIMEFRAME",
        "AATS_SECONDARY_TIMEFRAME",
    }
    profile_specific_required = {
        ".env.spot": {
            "AATS_DEFAULT_ORDER_QTY",
            "AATS_MAX_ABS_POSITION_QTY",
            "AATS_MAX_NOTIONAL_PER_SYMBOL",
            "AATS_MAX_OPEN_ORDERS",
        },
        ".env.spot.live": {
            "AATS_DEFAULT_ORDER_QTY",
            "AATS_MAX_ABS_POSITION_QTY",
            "AATS_MAX_NOTIONAL_PER_SYMBOL",
            "AATS_MAX_OPEN_ORDERS",
        },
        ".env.derivatives": {
            "AATS_DEFAULT_ORDER_QTY",
            "AATS_MAX_ABS_POSITION_QTY",
            "AATS_MAX_NOTIONAL_PER_SYMBOL",
            "AATS_MAX_OPEN_ORDERS",
            "AATS_MAX_TARGET_LEVERAGE",
            "AATS_DEFAULT_TARGET_LEVERAGE",
            "AATS_DERIVATIVES_ONLY_REDUCE_TRIGGER_MARGIN_FRACTION",
            "AATS_DERIVATIVES_AUTO_HALT_MARGIN_USAGE_FRACTION",
            "AATS_DERIVATIVES_AUTO_HALT_LIQUIDATION_GAP_FRACTION",
            "AATS_MAX_MARGIN_USAGE_FRACTION",
            "AATS_LIQUIDATION_BUFFER_FRACTION",
        },
        ".env.derivatives.live": {
            "AATS_DEFAULT_ORDER_QTY",
            "AATS_MAX_ABS_POSITION_QTY",
            "AATS_MAX_NOTIONAL_PER_SYMBOL",
            "AATS_MAX_OPEN_ORDERS",
            "AATS_MAX_TARGET_LEVERAGE",
            "AATS_DEFAULT_TARGET_LEVERAGE",
            "AATS_DERIVATIVES_ONLY_REDUCE_TRIGGER_MARGIN_FRACTION",
            "AATS_DERIVATIVES_AUTO_HALT_MARGIN_USAGE_FRACTION",
            "AATS_DERIVATIVES_AUTO_HALT_LIQUIDATION_GAP_FRACTION",
            "AATS_MAX_MARGIN_USAGE_FRACTION",
            "AATS_LIQUIDATION_BUFFER_FRACTION",
        },
    }
    allowed_profile_derived_overrides = {
        ".env.derivatives.live": {"AATS_DERIVATIVES_POSITION_MODE"},
    }
    spot_only_forbidden = {
        "AATS_MAX_TARGET_LEVERAGE",
        "AATS_DEFAULT_TARGET_LEVERAGE",
    }

    for env_name, specific_keys in profile_specific_required.items():
        text = (repo_root / env_name).read_text(encoding="utf-8")
        assert "\ufffd" not in text
        values = _load_env_file(repo_root / env_name)

        for key in common_required_keys | specific_keys:
            assert key in values, key
        for key in MANAGED_PROFILE_DERIVED_ENV_KEYS:
            if key in allowed_profile_derived_overrides.get(env_name, set()):
                continue
            assert key not in values, key
        for key in deprecated_strategy_keys:
            assert key not in values, key
        # Keys consumed by Docker compose / bootstrap scripts, not
        # AATSSettings runtime fields:
        # - AATS_DB_NAME: compose interpolation for DATABASE_URL
        # - AATS_OPERATOR_ADMIN_*: seed_operator_admin bootstrap script
        infrastructure_only_keys = {
            "AATS_DB_NAME",
            "AATS_OPERATOR_ADMIN_USERNAME",
            "AATS_OPERATOR_ADMIN_PASSWORD",
        }
        for key in values:
            assert key in supported_keys or key in infrastructure_only_keys, key
        if env_name.startswith(".env.spot"):
            for key in spot_only_forbidden:
                assert key not in values, key


def test_generated_managed_config_artifacts_exist_and_match_profile_layout() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    for profile, definition in MANAGED_PROFILE_DEFINITIONS.items():
        strategy_path = repo_root / definition.strategy_tuning_relative_path
        assert strategy_path.exists(), strategy_path
        data = yaml.safe_load(strategy_path.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        expected_active_family = "independent" if profile == "derivatives_live" else "directional"
        assert data["strategy_family_active"] == expected_active_family
        assert "ai_operating_mode" in data
        assert "max_decisions_per_minute" in data
        example_env = repo_root / "configs" / "templates" / f".env.{profile}.example"
        assert example_env.exists(), example_env
        values = _load_env_file(example_env)
        for key in MANAGED_PROFILE_DERIVED_ENV_KEYS:
            assert key not in values, key

    reference_doc = repo_root / "docs" / "configuration" / "managed-config-reference.md"
    assert reference_doc.exists()
    text = reference_doc.read_text(encoding="utf-8")
    assert "Managed Profile 配置说明" in text
    assert "configs/strategy_profiles/<profile>.yaml" in text
    assert "deprecated" in text

    configs_readme = repo_root / "configs" / "README.md"
    assert configs_readme.exists()
    assert "strategy_profiles" in configs_readme.read_text(encoding="utf-8")


def test_derivatives_managed_profiles_use_relaxed_directional_thresholds() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    for profile in ("derivatives", "derivatives_live"):
        values = load_managed_profile_values(profile, project_root=repo_root)

        assert values["strategy_entry_allowed_regimes"] == ["trend", "breakout", "range", "uncertain"]
        assert values["strategy_short_entry_allowed_regimes"] == ["trend", "breakout", "range", "uncertain"]
        expected_entry_alpha = 0.10 if profile == "derivatives_live" else 0.18
        # 2026-04-19 下调 derivatives_live.strategy_entry_confidence_min 0.55→0.50
        # (与 calibration 后 baseline.confidence 分布对齐, 详见
        # docs/review/allocator_budget_zero_root_cause_2026_04_19.md).
        expected_entry_confidence = 0.50 if profile == "derivatives_live" else 0.66
        assert values["strategy_entry_alpha_min"] == expected_entry_alpha
        assert values["strategy_entry_confidence_min"] == expected_entry_confidence
        assert values["strategy_scale_in_min_signal_edge_bps"] == 16.0
        assert values["strategy_scale_in_alpha_min"] == 0.22
        assert values["strategy_scale_in_confidence_min"] == 0.68
        assert values["strategy_reversal_min_signal_edge_bps"] == 20.0
        assert values["strategy_reversal_alpha_min"] == 0.28
        assert values["strategy_reversal_confidence_min"] == 0.72
        assert values["strategy_max_fee_drag_ratio"] == 0.48
        assert values["strategy_max_churn_ratio"] == 0.42
        assert values["strategy_low_edge_threshold_bps"] == 4.0
        assert values["strategy_low_edge_streak_limit"] == 4
        assert values["strategy_low_edge_cooldown_seconds"] == 900.0
        expected_overlay_mode = "independent" if profile == "derivatives_live" else "protective"
        assert values["strategy_hedge_overlay_mode"] == expected_overlay_mode
        expected_protective_enabled = profile != "derivatives_live"
        assert values["strategy_hedge_protective_enabled"] is expected_protective_enabled
        expected_opportunistic_enabled = False
        expected_opportunistic_rollout = "dry_run"
        assert values["strategy_hedge_opportunistic_enabled"] is expected_opportunistic_enabled
        assert values["strategy_hedge_opportunistic_rollout_stage"] == expected_opportunistic_rollout
        assert values["strategy_hedge_opportunistic_open_threshold"] == 0.62
        assert values["strategy_hedge_opportunistic_close_threshold"] == 0.46
        assert values["strategy_hedge_opportunistic_max_ratio"] == 0.35
        assert values["strategy_hedge_opportunistic_min_hold_seconds"] == 180.0
        assert values["strategy_hedge_opportunistic_rebalance_cooldown_seconds"] == 90.0
        assert values["strategy_hedge_opportunistic_max_fee_drag_ratio"] == 0.18
        assert values["strategy_hedge_opportunistic_max_churn_ratio"] == 0.22
        assert values["strategy_hedge_opportunistic_min_safe_net_edge_bps"] == 3.0
        assert values["strategy_hedge_opportunistic_expected_slippage_buffer_bps"] == 1.0
        assert values["strategy_hedge_opportunistic_expected_execution_buffer_bps"] == 2.0
        assert values["strategy_hedge_opportunistic_weak_edge_execution_mode"] == "report_only"
        assert values["strategy_hedge_opportunistic_max_acceptable_cost_bps"] == 7.5
        assert values["strategy_hedge_opportunistic_passive_first_enabled"] is True
        expected_independent_enabled = profile == "derivatives_live"
        expected_independent_rollout = "live" if profile == "derivatives_live" else "dry_run"
        expected_independent_family_enabled = profile == "derivatives_live"
        assert values["strategy_family_protective_enabled"] is False
        assert values["strategy_family_protective_shadow_mode_enabled"] is False
        assert values["strategy_family_protective_live_execution_enabled"] is False
        assert values["strategy_family_opportunistic_enabled"] is False
        assert values["strategy_family_opportunistic_shadow_mode_enabled"] is False
        assert values["strategy_family_opportunistic_live_execution_enabled"] is False
        assert values["strategy_hedge_independent_enabled"] is expected_independent_enabled
        assert values["strategy_hedge_independent_rollout_stage"] == expected_independent_rollout
        assert values["strategy_family_independent_enabled"] is expected_independent_family_enabled
        assert values["strategy_family_independent_shadow_mode_enabled"] is False
        assert values["strategy_family_independent_live_execution_enabled"] is expected_independent_family_enabled
        # 2026-04-19 下调 derivatives_live 独立双书 entry_threshold 0.30→0.25
        # (与 DecisionEngine confidence_min=0.50 同步, 详见
        # docs/review/allocator_budget_zero_root_cause_2026_04_19.md).
        expected_independent_long_entry = 0.25 if profile == "derivatives_live" else 0.66
        expected_independent_short_entry = 0.25 if profile == "derivatives_live" else 0.66
        expected_independent_long_scale_in = 0.34 if profile == "derivatives_live" else 0.70
        # P2-6: short_scale_in_threshold 已对齐至 long=0.40 (原 0.36 与注释声明的"钉住值：0.40"不一致)
        expected_independent_short_scale_in = 0.40 if profile == "derivatives_live" else 0.70
        assert values["strategy_hedge_independent_long_entry_threshold"] == expected_independent_long_entry
        assert values["strategy_hedge_independent_short_entry_threshold"] == expected_independent_short_entry
        assert values["strategy_hedge_independent_long_scale_in_threshold"] == expected_independent_long_scale_in
        assert values["strategy_hedge_independent_short_scale_in_threshold"] == expected_independent_short_scale_in
        assert values["strategy_hedge_independent_long_min_hold_seconds"] == 300.0
        assert values["strategy_hedge_independent_short_min_hold_seconds"] == 300.0
        assert values["strategy_hedge_independent_rebalance_cooldown_seconds"] == 120.0
        assert values["strategy_hedge_independent_trial_guard_enabled"] is True
        assert values["strategy_hedge_independent_min_confirm_ticks"] == 2
        expected_independent_score_drawdown = 6.0 if profile == "derivatives_live" else 2.0
        assert values["strategy_hedge_independent_min_score_drawdown_bps"] == expected_independent_score_drawdown
        assert values["strategy_hedge_independent_min_liquidity_quality"] == 0.55
        assert values["strategy_hedge_independent_require_execution_health_ok"] is True
        assert values["strategy_hedge_independent_max_thesis_age_seconds"] == 1800
        assert values["strategy_hedge_independent_de_risk_net_edge_bps"] == 2.0
        assert values["strategy_hedge_independent_failed_thesis_net_edge_bps"] == -1.0
        assert values["strategy_hedge_independent_execution_health_de_risk_enabled"] is True
        assert values["strategy_hedge_independent_liquidity_de_risk_enabled"] is True
        assert values["strategy_hedge_independent_entry_execution_mode"] == "passive_first"
        assert values["strategy_hedge_independent_scale_in_execution_mode"] == "bounded_limit"
        assert values["strategy_hedge_independent_de_risk_execution_mode"] == "bounded_taker"
        assert values["strategy_hedge_independent_close_failed_thesis_execution_mode"] == "aggressive_bounded_taker"
        assert values["strategy_hedge_independent_close_stale_execution_mode"] == "bounded_limit"
        assert values["strategy_hedge_independent_limit_offset_bps_entry"] == 1.5
        assert values["strategy_hedge_independent_limit_offset_bps_scale_in"] == 1.0
        assert values["strategy_hedge_independent_limit_offset_bps_stale_close"] == 0.8
        assert values["strategy_hedge_independent_emit_book_level_metrics"] is True
        assert values["strategy_hedge_independent_emit_expected_vs_realized_metrics"] is True
        assert values["strategy_hedge_independent_emit_close_reason_metrics"] is True
        assert values["strategy_hedge_independent_emit_execution_policy_metrics"] is True
        if profile == "derivatives_live":
            # 2026-04-19 等分位标定后 P0→P2.7 权重重分配的新值
            # (docs/calibration/baseline_weight_recalibration_2026_04_19.md).
            assert values["strategy_baseline_breakout_alpha_threshold"] == 0.06
            assert values["strategy_baseline_trend_alpha_threshold"] == 0.10
            assert values["strategy_baseline_range_alpha_threshold"] == 0.11
            assert values["strategy_baseline_uncertain_alpha_threshold"] == 0.18
            assert values["strategy_baseline_alignment_bonus"] == 0.03
            assert values["strategy_baseline_impulse_override_enabled"] is True
            assert values["strategy_baseline_impulse_alpha_min"] == 0.10
            assert values["strategy_baseline_impulse_microstructure_min"] == 0.25
            assert values["strategy_baseline_impulse_momentum_min"] == 0.00035
            assert values["strategy_baseline_impulse_range_ratio_min"] == 0.003
            assert values["strategy_baseline_impulse_body_ratio_min"] == 0.10
            assert values["strategy_baseline_impulse_require_mtf_alignment"] is True
            assert values["strategy_hedge_independent_long_close_threshold"] == 0.15
            assert values["strategy_hedge_independent_short_close_threshold"] == 0.15
            assert values["strategy_hedge_independent_min_safe_net_edge_bps"] == 2.0
            assert values["strategy_hedge_independent_expected_slippage_buffer_bps"] == 0.5
            assert values["strategy_hedge_independent_expected_execution_buffer_bps"] == 0.5
            assert values["strategy_hedge_independent_weak_edge_execution_mode"] == "block"
            assert values["strategy_hedge_independent_max_acceptable_cost_bps"] == 7.5
            assert values["strategy_hedge_independent_passive_first_enabled"] is True


def test_derivatives_live_managed_profile_is_pinned_for_independent_live() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    values = load_managed_profile_values("derivatives_live", project_root=repo_root)

    assert values["derivatives_position_mode"] == "hedge"
    assert values["strategy_family_active"] == "independent"
    assert values["strategy_family_auto_selection_enabled"] is False
    assert values["strategy_family_independent_enabled"] is True
    assert values["strategy_family_independent_shadow_mode_enabled"] is False
    assert values["strategy_family_independent_live_execution_enabled"] is True
    assert values["strategy_family_protective_enabled"] is False
    assert values["strategy_family_protective_shadow_mode_enabled"] is False
    assert values["strategy_family_protective_live_execution_enabled"] is False
    assert values["strategy_family_opportunistic_enabled"] is False
    assert values["strategy_family_opportunistic_shadow_mode_enabled"] is False
    assert values["strategy_family_opportunistic_live_execution_enabled"] is False
    assert values["smart_arbitrage_enabled"] is False
    assert values["strategy_hedge_overlay_mode"] == "independent"
    assert values["strategy_entry_alpha_min"] == 0.10
    # 2026-04-19 下调 0.55→0.50 与 calibration 对齐
    assert values["strategy_entry_confidence_min"] == 0.50
    # 2026-04-19 等分位标定后 P0→P2.7 权重重分配的新值
    # (docs/calibration/baseline_weight_recalibration_2026_04_19.md).
    assert values["strategy_baseline_breakout_alpha_threshold"] == 0.06
    assert values["strategy_baseline_trend_alpha_threshold"] == 0.10
    assert values["strategy_baseline_range_alpha_threshold"] == 0.11
    assert values["strategy_baseline_uncertain_alpha_threshold"] == 0.18
    assert values["strategy_baseline_alignment_bonus"] == 0.03
    assert values["strategy_baseline_impulse_override_enabled"] is True
    assert values["strategy_baseline_impulse_alpha_min"] == 0.10
    assert values["strategy_baseline_impulse_microstructure_min"] == 0.25
    assert values["strategy_baseline_impulse_momentum_min"] == 0.00035
    assert values["strategy_baseline_impulse_range_ratio_min"] == 0.003
    assert values["strategy_baseline_impulse_body_ratio_min"] == 0.10
    assert values["strategy_baseline_impulse_require_mtf_alignment"] is True
    # 2026-04-19 下调 0.30→0.25 与 calibration 对齐
    assert values["strategy_hedge_independent_long_entry_threshold"] == 0.25
    assert values["strategy_hedge_independent_short_entry_threshold"] == 0.25
    assert values["strategy_hedge_independent_long_scale_in_threshold"] == 0.34
    assert values["strategy_hedge_independent_short_scale_in_threshold"] == 0.40
    assert values["strategy_hedge_independent_long_close_threshold"] == 0.15
    assert values["strategy_hedge_independent_short_close_threshold"] == 0.15
    assert values["strategy_hedge_independent_min_confirm_ticks"] == 2
    assert values["strategy_health_lookback_window_seconds"] == 14400.0
    assert values["strategy_hedge_independent_min_score_drawdown_bps"] == 6.0
    assert values["strategy_hedge_independent_min_liquidity_quality"] == 0.55
    assert values["strategy_hedge_independent_require_execution_health_ok"] is True
    assert values["strategy_hedge_independent_max_thesis_age_seconds"] == 1800
    assert values["strategy_hedge_independent_de_risk_net_edge_bps"] == 2.0
    assert values["strategy_hedge_independent_failed_thesis_net_edge_bps"] == -1.0
    assert values["strategy_hedge_independent_execution_health_de_risk_enabled"] is True
    assert values["strategy_hedge_independent_liquidity_de_risk_enabled"] is True
    assert values["strategy_hedge_independent_entry_execution_mode"] == "passive_first"
    assert values["strategy_hedge_independent_scale_in_execution_mode"] == "bounded_limit"
    assert values["strategy_hedge_independent_de_risk_execution_mode"] == "bounded_taker"
    assert values["strategy_hedge_independent_close_failed_thesis_execution_mode"] == "aggressive_bounded_taker"
    assert values["strategy_hedge_independent_close_stale_execution_mode"] == "bounded_limit"
    assert values["strategy_hedge_independent_limit_offset_bps_entry"] == 1.5
    assert values["strategy_hedge_independent_limit_offset_bps_scale_in"] == 1.0
    assert values["strategy_hedge_independent_limit_offset_bps_stale_close"] == 0.8
    assert values["strategy_hedge_independent_emit_book_level_metrics"] is True
    assert values["strategy_hedge_independent_emit_expected_vs_realized_metrics"] is True
    assert values["strategy_hedge_independent_emit_close_reason_metrics"] is True
    assert values["strategy_hedge_independent_emit_execution_policy_metrics"] is True
    assert values["strategy_hedge_independent_adaptive_rollout_enabled"] is False
    assert values["strategy_hedge_independent_health_enforcement_enabled"] is False
    assert values["strategy_hedge_independent_size_down_entry_enabled"] is False
    assert values["strategy_hedge_independent_long_short_asymmetry_enabled"] is False
    assert values["strategy_hedge_independent_short_asymmetry_penalty_multiplier"] == 0.85
    assert values["strategy_hedge_independent_entry_size_down_floor"] == 0.50
    assert values["strategy_hedge_independent_min_safe_net_edge_bps"] == 2.0
    assert values["strategy_hedge_independent_expected_slippage_buffer_bps"] == 0.5
    assert values["strategy_hedge_independent_expected_execution_buffer_bps"] == 0.5
    assert values["strategy_hedge_independent_weak_edge_execution_mode"] == "block"
    assert values["strategy_hedge_independent_max_acceptable_cost_bps"] == 7.5
    assert values["strategy_hedge_independent_passive_first_enabled"] is True


def test_managed_profiles_drop_legacy_cross_runtime_strategy_tuning() -> None:
    repo_root = Path(__file__).resolve().parents[2]

    for profile in ("spot", "spot_live"):
        values = load_managed_profile_values(profile, project_root=repo_root)

        assert "smart_arbitrage_enabled" not in values
        assert "smart_arbitrage_negative_basis_mode" not in values
        assert "strategy_short_bias_enabled" not in values
        assert "strategy_dynamic_leverage_enabled" not in values
        assert "strategy_short_entry_allowed_regimes" not in values
        assert "strategy_short_entry_min_signal_edge_bps" not in values
        assert "strategy_short_reversal_confidence_min" not in values

    for profile in ("derivatives", "derivatives_live"):
        values = load_managed_profile_values(profile, project_root=repo_root)

        assert "spot_grid_enabled" not in values
        assert "spot_grid_band_bps" not in values
        assert "dca_enabled" not in values
        assert "dca_pullback_entry_bps" not in values


def test_profile_templates_use_distinct_parallel_runtime_defaults() -> None:
    repo_root = Path(__file__).resolve().parents[2]

    spot = _load_env_file(repo_root / ".env.spot")
    derivatives = _load_env_file(repo_root / ".env.derivatives")
    spot_live = _load_env_file(repo_root / ".env.spot.live")
    derivatives_live = _load_env_file(repo_root / ".env.derivatives.live")

    assert spot["AATS_API_PORT"] != derivatives["AATS_API_PORT"]
    assert spot["AATS_LOG_DIR"] != derivatives["AATS_LOG_DIR"]
    assert spot["AATS_OPERATOR_SESSION_COOKIE_NAME"] != derivatives["AATS_OPERATOR_SESSION_COOKIE_NAME"]
    assert spot_live["AATS_API_PORT"] != derivatives_live["AATS_API_PORT"]
    assert (
        spot_live["AATS_OPERATOR_SESSION_COOKIE_NAME"]
        != derivatives_live["AATS_OPERATOR_SESSION_COOKIE_NAME"]
    )


def test_ai_config_view_is_utf8_and_only_exposes_supported_controls() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    text = (repo_root / "aats" / "api" / "static" / "modules" / "views" / "ai-config-view.js").read_text(
        encoding="utf-8"
    )

    assert "\ufffd" not in text
    assert "运行模式切换" in text
    assert "自动换档控制" in text
    assert "策略档位切换" in text
    assert "运行参数概览" in text
    assert "策略层 shadow" in text
    assert "执行层 shadow" in text
    assert "前往 AI 工作台" not in text
    assert "前往 AI 分析" not in text
