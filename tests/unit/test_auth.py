from __future__ import annotations

import unittest
from types import SimpleNamespace

from aats.api.auth import authenticate_operator_user, _write_api_key_compatibility_enabled
from aats.schemas.operator import OperatorUserRecord
from aats.services.operator.passwords import hash_password
from aats.storage.operator_repo import InMemoryOperatorUserRepository


class TestAuth(unittest.TestCase):
    def test_authenticate_operator_user_locks_account_after_repeated_failures(self) -> None:
        repo = InMemoryOperatorUserRepository()
        repo.save_user(
            OperatorUserRecord(
                username="admin",
                password_hash=hash_password("correct-pass"),
                role="admin",
            )
        )
        runtime = SimpleNamespace(
            operator_repo=repo,
            settings=SimpleNamespace(
                operator_login_max_failed_attempts=2,
                operator_login_lockout_seconds=300,
                operator_write_api_key=None,
                environment="dev",
            ),
            environment_capabilities=SimpleNamespace(exchange_coupled=False),
        )

        first = authenticate_operator_user(runtime, username="admin", password="wrong-pass")
        second = authenticate_operator_user(runtime, username="admin", password="wrong-pass")
        locked = authenticate_operator_user(runtime, username="admin", password="correct-pass")

        self.assertIsNone(first.principal)
        self.assertEqual(first.failure_code, "operator_login_failed")
        self.assertIsNone(second.principal)
        self.assertEqual(second.failure_code, "operator_login_failed")
        self.assertIsNone(locked.principal)
        self.assertEqual(locked.failure_code, "operator_login_locked")

        stored_user = repo.get_by_username("admin")
        self.assertIsNotNone(stored_user)
        assert stored_user is not None
        self.assertEqual(stored_user.failed_login_attempts, 2)
        self.assertIsNotNone(stored_user.locked_until)

    def test_successful_login_clears_previous_failure_counters(self) -> None:
        repo = InMemoryOperatorUserRepository()
        repo.save_user(
            OperatorUserRecord(
                username="admin",
                password_hash=hash_password("correct-pass"),
                role="admin",
                failed_login_attempts=1,
            )
        )
        runtime = SimpleNamespace(
            operator_repo=repo,
            settings=SimpleNamespace(
                operator_login_max_failed_attempts=5,
                operator_login_lockout_seconds=300,
                operator_write_api_key=None,
                environment="dev",
            ),
            environment_capabilities=SimpleNamespace(exchange_coupled=False),
        )

        result = authenticate_operator_user(runtime, username="admin", password="correct-pass")

        self.assertIsNotNone(result.principal)
        stored_user = repo.get_by_username("admin")
        self.assertIsNotNone(stored_user)
        assert stored_user is not None
        self.assertEqual(stored_user.failed_login_attempts, 0)
        self.assertIsNone(stored_user.locked_until)

    def test_write_api_key_compatibility_disabled_for_prod_exchange_runtime(self) -> None:
        runtime = SimpleNamespace(
            settings=SimpleNamespace(
                operator_write_api_key="write-key",
                environment="prod",
            ),
            environment_capabilities=SimpleNamespace(exchange_coupled=True),
        )

        self.assertFalse(_write_api_key_compatibility_enabled(runtime))

    def test_write_api_key_compatibility_retained_for_non_exchange_dev_runtime(self) -> None:
        runtime = SimpleNamespace(
            settings=SimpleNamespace(
                operator_write_api_key="write-key",
                environment="dev",
            ),
            environment_capabilities=SimpleNamespace(exchange_coupled=False),
        )

        self.assertTrue(_write_api_key_compatibility_enabled(runtime))


if __name__ == "__main__":
    unittest.main()
