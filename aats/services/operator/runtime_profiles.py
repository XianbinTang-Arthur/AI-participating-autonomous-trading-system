from __future__ import annotations

from aats.bootstrap.settings import AATSSettings
from aats.schemas.runtime_profiles import (
    RuntimeProfileResolution,
    runtime_profile_payload_from_settings,
    summarize_runtime_profile_payload,
)


def runtime_profile_resolution(*, settings: AATSSettings) -> RuntimeProfileResolution:
    return RuntimeProfileResolution(
        profile_source="env_only",
        resolved_settings=settings.model_dump(mode="python"),
    )


def readonly_runtime_profile_snapshot(
    *,
    settings: AATSSettings,
    resolution: RuntimeProfileResolution,
) -> dict[str, object]:
    current_payload = runtime_profile_payload_from_settings(settings)
    return {
        "profile_source": resolution.profile_source,
        "current_runtime_payload": current_payload,
        "current_runtime_summary": summarize_runtime_profile_payload(current_payload),
        "management_enabled": False,
        "control_plane_status": "removed_env_only",
        "control_plane_summary": "runtime_profile_control_removed",
    }
