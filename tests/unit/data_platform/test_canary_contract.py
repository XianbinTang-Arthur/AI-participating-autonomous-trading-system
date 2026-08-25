from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from aats.data_platform.operations.canary_contract import validate_canary_contract


CONTRACT_PATH = Path("configs/canary/derivatives_canary_contract.json")


def _contract() -> dict:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def test_checked_in_contract_is_valid_but_never_deployable() -> None:
    result = validate_canary_contract(_contract())
    assert result.valid is True
    assert result.deployable is False


@pytest.mark.parametrize(
    ("section", "field", "value", "reason"),
    [
        ("deployment", "deployable", True, "deployment_must_be_false:deployable"),
        ("deployment", "override_supported", True, "deployment_must_be_false:override_supported"),
        ("credential_policy", "forbidden_permissions", [], "withdraw_and_transfer_must_be_forbidden"),
        ("risk_limits", "leverage", 2, "risk_limit_exceeds_contract:leverage"),
        ("risk_limits", "max_daily_loss_usdt", 10, "risk_limit_exceeds_contract:max_daily_loss_usdt"),
        ("governance", "automatic_resume_allowed", True, "automatic_resume_forbidden"),
    ],
)
def test_contract_weakening_is_rejected(
    section: str, field: str, value: object, reason: str
) -> None:
    payload = copy.deepcopy(_contract())
    payload[section][field] = value
    result = validate_canary_contract(payload)
    assert result.valid is False
    assert reason in result.reason_codes


def test_canary_is_not_registered_in_deployment_entrypoint() -> None:
    deploy_source = Path("scripts/deploy.sh").read_text(encoding="utf-8")
    compose_source = Path(
        "deploy/wsl2-dev/docker-compose.aats.derivatives.yml"
    ).read_text(encoding="utf-8")
    assert "future_derivatives_canary" not in deploy_source
    assert "future_derivatives_canary" not in compose_source
    assert "derivatives-canary" not in deploy_source
