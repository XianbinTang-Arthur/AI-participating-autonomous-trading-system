from __future__ import annotations

import unittest
from unittest.mock import patch

from aats.bootstrap.config import build_runtime
from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.schemas.exchange import ExchangeAccountConfiguration, ExchangeAccountSnapshot
from tests.support.postgres import temporary_postgres_url


class _FakePositionModeAccountService:
    SNAPSHOT: ExchangeAccountSnapshot | None = None

    def __init__(self, *, settings, client, private_ws_client=None) -> None:
        _ = client
        _ = private_ws_client
        self.settings = settings
        self._snapshot = type(self).SNAPSHOT

    async def refresh(self, *, force: bool = False):
        _ = force
        return self._snapshot

    def latest_snapshot(self):
        return self._snapshot

    def status(self):
        return {
            "backend": "okx",
            "enabled": True,
            "credentials_configured": True,
            "connected": self._snapshot is not None,
            "fresh": self._snapshot is not None,
            "last_update_ts": None if self._snapshot is None else self._snapshot.fetched_at,
            "last_error": None,
            "ready": self._snapshot is not None,
            "detail": "fake_position_mode_account",
            "blockers": [] if self._snapshot is not None else ["account_snapshot_missing"],
        }


class TestTask72A1DerivativesStartupGuards(unittest.IsolatedAsyncioTestCase):
    async def test_startup_profile_derivatives_requires_derivatives_product_type(self) -> None:
        with self.assertRaisesRegex(ValueError, "startup_profile_derivatives_requires_derivatives_product_type"):
            await build_runtime(
                AATSSettings.model_validate(
                    {
                        "startup_profile": "derivatives",
                        "config_profile": "guarded_derivatives_enabled",
                        "mode": "guarded_live",
                        "market_data_backend": "okx",
                        "execution_backend": "okx",
                        "account_backend": "okx",
                        "account_read_enabled": True,
                        "trading_product_type": "spot",
                        "margin_mode": "cash",
                    }
                )
            )

    async def test_startup_profile_derivatives_requires_dedicated_derivatives_profile(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "startup_profile_derivatives_requires_dedicated_derivatives_config_profile",
        ):
            await build_runtime(
                AATSSettings.model_validate(
                    {
                        "startup_profile": "derivatives",
                        "config_profile": "guarded_simulated_submit_enabled",
                        "mode": "guarded_live",
                        "market_data_backend": "okx",
                        "execution_backend": "okx",
                        "account_backend": "okx",
                        "account_read_enabled": True,
                        "trading_product_type": "derivatives",
                        "margin_mode": "cross",
                    }
                )
            )

    async def test_dedicated_derivatives_profile_disallows_cash_margin(self) -> None:
        with self.assertRaisesRegex(ValueError, "guarded_derivatives_config_profile_disallows_cash_margin_mode"):
            await build_runtime(
                AATSSettings.model_validate(
                    {
                        "startup_profile": "derivatives",
                        "config_profile": "guarded_derivatives_dry_run",
                        "mode": "guarded_live",
                        "market_data_backend": "okx",
                        "execution_backend": "okx",
                        "account_backend": "okx",
                        "account_read_enabled": True,
                        "trading_product_type": "derivatives",
                        "margin_mode": "cash",
                    }
                )
            )

    async def test_derivatives_exchange_runtime_requires_postgres_storage(self) -> None:
        with self.assertRaisesRegex(ValueError, "derivatives_exchange_runtime_requires_postgres_storage"):
            await build_runtime(
                AATSSettings.model_validate(
                    {
                        "startup_profile": "derivatives",
                        "config_profile": "guarded_derivatives_dry_run",
                        "mode": "guarded_live",
                        "market_data_backend": "okx",
                        "execution_backend": "okx",
                        "account_backend": "okx",
                        "account_read_enabled": True,
                        "okx_simulated_trading": True,
                        "trading_product_type": "derivatives",
                        "margin_mode": "cross",
                        "storage_mode": "memory",
                    }
                )
            )

    async def test_removed_old_auto_parallel_key_is_rejected_before_runtime_build(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "strategy_sleeve_auto_parallel_enabled_has_been_removed_use_strategy_sleeve_auto_execution_enabled",
        ):
            AATSSettings.model_validate(
                {
                    "config_profile": "local_demo",
                    "mode": "paper_live",
                    "market_data_backend": "demo",
                    "execution_backend": "paper",
                    "account_backend": "disabled",
                    "account_read_enabled": False,
                    "storage_mode": "memory",
                    "event_persistence_mode": "strict",
                    "strategy_sleeve_auto_parallel_enabled": False,
                }
            )

    async def test_new_auto_execution_key_avoids_deprecation_warning_but_keeps_guard_warning(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "config_profile": "local_demo",
                "mode": "paper_live",
                "market_data_backend": "demo",
                "execution_backend": "paper",
                "account_backend": "disabled",
                "account_read_enabled": False,
                "storage_mode": "memory",
                "event_persistence_mode": "strict",
                "strategy_sleeve_auto_execution_enabled": False,
            }
        )

        with self.assertLogs("aats.bootstrap", level="WARNING") as captured:
            await build_runtime(settings)

        rendered = "\n".join(captured.output)
        self.assertNotIn("startup_deprecated_auto_parallel_key", rendered)
        self.assertIn("startup_entry_execution_guard_active", rendered)
        self.assertIn("non_protective_entry_execution_advisory_only", rendered)

    async def test_derivatives_exchange_runtime_requires_database_url_and_credentials(self) -> None:
        with self.assertRaisesRegex(ValueError, "derivatives_exchange_runtime_requires_database_url"):
            await build_runtime(
                AATSSettings.model_validate(
                    {
                        "startup_profile": "derivatives",
                        "config_profile": "guarded_derivatives_enabled",
                        "mode": "guarded_live",
                        "market_data_backend": "okx",
                        "execution_backend": "okx",
                        "account_backend": "okx",
                        "account_read_enabled": True,
                        "okx_simulated_trading": True,
                        "trading_product_type": "derivatives",
                        "margin_mode": "cross",
                        "storage_mode": "postgres",
                    }
                )
            )

        with self.assertRaisesRegex(ValueError, "derivatives_exchange_runtime_requires_okx_credentials"):
            await build_runtime(
                AATSSettings.model_validate(
                    {
                        "startup_profile": "derivatives",
                        "config_profile": "guarded_derivatives_enabled",
                        "mode": "guarded_live",
                        "market_data_backend": "okx",
                        "execution_backend": "okx",
                        "account_backend": "okx",
                        "account_read_enabled": True,
                        "okx_simulated_trading": True,
                        "trading_product_type": "derivatives",
                        "margin_mode": "cross",
                        "storage_mode": "postgres",
                        "database_url": "postgresql+psycopg://aats:aats@localhost:5432/aats",
                    }
                )
            )

    async def test_derivatives_exchange_runtime_requires_operator_auth(self) -> None:
        with self.assertRaisesRegex(ValueError, "derivatives_exchange_runtime_requires_operator_auth"):
            await build_runtime(
                AATSSettings.model_validate(
                    {
                        "startup_profile": "derivatives",
                        "config_profile": "guarded_derivatives_enabled",
                        "mode": "guarded_live",
                        "market_data_backend": "okx",
                        "execution_backend": "okx",
                        "account_backend": "okx",
                        "account_read_enabled": True,
                        "okx_simulated_trading": True,
                        "trading_product_type": "derivatives",
                        "margin_mode": "cross",
                        "storage_mode": "postgres",
                        "database_url": "postgresql+psycopg://aats:aats@localhost:5432/aats",
                        "okx_api_key": "key",
                        "okx_api_secret": "secret",
                        "okx_api_passphrase": "passphrase",
                        "operator_auth_enabled": False,
                    }
                )
            )

    async def test_derivatives_exchange_runtime_requires_secure_operator_session_cookie(self) -> None:
        # environment=prod + okx_simulated_trading=False 走严格路径：
        # dev+simulated 的放行分支是 slice docker-compose-hardening fix slice 加的，
        # 对应放行 case 在 tests/unit/test_bootstrap_config_dev_simulated_hardening.py 覆盖。
        with self.assertRaisesRegex(
            ValueError,
            "derivatives_exchange_runtime_requires_secure_operator_session_cookie",
        ):
            await build_runtime(
                AATSSettings.model_validate(
                    {
                        "environment": "prod",
                        "startup_profile": "derivatives",
                        "config_profile": "guarded_derivatives_enabled",
                        "mode": "guarded_live",
                        "market_data_backend": "okx",
                        "execution_backend": "okx",
                        "account_backend": "okx",
                        "account_read_enabled": True,
                        "okx_simulated_trading": False,
                        "trading_product_type": "derivatives",
                        "margin_mode": "cross",
                        "storage_mode": "postgres",
                        "database_url": "postgresql+psycopg://aats:aats@localhost:5432/aats",
                        "okx_api_key": "key",
                        "okx_api_secret": "secret",
                        "okx_api_passphrase": "passphrase",
                        "operator_auth_enabled": True,
                        "operator_session_secret": "session-secret",
                        "operator_session_cookie_secure": False,
                    }
                )
            )

    async def test_derivatives_exchange_runtime_fails_fast_when_exchange_position_mode_mismatches_configured_mode(self) -> None:
        _FakePositionModeAccountService.SNAPSHOT = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=utc_now(),
            account_mode="4",
            position_mode="net_mode",
            account_configuration=ExchangeAccountConfiguration(
                account_level_code="4",
                account_level_label="portfolio_margin",
                position_mode="net_mode",
                position_mode_label="net",
            ),
        )
        try:
            with temporary_postgres_url() as (database_url, _admin_engine, _schema_name):
                settings = AATSSettings.model_validate(
                    {
                        "startup_profile": "derivatives",
                        "config_profile": "guarded_derivatives_enabled",
                        "mode": "guarded_live",
                        "market_data_backend": "okx",
                        "execution_backend": "okx",
                        "account_backend": "okx",
                        "account_read_enabled": True,
                        "okx_simulated_trading": True,
                        "trading_product_type": "derivatives",
                        "margin_mode": "cross",
                        "storage_mode": "postgres",
                        "database_url": database_url,
                        "okx_api_key": "key",
                        "okx_api_secret": "secret",
                        "okx_api_passphrase": "passphrase",
                        "operator_auth_enabled": True,
                        "derivatives_position_mode": "hedge",
                    }
                )
                with patch("aats.bootstrap.config.OKXAccountService", _FakePositionModeAccountService):
                    with self.assertRaisesRegex(ValueError, "derivatives_exchange_runtime_position_mode_mismatch"):
                        await build_runtime(settings)
        finally:
            _FakePositionModeAccountService.SNAPSHOT = None

    async def test_derivatives_exchange_runtime_fails_fast_when_exchange_position_mode_is_missing(self) -> None:
        _FakePositionModeAccountService.SNAPSHOT = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=utc_now(),
            account_mode="4",
            position_mode=None,
            account_configuration=ExchangeAccountConfiguration(
                account_level_code="4",
                account_level_label="portfolio_margin",
                position_mode=None,
                position_mode_label=None,
            ),
        )
        try:
            with temporary_postgres_url() as (database_url, _admin_engine, _schema_name):
                settings = AATSSettings.model_validate(
                    {
                        "startup_profile": "derivatives",
                        "config_profile": "guarded_derivatives_enabled",
                        "mode": "guarded_live",
                        "market_data_backend": "okx",
                        "execution_backend": "okx",
                        "account_backend": "okx",
                        "account_read_enabled": True,
                        "okx_simulated_trading": True,
                        "trading_product_type": "derivatives",
                        "margin_mode": "cross",
                        "storage_mode": "postgres",
                        "database_url": database_url,
                        "okx_api_key": "key",
                        "okx_api_secret": "secret",
                        "okx_api_passphrase": "passphrase",
                        "operator_auth_enabled": True,
                        "derivatives_position_mode": "net",
                    }
                )
                with patch("aats.bootstrap.config.OKXAccountService", _FakePositionModeAccountService):
                    with self.assertRaisesRegex(ValueError, "derivatives_exchange_runtime_requires_exchange_position_mode"):
                        await build_runtime(settings)
        finally:
            _FakePositionModeAccountService.SNAPSHOT = None

    async def test_derivatives_exchange_runtime_rejects_placeholder_credentials(self) -> None:
        with self.assertRaisesRegex(ValueError, "derivatives_exchange_runtime_requires_okx_credentials"):
            await build_runtime(
                AATSSettings.model_validate(
                    {
                        "startup_profile": "derivatives",
                        "config_profile": "guarded_derivatives_enabled",
                        "mode": "guarded_live",
                        "market_data_backend": "okx",
                        "execution_backend": "okx",
                        "account_backend": "okx",
                        "account_read_enabled": True,
                        "okx_simulated_trading": False,
                        "trading_product_type": "derivatives",
                        "margin_mode": "cross",
                        "storage_mode": "postgres",
                        "database_url": "postgresql+psycopg://aats:aats@localhost:5432/aats",
                        "okx_api_key": "REPLACE_WITH_REAL_OKX_API_KEY",
                        "okx_api_secret": "REPLACE_WITH_REAL_OKX_API_SECRET",
                        "okx_api_passphrase": "REPLACE_WITH_REAL_OKX_API_PASSPHRASE",
                        "operator_auth_enabled": True,
                    }
                )
            )


