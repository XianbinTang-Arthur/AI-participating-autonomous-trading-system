"""单元测试：dev + 模拟盘 exchange runtime hardening 放行 helper.

覆盖 slice
``docs/task/slice_docker_compose_hardening_fix_design.md`` 工作包 A:

- ``_is_dev_simulated_exchange_runtime``: 纯 helper 的四象限真值表
- ``_validate_startup_profile_settings``: cookie_secure 检查在 dev+simulated 放行、
  在 prod/live 仍硬性阻断
- ``_validate_operator_auth_settings``: enabled admin user 检查在 dev+simulated 放行、
  在 prod/live 仍硬性阻断

测试不依赖真实 postgres；``enabled_admin_count`` 用 ``unittest.mock.patch`` 打桩，
``StorageBackends`` 用 ``MagicMock`` 顶替。
"""
from __future__ import annotations

import logging
import unittest
from unittest.mock import MagicMock, patch

from aats.bootstrap.config import (
    _is_dev_simulated_exchange_runtime,
    _validate_operator_auth_settings,
    _validate_startup_profile_settings,
)
from aats.bootstrap.settings import AATSSettings
from aats.services.governance_engine.runtime_layers import resolve_runtime_layering


def _dev_simulated_spot_settings(**overrides: object) -> AATSSettings:
    """构造 dev + spot 模拟盘 + exchange_coupled 的 settings.

    对应 managed profile ``spot`` 的 runtime_defaults 组合：
    - environment=dev
    - mode=guarded_live + execution_backend=okx + okx_simulated_trading=True
      → exchange_simulated_spot runtime profile → exchange_coupled=True
    - config_profile 走 guarded_spot_enabled 让 hardening_kind 返回 "spot"
    """
    base: dict[str, object] = {
        "environment": "dev",
        "startup_profile": "spot",
        "config_profile": "guarded_spot_enabled",
        "mode": "guarded_live",
        "market_data_backend": "okx",
        "execution_backend": "okx",
        "account_backend": "okx",
        "account_read_enabled": True,
        "trading_product_type": "spot",
        "margin_mode": "cash",
        "storage_mode": "postgres",
        "database_url": "postgresql+psycopg://u:p@h:5432/d",
        "database_single_runtime_guard_enabled": True,
        "okx_api_key": "k" * 24,
        "okx_api_secret": "s" * 24,
        "okx_api_passphrase": "p" * 24,
        "okx_simulated_trading": True,
        "operator_auth_enabled": True,
        "operator_session_secret": "S" * 48,
        "operator_session_cookie_secure": False,  # managed profile spot dev UX default
    }
    base.update(overrides)
    return AATSSettings.model_validate(base)


def _dev_simulated_derivatives_settings(**overrides: object) -> AATSSettings:
    """构造 dev + derivatives 模拟盘 + exchange_coupled 的 settings."""
    base: dict[str, object] = {
        "environment": "dev",
        "startup_profile": "derivatives",
        "config_profile": "guarded_derivatives_enabled",
        "mode": "guarded_live",
        "market_data_backend": "okx",
        "execution_backend": "okx",
        "account_backend": "okx",
        "account_read_enabled": True,
        "trading_product_type": "derivatives",
        "margin_mode": "cross",
        "storage_mode": "postgres",
        "database_url": "postgresql+psycopg://u:p@h:5432/d",
        "database_single_runtime_guard_enabled": True,
        "okx_api_key": "k" * 24,
        "okx_api_secret": "s" * 24,
        "okx_api_passphrase": "p" * 24,
        "okx_simulated_trading": True,
        "operator_auth_enabled": True,
        "operator_session_secret": "S" * 48,
        "operator_session_cookie_secure": False,  # managed profile derivatives dev UX default
    }
    base.update(overrides)
    return AATSSettings.model_validate(base)


