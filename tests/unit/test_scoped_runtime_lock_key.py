from __future__ import annotations

import unittest

from aats.storage.session import scoped_runtime_lock_key


_DATABASE_URL = "postgresql+psycopg://user:pass@localhost:5432/aats?options=-csearch_path%3Daats"
_BASE_LOCK_KEY = 4242


class TestScopedRuntimeLockKey(unittest.TestCase):
    def test_default_role_matches_monolith(self) -> None:
        without_role = scoped_runtime_lock_key(
            database_url=_DATABASE_URL,
            base_lock_key=_BASE_LOCK_KEY,
        )
        monolith = scoped_runtime_lock_key(
            database_url=_DATABASE_URL,
            base_lock_key=_BASE_LOCK_KEY,
            process_role="monolith",
        )
        self.assertEqual(without_role, monolith)

    def test_each_role_has_distinct_lock_key(self) -> None:
        roles = ("gateway", "market", "decision", "execution", "monolith")
        derived = {
            role: scoped_runtime_lock_key(
                database_url=_DATABASE_URL,
                base_lock_key=_BASE_LOCK_KEY,
                process_role=role,
            )
            for role in roles
        }
        # All five roles must derive different lock keys; otherwise two processes would block each other.
        self.assertEqual(len(set(derived.values())), len(roles))

    def test_role_token_is_normalized(self) -> None:
        lower = scoped_runtime_lock_key(
            database_url=_DATABASE_URL,
            base_lock_key=_BASE_LOCK_KEY,
            process_role="gateway",
        )
        upper = scoped_runtime_lock_key(
            database_url=_DATABASE_URL,
            base_lock_key=_BASE_LOCK_KEY,
            process_role="GATEWAY",
        )
        padded = scoped_runtime_lock_key(
            database_url=_DATABASE_URL,
            base_lock_key=_BASE_LOCK_KEY,
            process_role="  gateway  ",
        )
        self.assertEqual(lower, upper)
        self.assertEqual(lower, padded)

    def test_empty_role_falls_back_to_monolith(self) -> None:
        empty = scoped_runtime_lock_key(
            database_url=_DATABASE_URL,
            base_lock_key=_BASE_LOCK_KEY,
            process_role="   ",
        )
        none_role = scoped_runtime_lock_key(
            database_url=_DATABASE_URL,
            base_lock_key=_BASE_LOCK_KEY,
            process_role=None,
        )
        self.assertEqual(empty, none_role)

    def test_lock_key_within_postgres_bigint_range(self) -> None:
        # pg_try_advisory_lock(bigint) requires the value to fit in a signed 64-bit integer.
        # We mask to 63 bits inside the helper to avoid sign issues.
        max_signed_bigint = (1 << 63) - 1
        for role in ("gateway", "market", "decision", "execution", "monolith"):
            with self.subTest(role=role):
                derived = scoped_runtime_lock_key(
                    database_url=_DATABASE_URL,
                    base_lock_key=_BASE_LOCK_KEY,
                    process_role=role,
                )
                self.assertGreater(derived, 0)
                self.assertLessEqual(derived, max_signed_bigint)

    def test_database_isolation_changes_lock_key(self) -> None:
        # Two databases on the same host must produce different lock keys for the same role.
        url_a = "postgresql+psycopg://user:pass@localhost:5432/aats_a"
        url_b = "postgresql+psycopg://user:pass@localhost:5432/aats_b"
        derived_a = scoped_runtime_lock_key(
            database_url=url_a,
            base_lock_key=_BASE_LOCK_KEY,
            process_role="execution",
        )
        derived_b = scoped_runtime_lock_key(
            database_url=url_b,
            base_lock_key=_BASE_LOCK_KEY,
            process_role="execution",
        )
        self.assertNotEqual(derived_a, derived_b)

    def test_search_path_changes_lock_key(self) -> None:
        # Two schemas in the same database must derive different lock keys.
        url_a = "postgresql+psycopg://user:pass@localhost:5432/aats?options=-csearch_path%3Daats"
        url_b = "postgresql+psycopg://user:pass@localhost:5432/aats?options=-csearch_path%3Daats_dev"
        derived_a = scoped_runtime_lock_key(
            database_url=url_a,
            base_lock_key=_BASE_LOCK_KEY,
            process_role="execution",
        )
        derived_b = scoped_runtime_lock_key(
            database_url=url_b,
            base_lock_key=_BASE_LOCK_KEY,
            process_role="execution",
        )
        self.assertNotEqual(derived_a, derived_b)


if __name__ == "__main__":
    unittest.main()