class TestTask72A1SpotStartupGuards(unittest.IsolatedAsyncioTestCase):
    async def test_spot_exchange_runtime_requires_postgres_storage(self) -> None:
        with self.assertRaisesRegex(ValueError, "spot_exchange_runtime_requires_postgres_storage"):
            await build_runtime(
                AATSSettings.model_validate(
                    {
                        "startup_profile": "spot",
                        "config_profile": "guarded_spot_enabled",
                        "mode": "guarded_live",
                        "market_data_backend": "okx",
                        "execution_backend": "okx",
                        "account_backend": "okx",
                        "account_read_enabled": True,
                        "okx_simulated_trading": False,
                        "trading_product_type": "spot",
                        "margin_mode": "cash",
                        "storage_mode": "memory",
                    }
                )
            )

    async def test_spot_exchange_runtime_requires_cash_margin_mode(self) -> None:
        with self.assertRaisesRegex(ValueError, "guarded_spot_config_profile_requires_cash_margin_mode"):
            await build_runtime(
                AATSSettings.model_validate(
                    {
                        "startup_profile": "spot",
                        "config_profile": "guarded_spot_enabled",
                        "mode": "guarded_live",
                        "market_data_backend": "okx",
                        "execution_backend": "okx",
                        "account_backend": "okx",
                        "account_read_enabled": True,
                        "okx_simulated_trading": False,
                        "trading_product_type": "spot",
                        "margin_mode": "cross",
                        "storage_mode": "postgres",
                        "database_url": "postgresql+psycopg://aats:aats@localhost:5432/aats",
                        "okx_api_key": "key",
                        "okx_api_secret": "secret",
                        "okx_api_passphrase": "passphrase",
                        "operator_auth_enabled": True,
                    }
                )
            )

    async def test_spot_exchange_runtime_requires_database_url_and_credentials(self) -> None:
        with self.assertRaisesRegex(ValueError, "spot_exchange_runtime_requires_database_url"):
            await build_runtime(
                AATSSettings.model_validate(
                    {
                        "startup_profile": "spot",
                        "config_profile": "guarded_spot_enabled",
                        "mode": "guarded_live",
                        "market_data_backend": "okx",
                        "execution_backend": "okx",
                        "account_backend": "okx",
                        "account_read_enabled": True,
                        "okx_simulated_trading": False,
                        "trading_product_type": "spot",
                        "margin_mode": "cash",
                        "storage_mode": "postgres",
                    }
                )
            )

        with self.assertRaisesRegex(ValueError, "spot_exchange_runtime_requires_database_url"):
            await build_runtime(
                AATSSettings.model_validate(
                    {
                        "startup_profile": "spot",
                        "config_profile": "guarded_spot_enabled",
                        "mode": "guarded_live",
                        "market_data_backend": "okx",
                        "execution_backend": "okx",
                        "account_backend": "okx",
                        "account_read_enabled": True,
                        "okx_simulated_trading": False,
                        "trading_product_type": "spot",
                        "margin_mode": "cash",
                        "storage_mode": "postgres",
                        "database_url": "REPLACE_WITH_REAL_DATABASE_URL",
                        "okx_api_key": "key",
                        "okx_api_secret": "secret",
                        "okx_api_passphrase": "passphrase",
                        "operator_auth_enabled": True,
                    }
                )
            )

        with self.assertRaisesRegex(ValueError, "spot_exchange_runtime_requires_okx_credentials"):
            await build_runtime(
                AATSSettings.model_validate(
                    {
                        "startup_profile": "spot",
                        "config_profile": "guarded_spot_enabled",
                        "mode": "guarded_live",
                        "market_data_backend": "okx",
                        "execution_backend": "okx",
                        "account_backend": "okx",
                        "account_read_enabled": True,
                        "okx_simulated_trading": False,
                        "trading_product_type": "spot",
                        "margin_mode": "cash",
                        "storage_mode": "postgres",
                        "database_url": "postgresql+psycopg://aats:aats@localhost:5432/aats",
                        "okx_api_key": "REPLACE_WITH_REAL_OKX_API_KEY",
                        "okx_api_secret": "REPLACE_WITH_REAL_OKX_API_SECRET",
                        "okx_api_passphrase": "REPLACE_WITH_REAL_OKX_API_PASSPHRASE",
                        "operator_auth_enabled": True,
                    }
                )
            )

    async def test_spot_exchange_runtime_requires_operator_auth(self) -> None:
        with self.assertRaisesRegex(ValueError, "spot_exchange_runtime_requires_operator_auth"):
            await build_runtime(
                AATSSettings.model_validate(
                    {
                        "startup_profile": "spot",
                        "config_profile": "guarded_spot_enabled",
                        "mode": "guarded_live",
                        "market_data_backend": "okx",
                        "execution_backend": "okx",
                        "account_backend": "okx",
                        "account_read_enabled": True,
                        "okx_simulated_trading": False,
                        "trading_product_type": "spot",
                        "margin_mode": "cash",
                        "storage_mode": "postgres",
                        "database_url": "postgresql+psycopg://aats:aats@localhost:5432/aats",
                        "okx_api_key": "key",
                        "okx_api_secret": "secret",
                        "okx_api_passphrase": "passphrase",
                        "operator_auth_enabled": False,
                    }
                )
            )


if __name__ == "__main__":
    unittest.main()