def _prod_live_spot_settings(**overrides: object) -> AATSSettings:
    """构造 prod + spot 实盘 + exchange_coupled 的 settings（对应 spot_live variant）."""
    base: dict[str, object] = {
        "environment": "prod",
        "startup_profile": "spot",
        "config_profile": "guarded_spot_enabled",
        "mode": "guarded_live",
        "market_data_backend": "okx",
        "execution_backend": "okx",
        "account_backend": "okx",
        "account_read_enabled": True,
        "trading_product_type": "spot",
        "margin_mode": "cash",
        "storage_mode": "postgres",
        "database_url": "postgresql+psycopg://u:p@h:5432/d",
        "database_single_runtime_guard_enabled": True,
        "okx_api_key": "k" * 24,
        "okx_api_secret": "s" * 24,
        "okx_api_passphrase": "p" * 24,
        "okx_simulated_trading": False,  # live
        "operator_auth_enabled": True,
        "operator_session_secret": "S" * 48,
        "operator_session_cookie_secure": True,  # live profile default is True
    }
    base.update(overrides)
    return AATSSettings.model_validate(base)


def _prod_live_derivatives_settings(**overrides: object) -> AATSSettings:
    """构造 prod + derivatives 实盘 + exchange_coupled 的 settings（对应 derivatives_live variant）."""
    base: dict[str, object] = {
        "environment": "prod",
        "startup_profile": "derivatives",
        "config_profile": "guarded_derivatives_enabled",
        "mode": "guarded_live",
        "market_data_backend": "okx",
        "execution_backend": "okx",
        "account_backend": "okx",
        "account_read_enabled": True,
        "trading_product_type": "derivatives",
        "margin_mode": "cross",
        "storage_mode": "postgres",
        "database_url": "postgresql+psycopg://u:p@h:5432/d",
        "database_single_runtime_guard_enabled": True,
        "okx_api_key": "k" * 24,
        "okx_api_secret": "s" * 24,
        "okx_api_passphrase": "p" * 24,
        "okx_simulated_trading": False,  # live
        "operator_auth_enabled": True,
        "operator_session_secret": "S" * 48,
        "operator_session_cookie_secure": True,
    }
    base.update(overrides)
    return AATSSettings.model_validate(base)


class TestIsDevSimulatedExchangeRuntimeHelper(unittest.TestCase):
    """`_is_dev_simulated_exchange_runtime` 纯函数四象限真值表."""

    def test_dev_plus_simulated_returns_true(self) -> None:
        settings = _dev_simulated_spot_settings()
        self.assertTrue(_is_dev_simulated_exchange_runtime(settings))

    def test_dev_plus_non_simulated_returns_false(self) -> None:
        settings = _dev_simulated_spot_settings(okx_simulated_trading=False)
        self.assertFalse(_is_dev_simulated_exchange_runtime(settings))

    def test_prod_plus_simulated_returns_false(self) -> None:
        # 防御性：即使有人把 prod 和 simulated 放一起（managed profile 不会这么做），
        # 也不放行
        settings = _prod_live_spot_settings(okx_simulated_trading=True)
        self.assertFalse(_is_dev_simulated_exchange_runtime(settings))

    def test_prod_plus_non_simulated_returns_false(self) -> None:
        settings = _prod_live_spot_settings()
        self.assertFalse(_is_dev_simulated_exchange_runtime(settings))

    def test_staging_plus_simulated_returns_false(self) -> None:
        # helper 只认 environment == "dev"，staging 也不放行（保险起见）
        settings = _dev_simulated_spot_settings(environment="staging")
        self.assertFalse(_is_dev_simulated_exchange_runtime(settings))


