from __future__ import annotations

from pathlib import Path

import pytest

from scripts.write_trading_readiness_evidence import _reject_secret_keys


def test_secret_fields_are_rejected_recursively() -> None:
    with pytest.raises(ValueError, match="secret_material_forbidden"):
        _reject_secret_keys({"facts": [{"database_url": "do-not-write"}]})


def test_script_has_no_dotenv_or_live_profile_loading() -> None:
    source = Path("scripts/write_trading_readiness_evidence.py").read_text(encoding="utf-8")
    assert "dotenv" not in source
    assert ".env" not in source
    assert "derivatives-live" not in source


def test_empty_checked_in_template_is_explicitly_fail_closed() -> None:
    import json

    payload = json.loads(
        Path("configs/templates/trading_readiness_manifest.example.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload["target"] == "simulation"
    assert payload["facts"] == []
