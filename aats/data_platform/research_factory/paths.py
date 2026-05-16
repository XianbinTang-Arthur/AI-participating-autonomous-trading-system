"""Research Factory filesystem boundary helpers."""

from __future__ import annotations

import shutil
from pathlib import Path

FORBIDDEN_RESEARCH_ARTIFACT_PATH_TOKENS = (
    ".env",
    "api_key",
    "credential",
    "credentials",
    "live",
    "passwd",
    "password",
    "private_key",
    "production_config",
    "secret",
    "token",
)


def require_research_artifact_json_file(
    path: str | Path,
    field_name: str,
    *,
    research_root: str | Path | None = None,
) -> Path:
    """Validate a JSON file path is inside the research artifact tree."""
    source_path = Path(path)
    if not str(field_name).strip():
        raise ValueError("field_name must be a non-empty string")
    _reject_unsafe_path_parts(source_path, field_name)
    if source_path.suffix.lower() != ".json":
        raise ValueError(f"{field_name} must be a .json research artifact")
    allowed_root = _research_artifact_root(Path(research_root)) if research_root else None
    if allowed_root is None:
        allowed_root = _research_artifact_root(source_path)
    if allowed_root is None:
        raise ValueError(f"{field_name} must be under artifacts/research")
    if not _is_relative_to(source_path.resolve(strict=False), allowed_root):
        raise ValueError(f"{field_name} must be under artifacts/research")
    if not source_path.exists():
        raise ValueError(f"{field_name} does not exist")
    if not source_path.is_file():
        raise ValueError(f"{field_name} must be a file")
    return source_path


def copy_research_artifact_file(
    source_path: str | Path,
    destination_dir: str | Path,
    *,
    destination_name: str,
    research_root: str | Path | None = None,
) -> str:
    """Copy a validated research artifact file into an experiment directory."""
    source = require_research_artifact_json_file(
        source_path,
        "source_path",
        research_root=research_root,
    )
    destination_name = _require_safe_filename(destination_name, "destination_name")
    destination = Path(destination_dir) / destination_name
    _reject_unsafe_path_parts(destination, "destination")
    destination.parent.mkdir(parents=True, exist_ok=True)

    if source.resolve() != destination.resolve(strict=False):
        shutil.copyfile(source, destination)
    return destination_name


def _research_artifact_root(path: Path) -> Path | None:
    resolved = path.resolve(strict=False)
    parts = resolved.parts
    for index in range(len(parts) - 1):
        if parts[index] == "artifacts" and parts[index + 1] == "research":
            return Path(*parts[: index + 2])
    return None


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _reject_unsafe_path_parts(path: Path, field_name: str) -> None:
    parts = path.parts
    if not parts:
        raise ValueError(f"{field_name} must be a non-empty path")
    if str(path).startswith("~") or "~" in parts:
        raise ValueError(f"{field_name} must not use home-directory expansion")
    if ".." in parts:
        raise ValueError(f"{field_name} must not contain path traversal")
    lowered_parts = [part.lower() for part in parts]
    for part in lowered_parts:
        for token in FORBIDDEN_RESEARCH_ARTIFACT_PATH_TOKENS:
            if token in part:
                raise ValueError(f"{field_name} contains forbidden path token: {token}")


def _require_safe_filename(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    path = Path(value)
    if len(path.parts) != 1 or path.name in {".", ".."}:
        raise ValueError(f"{field_name} must be a plain filename")
    return value
