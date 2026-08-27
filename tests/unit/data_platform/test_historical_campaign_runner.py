from __future__ import annotations

from pathlib import Path

import pytest

from aats.data_platform.data_governance import historical_campaign_runner


def test_runner_is_frozen_before_filesystem_access(tmp_path: Path) -> None:
    storage_root = tmp_path / "must-not-exist"

    with pytest.raises(
        RuntimeError,
        match="execution_unavailable_until_persistent_fencing_and_immutable_silver",
    ):
        historical_campaign_runner.run_historical_campaign(
            campaign_id="00000000-0000-0000-0000-000000000123",
            storage_root=storage_root,
            project_root=tmp_path,
            resume_running=True,
        )

    assert not storage_root.exists()


def test_runner_keeps_no_privately_callable_legacy_execution_chain() -> None:
    assert not hasattr(historical_campaign_runner, "_legacy_campaign_execution_body")
    assert not hasattr(historical_campaign_runner, "_run_campaign_inputs")
    assert not hasattr(historical_campaign_runner, "_bundle_contract_binding_matches")
