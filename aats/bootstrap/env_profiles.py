from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values


CanonicalStartupProfile = Literal["spot", "derivatives"]
EnvTemplateProfile = Literal["spot", "derivatives", "spot_live", "derivatives_live"]

PROFILE_ENV_FILES: dict[EnvTemplateProfile, str] = {
    "spot": ".env.spot",
    "derivatives": ".env.derivatives",
    "spot_live": ".env.spot.live",
    "derivatives_live": ".env.derivatives.live",
}

PROFILE_STARTUP_PROFILES: dict[EnvTemplateProfile, CanonicalStartupProfile] = {
    "spot": "spot",
    "derivatives": "derivatives",
    "spot_live": "spot",
    "derivatives_live": "derivatives",
}

_DERIVED_PROFILE_KEYS = {"AATS_STARTUP_PROFILE", "AATS_ENV_TEMPLATE_PROFILE"}
_LAST_PROFILE_VALUES: dict[str, str] = {}

def resolve_profile_dotenv_path(project_root: Path, profile: EnvTemplateProfile | None = None) -> Path:
    if profile is None:
        raise FileNotFoundError("startup_profile_required")
    return project_root / PROFILE_ENV_FILES[profile]


def reset_profiled_dotenv_state() -> None:
    _LAST_PROFILE_VALUES.clear()


def load_profiled_dotenv_into_process(project_root: Path, profile: EnvTemplateProfile | None = None) -> Path:
    dotenv_path = resolve_profile_dotenv_path(project_root, profile)
    if not dotenv_path.exists():
        raise FileNotFoundError(f"dotenv_profile_not_found:{dotenv_path.name}")

    # Remove only values that were injected by the previous profile load and are
    # still unchanged. This allows repeated profile switching in one process while
    # preserving secrets provided externally through the shell or a secret manager.
    for key, previous_value in list(_LAST_PROFILE_VALUES.items()):
        if key in _DERIVED_PROFILE_KEYS:
            continue
        current_value = os.environ.get(key)
        if current_value == previous_value:
            os.environ.pop(key, None)

    next_profile_values: dict[str, str] = {}

    for key, value in dotenv_values(dotenv_path).items():
        if key is None or value is None:
            continue
        if key.startswith("AATS_") and key in os.environ:
            continue
        os.environ[key] = value
        if key.startswith("AATS_"):
            next_profile_values[key] = value

    os.environ["AATS_STARTUP_PROFILE"] = PROFILE_STARTUP_PROFILES[profile]
    os.environ["AATS_ENV_TEMPLATE_PROFILE"] = profile
    next_profile_values["AATS_STARTUP_PROFILE"] = PROFILE_STARTUP_PROFILES[profile]
    next_profile_values["AATS_ENV_TEMPLATE_PROFILE"] = profile
    _LAST_PROFILE_VALUES.clear()
    _LAST_PROFILE_VALUES.update(next_profile_values)
    return dotenv_path
