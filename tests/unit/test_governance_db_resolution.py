from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from aats.data_platform.governance._db_util import resolve_governance_db_url


def test_resolve_governance_db_url_prefers_explicit_env(monkeypatch) -> None:
    monkeypatch.setenv("AATS_ACTIVE_PARAMETER_DB_URL", "postgresql://explicit")
    monkeypatch.setenv("RDP_DATABASE_URL", "postgresql://rdp")

    with patch(
        "aats.data_platform.config.get_settings",
        return_value=SimpleNamespace(database_url="postgresql://settings"),
    ):
        assert resolve_governance_db_url() == "postgresql://explicit"


def test_resolve_governance_db_url_falls_back_to_rdp_url(monkeypatch) -> None:
    monkeypatch.delenv("AATS_ACTIVE_PARAMETER_DB_URL", raising=False)
    monkeypatch.setenv("RDP_DATABASE_URL", "postgresql://rdp")

    with patch(
        "aats.data_platform.config.get_settings",
        return_value=SimpleNamespace(database_url="postgresql://settings"),
    ):
        assert resolve_governance_db_url() == "postgresql://rdp"


def test_resolve_governance_db_url_falls_back_to_settings(monkeypatch) -> None:
    monkeypatch.delenv("AATS_ACTIVE_PARAMETER_DB_URL", raising=False)
    monkeypatch.delenv("RDP_DATABASE_URL", raising=False)

    with patch(
        "aats.data_platform.config.get_settings",
        return_value=SimpleNamespace(database_url="postgresql://settings"),
    ):
        assert resolve_governance_db_url() == "postgresql://settings"