class TestValidateStartupProfileCookieSecure(unittest.TestCase):
    """`_validate_startup_profile_settings` 的 cookie_secure 放行分支."""

    def test_dev_simulated_spot_insecure_cookie_passes_with_warning(self) -> None:
        settings = _dev_simulated_spot_settings()
        runtime_layering = resolve_runtime_layering(settings)
        # 前置：runtime profile 确实是 exchange_coupled simulated spot
        self.assertTrue(runtime_layering.environment_capabilities.exchange_coupled)
        self.assertEqual(runtime_layering.runtime_profile.name, "exchange_simulated_spot")

        with self.assertLogs("aats.bootstrap.config", level="WARNING") as log_cm:
            _validate_startup_profile_settings(settings, runtime_layering)
        self.assertTrue(
            any("dev_simulated_exchange_runtime_allows_insecure_cookie" in msg for msg in log_cm.output),
            f"expected insecure_cookie warning in {log_cm.output!r}",
        )

    def test_dev_simulated_derivatives_insecure_cookie_passes_with_warning(self) -> None:
        settings = _dev_simulated_derivatives_settings()
        runtime_layering = resolve_runtime_layering(settings)
        self.assertTrue(runtime_layering.environment_capabilities.exchange_coupled)
        self.assertEqual(runtime_layering.runtime_profile.name, "exchange_simulated_derivatives")

        with self.assertLogs("aats.bootstrap.config", level="WARNING") as log_cm:
            _validate_startup_profile_settings(settings, runtime_layering)
        self.assertTrue(
            any("dev_simulated_exchange_runtime_allows_insecure_cookie" in msg for msg in log_cm.output),
            f"expected insecure_cookie warning in {log_cm.output!r}",
        )

    def test_prod_live_spot_insecure_cookie_still_raises(self) -> None:
        settings = _prod_live_spot_settings(operator_session_cookie_secure=False)
        runtime_layering = resolve_runtime_layering(settings)
        self.assertTrue(runtime_layering.environment_capabilities.exchange_coupled)

        with self.assertRaisesRegex(
            ValueError,
            "spot_exchange_runtime_requires_secure_operator_session_cookie",
        ):
            _validate_startup_profile_settings(settings, runtime_layering)

    def test_prod_live_derivatives_insecure_cookie_still_raises(self) -> None:
        settings = _prod_live_derivatives_settings(operator_session_cookie_secure=False)
        runtime_layering = resolve_runtime_layering(settings)
        self.assertTrue(runtime_layering.environment_capabilities.exchange_coupled)

        with self.assertRaisesRegex(
            ValueError,
            "derivatives_exchange_runtime_requires_secure_operator_session_cookie",
        ):
            _validate_startup_profile_settings(settings, runtime_layering)

    def test_dev_simulated_spot_secure_cookie_passes_without_warning(self) -> None:
        """cookie_secure 已经是 True 时不该出现放行 warning（走 if 外 return 路径）."""
        settings = _dev_simulated_spot_settings(operator_session_cookie_secure=True)
        runtime_layering = resolve_runtime_layering(settings)
        # 这里不能用 assertNoLogs（python 3.10+ 才有）。手动捕获 logger 并检查
        logger = logging.getLogger("aats.bootstrap.config")
        records: list[logging.LogRecord] = []
        handler = logging.Handler()
        handler.setLevel(logging.WARNING)
        handler.emit = records.append  # type: ignore[assignment]
        logger.addHandler(handler)
        try:
            _validate_startup_profile_settings(settings, runtime_layering)
        finally:
            logger.removeHandler(handler)
        self.assertFalse(
            any("dev_simulated_exchange_runtime_allows_insecure_cookie" in (r.getMessage() or "") for r in records),
            f"did not expect insecure_cookie warning; got {[r.getMessage() for r in records]}",
        )

    def test_dev_non_simulated_paper_local_insecure_cookie_not_validated(self) -> None:
        """非 exchange_coupled 的 paper_local runtime profile 根本走不到 cookie 检查，
        hardening gate 在 kind==None 时 early return."""
        # execution_backend=paper 就会把 runtime profile 切到 paper_local,
        # 这个 profile 的 exchange_coupled=False, _exchange_runtime_hardening_kind 返回 None.
        settings = _dev_simulated_spot_settings(
            execution_backend="paper",
            account_backend="disabled",
            account_read_enabled=False,
            okx_simulated_trading=False,
            config_profile="local_demo",
            startup_profile=None,
        )
        runtime_layering = resolve_runtime_layering(settings)
        self.assertFalse(runtime_layering.environment_capabilities.exchange_coupled)
        # 不抛错、也不触发放行 warning（hardening gate 直接 return None 了）
        _validate_startup_profile_settings(settings, runtime_layering)


