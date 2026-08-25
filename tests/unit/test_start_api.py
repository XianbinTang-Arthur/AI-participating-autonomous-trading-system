from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

from scripts.start_api import (
    apply_runtime_bind_overrides,
    configure_local_single_process_role,
    parse_args,
    require_loopback_host,
    resolved_api_bind,
)


def test_resolved_api_bind_honors_cli_overrides() -> None:
    with patch.dict(os.environ, {}, clear=True):
        apply_runtime_bind_overrides(host="127.0.0.1", port=8001)

        assert resolved_api_bind() == ("127.0.0.1", 8001)


@pytest.mark.parametrize("existing_role", [None, "gateway", "execution", "gateways"])
def test_local_api_forces_complete_monolith_role(existing_role: str | None) -> None:
    initial = {} if existing_role is None else {"AATS_PROCESS_ROLE": existing_role}
    with patch.dict(os.environ, initial, clear=True):
        configure_local_single_process_role()

        assert os.environ["AATS_PROCESS_ROLE"] == "monolith"


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1", "localhost"])
def test_local_api_accepts_only_loopback_hosts(host: str) -> None:
    assert require_loopback_host(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.0.2.10", "gateway.internal", ""])
def test_local_api_rejects_non_loopback_hosts_without_mutating_env(host: str) -> None:
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(ValueError, match="local_api_host_must_be_loopback"):
            apply_runtime_bind_overrides(host=host, port=None)

        assert "AATS_API_HOST" not in os.environ


@pytest.mark.parametrize("profile", ["spot_live", "derivatives_live"])
def test_local_api_parser_rejects_live_profiles(profile: str) -> None:
    with patch.object(sys, "argv", ["start_api.py", "--profile", profile]):
        with pytest.raises(SystemExit) as exc_info:
            parse_args()

    assert exc_info.value.code == 2
