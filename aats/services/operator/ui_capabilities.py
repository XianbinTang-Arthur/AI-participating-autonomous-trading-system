from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any


UI_OPERATING_MODE_OVERRIDE_ENV = "AATS_ALLOW_UI_OPERATING_MODE_OVERRIDE"
UI_OPERATING_MODE_OVERRIDE_DISABLED_REASON = (
    "ui_operating_mode_override_disabled_by_governance_policy"
)

_TRUTHY_VALUES = {"true", "1", "yes"}


def ui_operating_mode_override_enabled(
    env: Mapping[str, str] | None = None,
) -> bool:
    source = env if env is not None else os.environ
    value = str(source.get(UI_OPERATING_MODE_OVERRIDE_ENV, "false")).strip().lower()
    return value in _TRUTHY_VALUES


def ui_operating_mode_override_policy(
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    enabled = ui_operating_mode_override_enabled(env)
    return {
        "enabled": enabled,
        "source": "environment",
        "disabled_reason": (
            None if enabled else UI_OPERATING_MODE_OVERRIDE_DISABLED_REASON
        ),
    }
