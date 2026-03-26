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
from aats.bootstrap.managed_profiles import MANAGED_PROFILE_DEFINITIONS, MANAGED_PROFILE_DERIVED_ENV_KEYS
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
        "AATS_DATABASE_URL",
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
            assert key not in values, key
        for key in deprecated_strategy_keys:
            assert key not in values, key
        for key in values:
            assert key in supported_keys, key
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
        assert data["strategy_family_active"] == "directional"
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
