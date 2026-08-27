"""Fail-closed boundary for the frozen historical campaign executor.

The former runner could not fence database, filesystem, and network side effects
with one persistent authority. Keep the public signature for callers, but do
not retain a privately callable copy of the unsafe execution chain.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aats.data_platform.data_governance.historical_campaign import (
    CAMPAIGN_EXECUTION_UNAVAILABLE_REASON,
)


def run_historical_campaign(
    *,
    campaign_id: str,
    storage_root: Path,
    project_root: Path,
    resume_running: bool = False,
) -> dict[str, Any]:
    """Reject execution before campaign, filesystem, network, or business DB work."""

    del campaign_id, storage_root, project_root, resume_running
    raise RuntimeError(CAMPAIGN_EXECUTION_UNAVAILABLE_REASON)


__all__ = ["run_historical_campaign"]
