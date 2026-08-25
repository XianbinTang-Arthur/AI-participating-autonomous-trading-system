from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, Request, Response

from aats.api import auth as auth_module
from aats.api import auth_routes
from aats.api.auth import OperatorLoginResult, OperatorPrincipal
from aats.api.auth_routes import LoginRequest
from aats.bootstrap.settings import AATSSettings
from aats.schemas.operator import OperatorUserRecord
from aats.services.operator import passwords
from aats.services.operator.passwords import hash_password
from aats.storage.operator_repo import InMemoryOperatorUserRepository


def _login_settings(**updates):
    defaults = {
        "operator_login_max_concurrency": 1,
        "operator_login_queue_timeout_seconds": 0.03,
        "operator_login_rate_limit_window_seconds": 60.0,
        "operator_login_rate_limit_global_attempts": 60,
        "operator_login_rate_limit_client_attempts": 20,
        "operator_login_rate_limit_identity_attempts": 10,
    }
    defaults.update(updates)
    return SimpleNamespace(**defaults)


def _request(*, settings=None, client=("127.0.0.1", 43100), headers=None):
    app = FastAPI()
    runtime = SimpleNamespace(settings=settings or _login_settings())
    app.state.runtime = runtime
    request = Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/auth/login",
            "raw_path": b"/auth/login",
            "query_string": b"",
            "root_path": "",
            "headers": headers or [],
            "client": client,
            "server": ("127.0.0.1", 8001),
            "app": app,
        }
    )
    return request, runtime


async def _wait_for_thread_event(event: threading.Event) -> None:
    for _ in range(200):
        if event.is_set():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("worker thread did not start")


def _failed_attempt() -> auth_routes._OperatorLoginAttempt:
    return auth_routes._OperatorLoginAttempt(
        login_result=OperatorLoginResult(
            principal=None,
            failure_code="operator_login_failed",
        )
    )


@pytest.mark.asyncio
async def test_login_worker_runs_off_event_loop_and_loop_remains_schedulable(monkeypatch) -> None:
    request, _ = _request()
    worker_started = threading.Event()
    release_worker = threading.Event()
    worker_thread_ids: list[int] = []
    loop_thread_id = threading.get_ident()

    def _blocking_worker(*_args, **_kwargs):
        worker_thread_ids.append(threading.get_ident())
        worker_started.set()
        assert release_worker.wait(timeout=2.0)
        return _failed_attempt()

    monkeypatch.setattr(auth_routes, "_authenticate_operator_login_attempt", _blocking_worker)
    task = asyncio.create_task(
        auth_routes._run_operator_login_attempt(
            request,
            username="admin",
            password="wrong",
        )
    )
    await _wait_for_thread_event(worker_started)

    loop_progressed = False

    async def _mark_progress() -> None:
        nonlocal loop_progressed
        await asyncio.sleep(0)
        loop_progressed = True

    await asyncio.wait_for(_mark_progress(), timeout=0.1)
    assert loop_progressed is True
    assert worker_thread_ids == [worker_thread_ids[0]]
    assert worker_thread_ids[0] != loop_thread_id
    assert not task.done()

    release_worker.set()
    result = await task
    assert result.login_result.failure_code == "operator_login_failed"


@pytest.mark.asyncio
async def test_capacity_timeout_creates_no_second_worker(monkeypatch) -> None:
    request, _ = _request()
    worker_started = threading.Event()
    release_worker = threading.Event()
    worker_calls = 0

    def _blocking_worker(*_args, **_kwargs):
        nonlocal worker_calls
        worker_calls += 1
        worker_started.set()
        assert release_worker.wait(timeout=2.0)
        return _failed_attempt()

    monkeypatch.setattr(auth_routes, "_authenticate_operator_login_attempt", _blocking_worker)
    first = asyncio.create_task(
        auth_routes._run_operator_login_attempt(
            request,
            username="admin",
            password="wrong",
        )
    )
    await _wait_for_thread_event(worker_started)

    with pytest.raises(HTTPException) as raised:
        await auth_routes._run_operator_login_attempt(
            request,
            username="other",
            password="wrong",
        )
    assert raised.value.status_code == 503
    assert raised.value.detail == "operator_login_capacity_exhausted"
    assert raised.value.headers == {"Retry-After": "1"}
    assert worker_calls == 1

    release_worker.set()
    await first


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_release_capacity_before_worker_finishes(monkeypatch) -> None:
    request, _ = _request()
    worker_started = threading.Event()
    release_worker = threading.Event()
    worker_calls = 0

    def _blocking_worker(*_args, **_kwargs):
        nonlocal worker_calls
        worker_calls += 1
        worker_started.set()
        assert release_worker.wait(timeout=2.0)
        return _failed_attempt()

    monkeypatch.setattr(auth_routes, "_authenticate_operator_login_attempt", _blocking_worker)
    waiter = asyncio.create_task(
        auth_routes._run_operator_login_attempt(
            request,
            username="admin",
            password="wrong",
        )
    )
    await _wait_for_thread_event(worker_started)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    with pytest.raises(HTTPException) as raised:
        await auth_routes._run_operator_login_attempt(
            request,
            username="other",
            password="wrong",
        )
    assert raised.value.detail == "operator_login_capacity_exhausted"
    assert worker_calls == 1

    release_worker.set()
    for _ in range(200):
        entry = getattr(request.app.state, auth_routes._OPERATOR_LOGIN_TASKS_ATTR)
        if not entry[1]:
            break
        await asyncio.sleep(0.005)
    assert not entry[1]

    third = await auth_routes._run_operator_login_attempt(
        request,
        username="third",
        password="wrong",
    )
    assert third.login_result.failure_code == "operator_login_failed"
    assert worker_calls == 2


