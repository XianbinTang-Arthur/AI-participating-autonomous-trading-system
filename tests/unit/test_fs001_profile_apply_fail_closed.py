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


class FS001ProfileApplyFailClosedTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.sessions: list[_FakeSession] = []
        self.request = Request(
            {"type": "http", "method": "POST", "path": "/", "headers": []}
        )
        self.principal = OperatorPrincipal(
            identity="apply_actor",
            role="admin",
            auth_enabled=True,
            auth_source="session",
        )

    @contextmanager
    def _governance_session(self):
        session = _FakeSession()
        self.sessions.append(session)
        yield session

    async def _invoke(self, *, rec: dict[str, object]) -> None:
        with (
            patch.object(
                routes,
                "_extract_profile_token",
                return_value="apply_actor",
            ),
            patch.object(routes, "_load_profile_rec", return_value=rec),
            patch.object(
                routes,
                "_governance_session",
                self._governance_session,
            ),
            patch.object(
                routes,
                "_load_parameter_set",
                side_effect=AssertionError("parameter set must not be loaded"),
            ),
            patch.object(
                routes,
                "_load_current_active_profile_values",
                side_effect=AssertionError("active profile must not be loaded"),
            ),
            patch.object(
                routes,
                "_compute_threshold_patches",
                side_effect=AssertionError("threshold patch must not be computed"),
            ),
            patch(
                "aats.data_platform.governance.profile_apply_saga.find_or_create_saga_operation",
                side_effect=AssertionError("saga operation must not be created"),
            ),
            patch(
                "aats.data_platform.governance.profile_apply_saga.apply_profile_saga",
                side_effect=AssertionError("saga must not run"),
            ),
            patch(
                "aats.data_platform.runtime.live_session.get_live_session",
                side_effect=AssertionError("live session must not be opened"),
            ),
        ):
            await routes.apply_profile_rec(
                "rec_fs001_apply",
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
            == "/rdp/profile-recommendations/{rec_id}/apply"
        )
        self.assertEqual(route.status_code, 501)

    async def test_valid_request_fails_closed_without_runtime_claim(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            await self._invoke(
                rec={
                    "approved_by": "different_approver",
                    "status": "released",
                }
            )

        error = raised.exception
        self.assertEqual(error.status_code, 501)
        self.assertEqual(error.detail["code"], "profile_apply_not_implemented")
        self.assertEqual(error.detail["current_status"], "released")
        self.assertFalse(error.detail["retryable"])
        self.assertNotIn("ok", error.detail)
        self.assertNotIn("operation_id", error.detail)
        self.assertNotIn("steps_completed", error.detail)
        self.assertNotIn("values", error.detail)
        self._assert_no_writes()

    async def test_non_released_recommendation_keeps_status_guard(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            await self._invoke(
                rec={
                    "approved_by": "different_approver",
                    "status": "approved",
                }
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["code"], "rec_status_not_released")
        self._assert_no_writes()

    async def test_dual_operator_check_precedes_fail_closed_response(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            await self._invoke(
                rec={
                    "approved_by": "apply_actor",
                    "status": "released",
                }
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["code"], "approver_equals_applier")
        self._assert_no_writes()

    async def test_duplicate_requests_are_idempotent_and_write_nothing(self) -> None:
        rec = {
            "approved_by": "different_approver",
            "status": "released",
        }
        for _ in range(3):
            with self.assertRaises(HTTPException) as raised:
                await self._invoke(rec=rec)
            self.assertEqual(raised.exception.status_code, 501)

        self.assertEqual(len(self.sessions), 3)
        self._assert_no_writes()


if __name__ == "__main__":
    unittest.main()
