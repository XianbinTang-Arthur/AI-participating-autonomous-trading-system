from __future__ import annotations

import unittest
from unittest.mock import patch, sentinel

from aats.storage.session import create_database_runtime


class TestCreateDatabaseRuntimeOptions(unittest.TestCase):
    def test_preserves_search_path_and_appends_idle_timeout_option(self) -> None:
        database_url = (
            "postgresql+psycopg://user:pass@localhost:5432/aats"
            "?options=-csearch_path%3Daats_test_schema"
        )
        with (
            patch("aats.storage.session.create_engine", return_value=sentinel.engine) as create_engine_mock,
            patch("aats.storage.session.sessionmaker", return_value=sentinel.session_factory) as sessionmaker_mock,
        ):
            runtime = create_database_runtime(database_url)

        create_engine_mock.assert_called_once()
        self.assertEqual(runtime.engine, sentinel.engine)
        self.assertEqual(runtime.session_factory, sentinel.session_factory)
        self.assertEqual(
            create_engine_mock.call_args.kwargs["connect_args"]["options"],
            "-csearch_path=aats_test_schema -c idle_in_transaction_session_timeout=60000",
        )
        sessionmaker_mock.assert_called_once_with(bind=sentinel.engine, expire_on_commit=False, future=True)

    def test_adds_idle_timeout_when_no_existing_options(self) -> None:
        database_url = "postgresql+psycopg://user:pass@localhost:5432/aats"
        with (
            patch("aats.storage.session.create_engine", return_value=sentinel.engine) as create_engine_mock,
            patch("aats.storage.session.sessionmaker", return_value=sentinel.session_factory),
        ):
            create_database_runtime(database_url)

        self.assertEqual(
            create_engine_mock.call_args.kwargs["connect_args"]["options"],
            "-c idle_in_transaction_session_timeout=60000",
        )

    def test_does_not_duplicate_idle_timeout_option(self) -> None:
        database_url = (
            "postgresql+psycopg://user:pass@localhost:5432/aats"
            "?options=-csearch_path%3Daats_test_schema%20-c%20idle_in_transaction_session_timeout%3D60000"
        )
        with (
            patch("aats.storage.session.create_engine", return_value=sentinel.engine) as create_engine_mock,
            patch("aats.storage.session.sessionmaker", return_value=sentinel.session_factory),
        ):
            create_database_runtime(database_url)

        self.assertEqual(
            create_engine_mock.call_args.kwargs["connect_args"]["options"],
            "-csearch_path=aats_test_schema -c idle_in_transaction_session_timeout=60000",
        )


if __name__ == "__main__":
    unittest.main()