class TestValidateOperatorAuthAdminUser(unittest.TestCase):
    """`_validate_operator_auth_settings` 的 admin user 放行分支."""

    def _make_storage_mock(self) -> MagicMock:
        storage = MagicMock(name="StorageBackends")
        storage.operator_repo = MagicMock(name="OperatorUserRepository")
        return storage

    def test_dev_simulated_spot_empty_admin_passes_with_warning(self) -> None:
        settings = _dev_simulated_spot_settings()
        storage = self._make_storage_mock()
        with patch("aats.bootstrap.config.enabled_admin_count", return_value=0):
            with self.assertLogs("aats.bootstrap.config", level="WARNING") as log_cm:
                _validate_operator_auth_settings(settings, storage)
        self.assertTrue(
            any("dev_simulated_exchange_runtime_allows_empty_admin_user" in msg for msg in log_cm.output),
            f"expected empty_admin_user warning in {log_cm.output!r}",
        )

    def test_dev_simulated_derivatives_empty_admin_passes_with_warning(self) -> None:
        settings = _dev_simulated_derivatives_settings()
        storage = self._make_storage_mock()
        with patch("aats.bootstrap.config.enabled_admin_count", return_value=0):
            with self.assertLogs("aats.bootstrap.config", level="WARNING") as log_cm:
                _validate_operator_auth_settings(settings, storage)
        self.assertTrue(
            any("dev_simulated_exchange_runtime_allows_empty_admin_user" in msg for msg in log_cm.output),
            f"expected empty_admin_user warning in {log_cm.output!r}",
        )

    def test_prod_live_spot_empty_admin_still_raises(self) -> None:
        settings = _prod_live_spot_settings()
        storage = self._make_storage_mock()
        with patch("aats.bootstrap.config.enabled_admin_count", return_value=0):
            with self.assertRaisesRegex(
                ValueError, "operator_session_auth_requires_enabled_admin_user"
            ):
                _validate_operator_auth_settings(settings, storage)

    def test_prod_live_derivatives_empty_admin_still_raises(self) -> None:
        settings = _prod_live_derivatives_settings()
        storage = self._make_storage_mock()
        with patch("aats.bootstrap.config.enabled_admin_count", return_value=0):
            with self.assertRaisesRegex(
                ValueError, "operator_session_auth_requires_enabled_admin_user"
            ):
                _validate_operator_auth_settings(settings, storage)

    def test_dev_simulated_with_existing_admin_passes_without_warning(self) -> None:
        settings = _dev_simulated_spot_settings()
        storage = self._make_storage_mock()
        with patch("aats.bootstrap.config.enabled_admin_count", return_value=1):
            logger = logging.getLogger("aats.bootstrap.config")
            records: list[logging.LogRecord] = []
            handler = logging.Handler()
            handler.setLevel(logging.WARNING)
            handler.emit = records.append  # type: ignore[assignment]
            logger.addHandler(handler)
            try:
                _validate_operator_auth_settings(settings, storage)
            finally:
                logger.removeHandler(handler)
        self.assertFalse(
            any(
                "dev_simulated_exchange_runtime_allows_empty_admin_user" in (r.getMessage() or "")
                for r in records
            ),
            f"did not expect empty_admin_user warning; got {[r.getMessage() for r in records]}",
        )

    def test_session_not_configured_short_circuits_before_admin_check(self) -> None:
        """operator_session_configured=False 时早返回，不检查 admin，也不打 warning."""
        settings = _dev_simulated_spot_settings(operator_session_secret="")
        storage = self._make_storage_mock()
        with patch("aats.bootstrap.config.enabled_admin_count") as admin_count:
            _validate_operator_auth_settings(settings, storage)
            admin_count.assert_not_called()


if __name__ == "__main__":
    unittest.main()
