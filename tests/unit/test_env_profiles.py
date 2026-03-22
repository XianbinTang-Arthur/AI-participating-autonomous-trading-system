from __future__ import annotations

import os
from pathlib import Path

import pytest

from aats.bootstrap.env_profiles import load_profiled_dotenv_into_process, resolve_profile_dotenv_path
from aats.bootstrap.settings import AATSSettings


def test_resolve_profile_dotenv_path_uses_named_profile(tmp_path: Path) -> None:
    assert resolve_profile_dotenv_path(tmp_path, "spot") == tmp_path / ".env.spot"
    assert resolve_profile_dotenv_path(tmp_path, "derivatives") == tmp_path / ".env.derivatives"


def test_resolve_profile_dotenv_path_requires_profile(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="startup_profile_required"):
        resolve_profile_dotenv_path(tmp_path, None)


def test_load_profiled_dotenv_into_process_clears_previous_aats_keys(tmp_path: Path) -> None:
    dotenv_path = tmp_path / ".env.spot"
    dotenv_path.write_text("AATS_MODE=guarded_live\nAATS_DEFAULT_SYMBOL=BTC-USDT\n", encoding="utf-8")
    os.environ["AATS_MODE"] = "paper_live"
    os.environ["AATS_DEFAULT_SYMBOL"] = "OLD"
    os.environ["AATS_SOMETHING_ELSE"] = "legacy"
    os.environ["UNRELATED_ENV"] = "keep"

    loaded = load_profiled_dotenv_into_process(tmp_path, "spot")

    assert loaded == dotenv_path
    assert os.environ["AATS_MODE"] == "guarded_live"
    assert os.environ["AATS_DEFAULT_SYMBOL"] == "BTC-USDT"
    assert "AATS_SOMETHING_ELSE" not in os.environ
    assert os.environ["UNRELATED_ENV"] == "keep"


def test_profile_templates_are_utf8_and_use_live_canonical_keys() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    supported_keys = {f"AATS_{name.upper()}" for name in AATSSettings.model_fields}
    expected_phrases = {
        ".env.spot": ["AATS_TRADING_PRODUCT_TYPE=spot", "AATS_AI_OPERATING_MODE", "AATS_TRIAL_GUARD_ENABLED"],
        ".env.derivatives": ["AATS_TRADING_PRODUCT_TYPE=derivatives", "AATS_AI_OPERATING_MODE", "AATS_TRIAL_GUARD_ENABLED"],
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
