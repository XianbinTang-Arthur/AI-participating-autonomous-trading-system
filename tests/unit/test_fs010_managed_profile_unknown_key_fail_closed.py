from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from aats.bootstrap.config import load_settings
from aats.bootstrap.managed_profiles import (
    MANAGED_PROFILE_DEFINITIONS,
    load_managed_profile_values,
)
from aats.bootstrap.settings import AATSSettings


REMOVED_PSEUDO_KEY = "strategy_profile_auto_rollback_enabled"


def _write_spot_strategy_profile(project_root: Path, content: str) -> Path:
    strategy_path = project_root / "configs" / "strategy_profiles" / "spot.yaml"
    strategy_path.parent.mkdir(parents=True)
    strategy_path.write_text(content, encoding="utf-8")
    return strategy_path


def test_current_managed_profiles_only_use_declared_settings_keys() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    allowed_keys = set(AATSSettings.model_fields)

    for profile, definition in MANAGED_PROFILE_DEFINITIONS.items():
        strategy_path = definition.strategy_tuning_path(repo_root)
        strategy_values = yaml.safe_load(strategy_path.read_text(encoding="utf-8"))

        assert isinstance(strategy_values, dict), profile
        assert set(definition.runtime_defaults) <= allowed_keys, profile
        assert set(strategy_values) <= allowed_keys, profile
        assert REMOVED_PSEUDO_KEY not in strategy_values, profile


def test_removed_pseudo_key_is_not_generated_or_advertised_as_current() -> None:
    from scripts.generate_managed_config_artifacts import _render_reference

    repo_root = Path(__file__).resolve().parents[2]
    generator = (repo_root / "scripts" / "generate_managed_config_artifacts.py").read_text(
        encoding="utf-8"
    )
    reference = (
        repo_root / "docs" / "configuration" / "managed-config-reference.md"
    ).read_text(encoding="utf-8")

    assert REMOVED_PSEUDO_KEY not in generator
    assert REMOVED_PSEUDO_KEY not in reference
    assert reference == _render_reference().rstrip() + "\n"
    assert "_write_configs_readme" not in generator


def test_unknown_managed_profile_key_fails_closed_with_source(tmp_path: Path) -> None:
    strategy_path = _write_spot_strategy_profile(
        tmp_path,
        "strategy_profile_typo_enabled: true\n",
    )

    with pytest.raises(ValueError) as exc_info:
        load_managed_profile_values("spot", project_root=tmp_path)

    message = str(exc_info.value)
    assert message.startswith("managed_profile_contains_unknown_settings_keys:")
    assert "profile=spot" in message
    assert f"source={strategy_path}" in message
    assert "keys=strategy_profile_typo_enabled" in message


def test_all_unknown_managed_profile_keys_are_reported_in_sorted_order(
    tmp_path: Path,
) -> None:
    _write_spot_strategy_profile(
        tmp_path,
        "z_unknown: true\na_unknown: false\n",
    )

    with pytest.raises(
        ValueError,
        match=r"keys=a_unknown,z_unknown$",
    ):
        load_managed_profile_values("spot", project_root=tmp_path)


def test_non_mapping_managed_profile_fails_closed(tmp_path: Path) -> None:
    strategy_path = _write_spot_strategy_profile(
        tmp_path,
        "- strategy_profile_auto_control_enabled\n",
    )

    with pytest.raises(ValueError) as exc_info:
        load_managed_profile_values("spot", project_root=tmp_path)

    message = str(exc_info.value)
    assert message.startswith("managed_profile_strategy_tuning_must_be_mapping:")
    assert "profile=spot" in message
    assert f"source={strategy_path}" in message


def test_known_managed_profile_key_is_merged(tmp_path: Path) -> None:
    _write_spot_strategy_profile(
        tmp_path,
        "strategy_profile_auto_control_enabled: true\n",
    )

    values = load_managed_profile_values("spot", project_root=tmp_path)

    assert values["strategy_profile_auto_control_enabled"] is True
    assert values["startup_profile"] == "spot"


def test_empty_managed_profile_remains_valid(tmp_path: Path) -> None:
    _write_spot_strategy_profile(tmp_path, "")

    values = load_managed_profile_values("spot", project_root=tmp_path)

    assert values == MANAGED_PROFILE_DEFINITIONS["spot"].runtime_defaults


def test_load_settings_cannot_bypass_managed_profile_key_validation(
    tmp_path: Path,
) -> None:
    _write_spot_strategy_profile(
        tmp_path,
        "strategy_profile_typo_enabled: true\n",
    )

    def _load_from_test_root(profile: str) -> dict[str, object]:
        return load_managed_profile_values(profile, project_root=tmp_path)  # type: ignore[arg-type]

    with (
        patch.object(
            AATSSettings,
            "model_config",
            {**AATSSettings.model_config, "env_file": None},
        ),
        patch.dict(
            os.environ,
            {"AATS_ENV_TEMPLATE_PROFILE": "spot"},
            clear=True,
        ),
        patch(
            "aats.bootstrap.config.load_managed_profile_values",
            side_effect=_load_from_test_root,
        ),
        pytest.raises(
            ValueError,
            match="managed_profile_contains_unknown_settings_keys",
        ),
    ):
        load_settings()
