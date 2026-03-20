from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values


StartupProfile = Literal["spot", "derivatives"]

PROFILE_ENV_FILES: dict[StartupProfile, str] = {
    "spot": ".env.spot",
    "derivatives": ".env.derivatives",
}


def resolve_profile_dotenv_path(project_root: Path, profile: StartupProfile | None = None) -> Path:
    if profile is None:
        default_path = project_root / ".env"
        if default_path.exists():
            return default_path
        raise FileNotFoundError("startup_profile_required")
    return project_root / PROFILE_ENV_FILES[profile]


def load_profiled_dotenv_into_process(project_root: Path, profile: StartupProfile | None = None) -> Path:
    dotenv_path = resolve_profile_dotenv_path(project_root, profile)
    if not dotenv_path.exists():
        raise FileNotFoundError(f"dotenv_profile_not_found:{dotenv_path.name}")

    for key in list(os.environ):
        if key.startswith("AATS_"):
            os.environ.pop(key, None)

    for key, value in dotenv_values(dotenv_path).items():
        if key is None or value is None:
            continue
        os.environ[key] = value
    return dotenv_path
