from __future__ import annotations

import os
from unittest import TestCase
from unittest.mock import patch

from aats.data_platform.config import ResearchPlatformSettings


class TestRdpDatabaseConfig(TestCase):
    def test_database_url_falls_back_to_active_parameter_db_url(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AATS_ACTIVE_PARAMETER_DB_URL": "postgresql+psycopg://admin:pw@postgres:5432/aats_research",
            },
            clear=False,
        ):
            settings = ResearchPlatformSettings(_env_file=None)

        self.assertEqual(
            settings.database_url,
            "postgresql+psycopg://admin:pw@postgres:5432/aats_research",
        )

    def test_explicit_rdp_database_url_wins_over_active_parameter_url(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AATS_ACTIVE_PARAMETER_DB_URL": "postgresql+psycopg://admin:pw@postgres:5432/aats_research",
                "RDP_DATABASE_URL": "postgresql+psycopg://custom:pw@custom-host:5432/custom_db",
            },
            clear=False,
        ):
            settings = ResearchPlatformSettings(_env_file=None)

        self.assertEqual(
            settings.database_url,
            "postgresql+psycopg://custom:pw@custom-host:5432/custom_db",
        )

    def test_constructor_database_url_wins_over_environment_fallback(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AATS_ACTIVE_PARAMETER_DB_URL": "postgresql+psycopg://admin:pw@postgres:5432/aats_research",
            },
            clear=False,
        ):
            settings = ResearchPlatformSettings(
                _env_file=None,
                database_url="postgresql+psycopg://manual:pw@manual-host:5432/manual_db",
            )

        self.assertEqual(
            settings.database_url,
            "postgresql+psycopg://manual:pw@manual-host:5432/manual_db",
        )
