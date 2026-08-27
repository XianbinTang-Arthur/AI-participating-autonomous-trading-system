from __future__ import annotations

import json

from scripts import rdp_download_historical_campaign


def test_apply_requires_registered_campaign_runner_before_network(
    tmp_path,
    capsys,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({}), encoding="utf-8")

    code = rdp_download_historical_campaign.main(
        [
            "--manifest",
            str(manifest_path),
            "--target-dir",
            str(tmp_path / "downloads"),
            "--apply",
            "--confirm",
        ]
    )

    assert code == 4
    assert "独立 manifest 下载入口已停用" in capsys.readouterr().err
    assert not (tmp_path / "downloads").exists()