def test_rate_state_enforces_each_dimension_and_expires_old_keys() -> None:
    global_state = auth_routes._OperatorLoginRateState()
    assert global_state.check_and_record(
        now=1.0,
        window_seconds=60.0,
        global_limit=2,
        client_key="a",
        client_limit=10,
        identity_key="u1",
        identity_limit=10,
    ) is None
    assert global_state.check_and_record(
        now=2.0,
        window_seconds=60.0,
        global_limit=2,
        client_key="b",
        client_limit=10,
        identity_key="u2",
        identity_limit=10,
    ) is None
    assert global_state.check_and_record(
        now=3.0,
        window_seconds=60.0,
        global_limit=2,
        client_key="c",
        client_limit=10,
        identity_key="u3",
        identity_limit=10,
    ) == "global"

    client_state = auth_routes._OperatorLoginRateState()
    for identity in ("u1", "u2"):
        assert client_state.check_and_record(
            now=1.0,
            window_seconds=60.0,
            global_limit=10,
            client_key="same-client",
            client_limit=2,
            identity_key=identity,
            identity_limit=10,
        ) is None
    assert client_state.check_and_record(
        now=2.0,
        window_seconds=60.0,
        global_limit=10,
        client_key="same-client",
        client_limit=2,
        identity_key="u3",
        identity_limit=10,
    ) == "client"

    identity_state = auth_routes._OperatorLoginRateState()
    for client in ("a", "b"):
        assert identity_state.check_and_record(
            now=1.0,
            window_seconds=60.0,
            global_limit=10,
            client_key=client,
            client_limit=10,
            identity_key="same-user",
            identity_limit=2,
        ) is None
    assert identity_state.check_and_record(
        now=2.0,
        window_seconds=60.0,
        global_limit=10,
        client_key="c",
        client_limit=10,
        identity_key="same-user",
        identity_limit=2,
    ) == "identity"

    assert identity_state.check_and_record(
        now=62.0,
        window_seconds=60.0,
        global_limit=10,
        client_key="new",
        client_limit=10,
        identity_key="new-user",
        identity_limit=2,
    ) is None
    assert set(identity_state.client_attempts) == {"new"}
    assert set(identity_state.identity_attempts) == {"new-user"}


@pytest.mark.asyncio
async def test_rate_limit_response_is_generic_and_does_not_trust_forwarded_for() -> None:
    settings = _login_settings(
        operator_login_rate_limit_global_attempts=10,
        operator_login_rate_limit_client_attempts=10,
        operator_login_rate_limit_identity_attempts=1,
    )
    request, _ = _request(
        settings=settings,
        client=("127.0.0.1", 43100),
        headers=[(b"x-forwarded-for", b"203.0.113.9")],
    )
    assert auth_routes._operator_login_client_key(request) == "127.0.0.1"

    auth_routes._enforce_operator_login_rate_limit(request, username="Admin")
    with pytest.raises(HTTPException) as raised:
        auth_routes._enforce_operator_login_rate_limit(request, username=" admin ")
    assert raised.value.status_code == 429
    assert raised.value.detail == "operator_login_rate_limited"
    assert raised.value.headers == {"Retry-After": "60"}


@pytest.mark.parametrize(
    "username",
    [
        "",
        "u" * 129,
    ],
)
def test_login_username_length_failure_is_generic(username: str) -> None:
    payload = LoginRequest(username=username, password="candidate")
    with pytest.raises(HTTPException) as raised:
        auth_routes._login_username(payload)
    assert raised.value.status_code == 422
    assert raised.value.detail == "operator_login_payload_invalid"


@pytest.mark.parametrize("password", ["", "x" * 1025])
def test_login_password_length_failure_is_generic_and_secret_is_masked(password: str) -> None:
    payload = LoginRequest(username="admin", password=password)
    assert password not in repr(payload) if password else True
    with pytest.raises(HTTPException) as raised:
        auth_routes._login_password(payload)
    assert raised.value.status_code == 422
    assert raised.value.detail == "operator_login_payload_invalid"


