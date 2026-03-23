from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from aats.bootstrap.env_profiles import (
    load_profiled_dotenv_into_process,
    reset_profiled_dotenv_state,
    resolve_profile_dotenv_path,
)
from aats.bootstrap.settings import AATSSettings


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


def test_profile_templates_are_utf8_and_use_live_canonical_keys() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    supported_keys = {f"AATS_{name.upper()}" for name in AATSSettings.model_fields}
    expected_phrases = {
        ".env.spot": [
            "AATS_CONFIG_PROFILE=guarded_spot_enabled",
            "AATS_TRADING_PRODUCT_TYPE=spot",
            "AATS_AI_OPERATING_MODE",
            "AATS_TRIAL_GUARD_ENABLED",
        ],
        ".env.derivatives": [
            "AATS_CONFIG_PROFILE=guarded_derivatives_enabled",
            "AATS_TRADING_PRODUCT_TYPE=derivatives",
            "AATS_AI_OPERATING_MODE",
            "AATS_TRIAL_GUARD_ENABLED",
        ],
        ".env.spot.live": [
            "AATS_CONFIG_PROFILE=guarded_spot_enabled",
            "AATS_TRADING_PRODUCT_TYPE=spot",
            "AATS_OKX_SIMULATED_TRADING=false",
            "AATS_OPERATOR_SESSION_COOKIE_SECURE=true",
        ],
        ".env.derivatives.live": [
            "AATS_CONFIG_PROFILE=guarded_derivatives_enabled",
            "AATS_TRADING_PRODUCT_TYPE=derivatives",
            "AATS_OKX_SIMULATED_TRADING=false",
            "AATS_OPERATOR_SESSION_COOKIE_SECURE=true",
        ],
    }
    required_keys = {
        "AATS_EXECUTION_COMMAND_FLOW_ENABLED",
        "AATS_PORTFOLIO_LEDGER_TRUTH_ENABLED",
        "AATS_RECOVERY_RECONCILIATION_EXECUTION_LEDGER_ENABLED",
        "AATS_OPERATOR_CONTROL_PLANE_EXECUTION_LEDGER_ENABLED",
        "AATS_FINANCIAL_CONVERGENCE_MODE_ENABLED",
        "AATS_AI_MANUAL_OPERATING_MODE_OVERRIDE_FREEZE_SECONDS",
        "AATS_STRATEGY_PROFILE_AUTO_CONTROL_ENABLED",
        "AATS_STRATEGY_PROFILE_ACTIVATION_MIN_ACTIVE_MINUTES",
        "AATS_STRATEGY_PROFILE_ACTIVATION_MIN_SCORE_DELTA",
        "AATS_STRATEGY_PROFILE_ACTIVATION_REQUIRED_CONSECUTIVE_WINS",
        "AATS_STRATEGY_PROFILE_AUTO_SWITCH_MIN_CLOSED_TRADES",
        "AATS_STRATEGY_PROFILE_AUTO_SWITCH_MIN_REPLAY_VALIDATIONS",
        "AATS_STRATEGY_PROFILE_COLD_START_LOCK_ENABLED",
        "AATS_STRATEGY_PROFILE_SAFETY_PROFILES",
        "AATS_STRATEGY_PROFILE_SAFETY_TRIGGER_EXECUTION_ERROR_COUNT",
        "AATS_TRIAL_GUARD_ENABLED",
        "AATS_TRIAL_GUARD_POLL_INTERVAL_SECONDS",
        "AATS_TRIAL_GUARD_LOOKBACK_FILLS",
        "AATS_TRIAL_GUARD_MIN_CLOSED_FILLS",
        "AATS_TRIAL_GUARD_MAX_DAILY_LOSS_USDT",
        "AATS_TRIAL_GUARD_MAX_CONSECUTIVE_LOSSES",
        "AATS_TRIAL_GUARD_MAX_FEE_TO_NOTIONAL_RATIO",
        "AATS_TRIAL_GUARD_MAX_HIGH_SLIPPAGE_RATIO",
        "AATS_TRIAL_GUARD_MAX_SLOW_SUBMIT_TO_FILL_RATIO",
    }

    for env_name, phrases in expected_phrases.items():
        text = (repo_root / env_name).read_text(encoding="utf-8")
        assert "\ufffd" not in text
        assert "AATS_AI_PRIMARY_" not in text
        for phrase in phrases:
            assert phrase in text
        for key in required_keys:
            assert f"{key}=" in text, key
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key = stripped.split("=", 1)[0].strip()
            assert key in supported_keys, key


def test_profile_templates_use_distinct_parallel_runtime_defaults() -> None:
    repo_root = Path(__file__).resolve().parents[2]

    def load_env_file(path: Path) -> dict[str, str]:
        values: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            values[key] = value
        return values

    spot = load_env_file(repo_root / ".env.spot")
    derivatives = load_env_file(repo_root / ".env.derivatives")
    spot_live = load_env_file(repo_root / ".env.spot.live")
    derivatives_live = load_env_file(repo_root / ".env.derivatives.live")

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
