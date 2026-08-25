from __future__ import annotations

import unittest
from contextlib import contextmanager
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

from aats.api import rdp_profile_routes as routes
from aats.api.auth import OperatorPrincipal


class _FakeSession:
    def __init__(self) -> None:
        self.execute_calls: list[tuple[object, object]] = []
        self.commits = 0

    def execute(self, statement: object, params: object = None) -> object:
        self.execute_calls.append((statement, params))
        return object()

    def commit(self) -> None:
        self.commits += 1


class FS001ProfileRollbackFailClosedTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.sessions: list[_FakeSession] = []
        self.request = Request(
            {"type": "http", "method": "POST", "path": "/", "headers": []}
        )
        self.principal = OperatorPrincipal(
            identity="rollback_actor",
            role="admin",
            auth_enabled=True,
            auth_source="session",
        )

    @contextmanager
    def _governance_session(self):
        session = _FakeSession()
        self.sessions.append(session)
        yield session

    async def _invoke(
        self,
        *,
        rec: dict[str, object],
        target: str | None = "ps_previous",
    ) -> None:
        with (
            patch.object(
                routes,
                "_extract_profile_token",
                return_value="rollback_actor",
            ),
            patch.object(routes, "_load_profile_rec", return_value=rec),
            patch.object(
                routes,
                "_governance_session",
                self._governance_session,
            ),
        ):
            await routes.rollback_profile_rec(
                "rec_fs001",
                routes._RollbackRequest(to_parameter_set_id=target),
                self.request,
                self.principal,
            )

    def _assert_no_writes(self) -> None:
        self.assertTrue(self.sessions)
        self.assertEqual(
            sum(len(session.execute_calls) for session in self.sessions),
            0,
        )
        self.assertEqual(sum(session.commits for session in self.sessions), 0)

    async def test_openapi_declares_fail_closed_status(self) -> None:
        route = next(
            item
            for item in routes.profile_router.routes
            if getattr(item, "path", "")
            == "/rdp/profile-recommendations/{rec_id}/rollback"
        )
        self.assertEqual(route.status_code, 501)

    async def test_valid_request_fails_closed_without_false_terminal_state(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            await self._invoke(
                rec={
                    "approved_by": "different_approver",
                    "status": "applied",
                }
            )

        error = raised.exception
        self.assertEqual(error.status_code, 501)
        self.assertEqual(error.detail["code"], "profile_rollback_not_implemented")
        self.assertEqual(error.detail["current_status"], "applied")
        self.assertEqual(error.detail["requested_parameter_set_id"], "ps_previous")
        self.assertFalse(error.detail["retryable"])
        self.assertNotIn("ok", error.detail)
        self.assertNotEqual(error.detail.get("status"), "rolled_back")
        self._assert_no_writes()

    async def test_dual_operator_check_still_precedes_fail_closed_response(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            await self._invoke(
                rec={
                    "approved_by": "rollback_actor",
                    "status": "applied",
                }
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["code"], "approver_equals_applier")
        self._assert_no_writes()

    async def test_duplicate_requests_are_idempotent_and_write_nothing(self) -> None:
        rec = {
            "approved_by": "different_approver",
            "status": "applied",
        }
        for _ in range(3):
            with self.assertRaises(HTTPException) as raised:
                await self._invoke(rec=rec, target=None)
            self.assertEqual(raised.exception.status_code, 501)
            self.assertIsNone(raised.exception.detail["requested_parameter_set_id"])

        self.assertEqual(len(self.sessions), 3)
        self._assert_no_writes()


if __name__ == "__main__":
    unittest.main()
