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
        ".env.spot": ["运行环境标识", "AI 运行模式", "交易产品类型", "执行参数建议模式"],
        ".env.derivatives": ["运行环境标识", "AI 运行模式", "交易产品类型", "执行参数建议模式"],
    }

    for env_name, phrases in expected_phrases.items():
        text = (repo_root / env_name).read_text(encoding="utf-8")
        assert "\ufffd" not in text
        assert "AATS_AI_PRIMARY_" not in text
        for phrase in phrases:
            assert phrase in text
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
    assert "管理员手动切换" in text
    assert "自动切换结论" in text
    assert "AI 运行模式" in text
    assert "立即评估并生成建议" not in text
    assert "评估并允许自动切换" not in text
    assert "回滚到上一稳定策略档位" not in text
