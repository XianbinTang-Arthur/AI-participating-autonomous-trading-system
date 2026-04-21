from __future__ import annotations

import unittest

from aats.storage.session import DatabaseRuntime


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _FakeConnection:
    def __init__(self, *, acquire_result=True) -> None:
        self.acquire_result = acquire_result
        self.executed: list[tuple[str, dict]] = []
        self.closed = False
        self.commit_count = 0

    def execute(self, statement, params=None):
        sql = str(statement)
        bound = params or {}
        self.executed.append((sql, bound))
        if "pg_try_advisory_lock" in sql:
            return _ScalarResult(self.acquire_result)
        return _ScalarResult(True)

    def commit(self) -> None:
        # 2026-04-21 fix：advisory_lock 获取后 commit 关 tx，让 connection 处于
        # idle 而非 idle_in_transaction，避免 PG 端 idle_in_transaction_session
        # _timeout 误杀 session-level lock 持有连接。
        self.commit_count += 1

    def close(self) -> None:
        self.closed = True


class _FakeEngine:
    def __init__(self, *, dialect_name: str, acquire_result=True) -> None:
        self.dialect = type("Dialect", (), {"name": dialect_name})()
        self._connection = _FakeConnection(acquire_result=acquire_result)
        self.disposed = False

    def connect(self):
        return self._connection

    def dispose(self) -> None:
        self.disposed = True


class TestDatabaseRuntimeGuard(unittest.TestCase):
    def test_postgres_runtime_acquires_and_releases_advisory_lock(self) -> None:
        engine = _FakeEngine(dialect_name="postgresql", acquire_result=True)
        runtime = DatabaseRuntime(engine=engine, session_factory=None)  # type: ignore[arg-type]

        runtime.acquire_single_runtime_lock(12345)
        runtime.dispose()

        self.assertEqual(runtime.runtime_lock_connection, None)
        self.assertEqual(runtime.runtime_lock_key, None)
        self.assertTrue(engine.disposed)
        executed_sql = [sql for sql, _params in engine._connection.executed]
        self.assertTrue(any("pg_try_advisory_lock" in sql for sql in executed_sql))
        self.assertTrue(any("pg_advisory_unlock" in sql for sql in executed_sql))
        self.assertTrue(engine._connection.closed)
        # 2026-04-21 anchor: acquire 后必须 commit 关 tx，否则 connection 处于
        # idle_in_transaction 会被 PG idle_in_transaction_session_timeout=60s
        # 误杀，丢失 session-level advisory_lock。
        self.assertEqual(engine._connection.commit_count, 1)

    def test_postgres_runtime_raises_when_lock_cannot_be_acquired(self) -> None:
        engine = _FakeEngine(dialect_name="postgresql", acquire_result=False)
        runtime = DatabaseRuntime(engine=engine, session_factory=None)  # type: ignore[arg-type]

        with self.assertRaisesRegex(RuntimeError, "database_single_runtime_lock_not_acquired"):
            runtime.acquire_single_runtime_lock(12345)

        self.assertTrue(engine._connection.closed)
        self.assertFalse(engine.disposed)

    def test_non_postgres_runtime_skips_locking(self) -> None:
        engine = _FakeEngine(dialect_name="mysql", acquire_result=True)
        runtime = DatabaseRuntime(engine=engine, session_factory=None)  # type: ignore[arg-type]

        runtime.acquire_single_runtime_lock(12345)
        runtime.dispose()

        self.assertEqual(engine._connection.executed, [])
        self.assertTrue(engine.disposed)


if __name__ == "__main__":
    unittest.main()
