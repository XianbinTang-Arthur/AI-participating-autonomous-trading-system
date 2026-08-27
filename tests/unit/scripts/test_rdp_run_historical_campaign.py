from __future__ import annotations

from scripts import rdp_run_historical_campaign


def test_apply_reports_stable_freeze_reason_without_creating_storage(
    tmp_path,
    capsys,
) -> None:
    storage_root = tmp_path / "must-not-exist"

    code = rdp_run_historical_campaign.main(
        [
            "--campaign-id",
            "00000000-0000-0000-0000-000000000123",
            "--storage-root",
            str(storage_root),
            "--resume-running",
            "--apply",
            "--confirm",
        ]
    )

    assert code == 3
    assert (
        "historical_campaign_execution_unavailable_until_"
        "persistent_fencing_and_immutable_silver"
    ) in capsys.readouterr().err
    assert not storage_root.exists()