@pytest.mark.parametrize(
    ("updates", "error_code"),
    [
        ({"operator_login_max_concurrency": 0}, "operator_login_max_concurrency"),
        ({"operator_login_max_concurrency": 33}, "operator_login_max_concurrency"),
        ({"operator_login_queue_timeout_seconds": 0}, "queue_timeout"),
        ({"operator_login_queue_timeout_seconds": float("nan")}, "queue_timeout"),
        ({"operator_login_queue_timeout_seconds": float("inf")}, "queue_timeout"),
        ({"operator_login_rate_limit_window_seconds": 0}, "rate_limit_window"),
        (
            {"operator_login_rate_limit_window_seconds": float("nan")},
            "rate_limit_window",
        ),
        (
            {"operator_login_rate_limit_window_seconds": float("inf")},
            "rate_limit_window",
        ),
        ({"operator_login_rate_limit_global_attempts": 0}, "global_attempts"),
        (
            {
                "operator_login_rate_limit_global_attempts": 5,
                "operator_login_rate_limit_client_attempts": 6,
            },
            "client_attempts",
        ),
        (
            {
                "operator_login_rate_limit_global_attempts": 5,
                "operator_login_rate_limit_client_attempts": 5,
                "operator_login_rate_limit_identity_attempts": 6,
            },
            "identity_attempts",
        ),
    ],
)
def test_login_protection_settings_fail_closed(updates, error_code) -> None:
    with pytest.raises(ValueError, match=error_code):
        AATSSettings.model_validate(updates)


def test_malformed_or_excessive_iteration_hash_runs_bounded_dummy_kdf(monkeypatch) -> None:
    calls: list[int] = []

    def _fake_pbkdf2(_name, _password, _salt, iterations):
        calls.append(iterations)
        return b"\x00" * 32

    monkeypatch.setattr(passwords.hashlib, "pbkdf2_hmac", _fake_pbkdf2)
    assert passwords.verify_password("secret", "malformed") is False
    assert passwords.verify_password(
        "secret",
        "pbkdf2_sha256$1000001$YQ$Yg",
    ) is False
    assert calls == [390_000, 390_000]


@pytest.mark.parametrize("enabled", [True, False])
def test_missing_or_disabled_user_consumes_dummy_kdf(monkeypatch, enabled: bool) -> None:
    repo = InMemoryOperatorUserRepository()
    username = "missing"
    if not enabled:
        username = "disabled"
        repo.save_user(
            OperatorUserRecord(
                username=username,
                password_hash="unused",
                role="viewer",
                enabled=False,
            )
        )
    dummy_passwords: list[str] = []
    monkeypatch.setattr(
        auth_module,
        "consume_dummy_password_verification",
        lambda password: dummy_passwords.append(password) or False,
    )
    runtime = SimpleNamespace(operator_repo=repo)

    result = auth_module.authenticate_operator_user(
        runtime,
        username=username,
        password="candidate",
    )
    assert result.failure_code == "operator_login_failed"
    assert dummy_passwords == ["candidate"]


def test_synchronous_attempt_keeps_existing_success_audit_and_session_version() -> None:
    repo = InMemoryOperatorUserRepository()
    repo.save_user(
        OperatorUserRecord(
            username="admin",
            password_hash=hash_password("correct"),
            role="admin",
        )
    )
    runtime = SimpleNamespace(
        operator_repo=repo,
        settings=SimpleNamespace(
            operator_login_max_failed_attempts=5,
            operator_login_lockout_seconds=300,
        ),
    )
    successful_audits: list[dict] = []
    failed_audits: list[dict] = []
    query_service = SimpleNamespace(
        record_operator_login=lambda **kwargs: successful_audits.append(kwargs),
        record_operator_login_failure=lambda **kwargs: failed_audits.append(kwargs),
    )

    attempt = auth_routes._authenticate_operator_login_attempt(
        runtime,
        username="admin",
        password="correct",
        query_service=query_service,
    )
    assert attempt.login_result.principal is not None
    assert attempt.session_version == 1
    assert successful_audits == [
        {
            "actor_identity": "admin",
            "actor_role": "admin",
            "auth_source": "session",
        }
    ]
    assert failed_audits == []


@pytest.mark.asyncio
async def test_auth_login_uses_bounded_attempt_result_to_issue_session(monkeypatch) -> None:
    settings = AATSSettings.model_validate(
        {
            "operator_auth_enabled": True,
            "operator_session_secret": "fs019-test-session-secret",
            "operator_session_cookie_secure": False,
        }
    )
    request, _ = _request(settings=settings)
    response = Response()
    calls: list[tuple[str, str]] = []

    async def _completed_attempt(_request, *, username: str, password: str):
        calls.append((username, password))
        return auth_routes._OperatorLoginAttempt(
            login_result=OperatorLoginResult(
                principal=OperatorPrincipal(
                    identity=username,
                    role="admin",
                    auth_enabled=True,
                    auth_source="session",
                )
            ),
            session_version=7,
        )

    monkeypatch.setattr(auth_routes, "_run_operator_login_attempt", _completed_attempt)
    payload = await auth_routes.auth_login(
        request,
        LoginRequest(username="admin", password="correct"),
        response,
    )

    assert payload == {
        "authenticated": True,
        "identity": "admin",
        "role": "admin",
        "auth_source": "session",
    }
    assert calls == [("admin", "correct")]
    set_cookie = response.headers["set-cookie"]
    assert "aats_operator_session=" in set_cookie
    assert "HttpOnly" in set_cookie
