from __future__ import annotations

import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import requests
import uvicorn
from fastapi import Depends, FastAPI, Query
from fastapi.responses import HTMLResponse
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.edge.options import Options as EdgeOptions
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from aats.api.auth import OperatorPrincipal, require_read_access
from aats.api.auth_routes import auth_router
from aats.api.ui import ui_router
from aats.bootstrap.settings import AATSSettings
from aats.schemas.operator import OperatorUserRecord
from aats.services.operator.passwords import hash_password
from aats.storage.operator_repo import InMemoryOperatorUserRepository

_SELF_SIGNED_CERT_PEM = """-----BEGIN CERTIFICATE-----
MIIDJTCCAg2gAwIBAgIUKwKPmFK+C/qTknXQ2PPNkfl+XAcwDQYJKoZIhvcNAQEL
BQAwFDESMBAGA1UEAwwJbG9jYWxob3N0MB4XDTI2MDQxNzAyMzg0N1oXDTI2MDQx
ODAyMzg0N1owFDESMBAGA1UEAwwJbG9jYWxob3N0MIIBIjANBgkqhkiG9w0BAQEF
AAOCAQ8AMIIBCgKCAQEAhQvHP1jQujie60l/DxcNMWX9kfd2WF590yqoOwJvEDqp
Xvr0DrHmRPGXnYlQ5+ABAxl+G+GvgorRCugMXtVwzbLxhKnPv6cZaiob5DOEdR2r
B08GaXsMC68nZ/jFpMNeU9ztDukt/OZgeHcIuVsi3H/M+EFKiipTqiDJLNXs1Pln
pTvOvbe15zQsQpokCqgzbfwo1VB3JPYV8AMS4SmMUezF6wqL4+qQ33swN0qVq726
4SVoBCpE4SG4lxJSHLq8lsOebL91GPFU/ms0T4o8KVwkGxxLwznM+ECXMbvw4n2y
Wpc5v6u5cCXGi+yNdt6A6HpmVdW61jfFcwcJsDxCRQIDAQABo28wbTAdBgNVHQ4E
FgQUaYvTWABAlSsUdcKuew3/SAimOvQwHwYDVR0jBBgwFoAUaYvTWABAlSsUdcKu
ew3/SAimOvQwDwYDVR0TAQH/BAUwAwEB/zAaBgNVHREEEzARgglsb2NhbGhvc3SH
BH8AAAEwDQYJKoZIhvcNAQELBQADggEBAFsoXef6MJnQBkWba26ecYO7YVEqXGrm
oS2eFlv6hLd0tHd2kRPLMTqChiw1uWKRBwwPQSipbbAq/T9eokEOXNFPuXUJ8Rfd
f1lT1lW3b8aKd3nHTVp5WhnFGS08uZbndkPAiZuMkeqHUMyHPotj1he9iRJya24/
4EHzd6UjS4KnWtTrsR9miY7OmGKabf6SuUBLdywyDYwjz/A7PFcR2zUPs9j9PMf1
T7SHl+NaapH89B4PMwuTiVtfhi5wFxYEZAPUUYMMkJjYpiLQdDjZysGZScjH481z
70vui4Vx/wZJVFPh5+LBpBiHfIkf73e2pNbBkAmjJqK8sbQAVa6X86I=
-----END CERTIFICATE-----
"""

_SELF_SIGNED_KEY_PEM = """-----BEGIN PRIVATE KEY-----
MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQCFC8c/WNC6OJ7r
SX8PFw0xZf2R93ZYXn3TKqg7Am8QOqle+vQOseZE8ZediVDn4AEDGX4b4a+CitEK
6Axe1XDNsvGEqc+/pxlqKhvkM4R1HasHTwZpewwLrydn+MWkw15T3O0O6S385mB4
dwi5WyLcf8z4QUqKKlOqIMks1ezU+WelO869t7XnNCxCmiQKqDNt/CjVUHck9hXw
AxLhKYxR7MXrCovj6pDfezA3SpWrvbrhJWgEKkThIbiXElIcuryWw55sv3UY8VT+
azRPijwpXCQbHEvDOcz4QJcxu/DifbJalzm/q7lwJcaL7I123oDoemZV1brWN8Vz
BwmwPEJFAgMBAAECggEAIECFsX6HQs1dAO6VJYRSB2qQ+KSDhNKLL/iERaHGaKm/
yy2Mok9P4eCq/159RWiQ9j5kyr9/+2ZJXJp5/TdCnCrHz4AWw3/vckP49O3kKzvg
7OmkRUe12NNB+ztcNh/CKxRAR0ARjOAP/MPmUoCcR9WXS4sQVcQC+hfujLbjLZFa
q19S1JJJXkKnpMBdST10+738W1Hq81XcAXa+uKJX2HqXBzDSLqGhcEsCGkgKl1Gh
PgWH+VrGW1yhHE/EmD0Z1QpgjrZFgykLiF2BjcPxfO0aWlE7MbXZ6cdFk4lbTn9g
WRnAhseIDoaVHp3MK7wkQBdMBdx/Hy19Qjt84Q5olQKBgQC31Rkto8VON2+jR0BT
cO4NYYHskMsFiSbH3NSYqJFzqsmtozIHbTl7PJvVcoqYnEMjbeXlDQl2r/6bXlQ8
sYgAK0v3Cr71d4FWq6gdQIWFuBlu0w0j1zKz04iZxQbRoLG26i7W+RuGu5l0zg+f
aP/pmgMPw4Dl9pFt4o/lhNN+gwKBgQC5RreEgESFngKK5JDXxwsVrIUw82qDZOnC
Iy/Dd+NbLGxnjEiVOxb8sXsB7bFb9wfaLt1g6zEeBHxs46XA2ngKU+fYOOoc5PYh
M/vsvUwb3WQLCEmXrBJBSfRpNoSS4Dua4KOHjg0dawKyb/hgCN6CvdYBQzhoNRdn
09+fE69hlwKBgEkfvh02eOSNDp4/WGoYkMjH0ZudWPTBwqhbwkFbREhjVkf4k4z6
uJO53y7/mfvspJQyQfFjxzDr/vYkhpOB9txCXLxPDPitachlDcFdCf/P5GX+E8r+
7g80BLFN+1Flf2uIKcufWYC1nOwmj3ZUmP9+INujY+GVu/Ge3qhotowrAoGAd4FF
nhGeIRFqUmxbgNLCM5iz0H8xlM7ieHZ5uHr8CzL8OU4jAx66FQPlc7j9TXRpfDH+
WSVa6SG7oAC2SU6hXwf/41fSqhCFMaV2OZ1gGhkTDoqp7Urv+2zYWYTwvkwkJiH/
WNAnZXJAqxfN/SO7YllQUEArggu8rRvcgZ8Q4MECgYEAg3sniQcnVWQ6MTtwnUUy
byh7oeNROjboPI2VgtNoOngKnCx1+RUZtwadHdwdcdVDdrqcJs81nWkA7cLtluLk
47fOYuhzas6nVMpz3QEiA1BEVmOseYLtHDdq1tmeL2j1yynvGvTmwrgdNcGzUbl4
vgVRj/5pYmQuvAt4C0rH+kw=
-----END PRIVATE KEY-----
"""


def _build_browser(*, ignore_certificate_errors: bool = False) -> webdriver.Edge:
    options = EdgeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1440,1200")
    if ignore_certificate_errors:
        options.add_argument("--ignore-certificate-errors")
    return webdriver.Edge(options=options)


def _build_runtime(*, secure_cookie_required: bool) -> SimpleNamespace:
    settings = AATSSettings.model_validate(
        {
            "operator_auth_enabled": True,
            "operator_session_secret": "session-secret",
            "operator_session_cookie_secure": secure_cookie_required,
        }
    )
    repo = InMemoryOperatorUserRepository()
    repo.save_user(
        OperatorUserRecord(
            username="admin",
            password_hash=hash_password("admin-pass"),
            role="admin",
        )
    )
    return SimpleNamespace(
        settings=settings,
        operator_repo=repo,
        database_runtime=None,
        environment_capabilities=SimpleNamespace(local_only=False),
    )


def _build_app(*, secure_cookie_required: bool, include_bundle: bool = False) -> FastAPI:
    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(ui_router)
    app.state.runtime = _build_runtime(secure_cookie_required=secure_cookie_required)

    if include_bundle:

        @app.get("/e2e/rdp-panel")
        async def _rdp_panel(_principal: OperatorPrincipal = Depends(require_read_access)):
            return {"headline": "RDP 核心面板已就绪"}

        @app.get("/e2e/auth-dashboard", response_class=HTMLResponse)
        async def _auth_dashboard_page() -> str:
            return """
<!doctype html>
<html lang="zh-CN">
  <head><meta charset="utf-8"><title>Operator Auth E2E</title></head>
  <body>
    <div id="status">加载中</div>
    <script type="module">
      async function main() {
        const root = document.getElementById("status");
        try {
          const response = await fetch("/e2e/rdp-panel");
          const payload = await response.json();
          if (!response.ok) {
            root.textContent = payload.detail || `HTTP_${response.status}`;
            return;
          }
          root.textContent = payload.headline;
        } catch (error) {
          root.textContent = `FETCH_ERROR:${error?.message || "unknown"}`;
        }
      }
      void main();
    </script>
  </body>
</html>
            """

        @app.get("/dashboard/bundle")
        async def _dashboard_bundle(
            view: str = "overview",
            panel: list[str] = Query(default=[]),
            _principal: OperatorPrincipal = Depends(require_read_access),
        ) -> dict[str, object]:
            panel_data = {
                "session": {
                    "authenticated": True,
                    "identity": "admin",
                    "role": "admin",
                    "auth_enabled": True,
                    "request_scheme": "https",
                    "secure_cookie_required": True,
                    "transport_compatible": True,
                    "required_transport": "https",
                    "auth_blocked_reason": None,
                },
                "authProviders": {
                    "auth_enabled": True,
                    "session_enabled": True,
                    "database_backed": True,
                    "configured_roles": ["admin"],
                    "stored_user_count": 1,
                    "request_scheme": "https",
                    "secure_cookie_required": True,
                    "transport_compatible": True,
                    "required_transport": "https",
                    "auth_blocked_reason": None,
                },
                "health": {
                    "runtime_state": "running",
                    "overall_status": "running",
                    "halted": False,
                },
                "mode": {
                    "default_symbol": "BTC-USDT-SWAP",
                },
                "runtime": {
                    "symbols": ["BTC-USDT-SWAP"],
                },
                "systemRecovery": {
                    "recovery": {
                        "safe_to_trade": True,
                        "halted": False,
                        "resume_eligible": True,
                        "review_required": False,
                    }
                },
                "blockerControl": {
                    "blockers": [],
                    "primary_blocker": None,
                },
                "blockers": {
                    "blockers": [],
                },
                "metrics": {
                    "decision_cycle_count": 3,
                    "order_intent_count": 2,
                    "fill_count": 1,
                    "reconciliation_mismatch_count": 0,
                    "current_open_order_count": 0,
                },
                "portfolio": {
                    "portfolio": {
                        "total_equity": 1200,
                        "unrealized_pnl": 12,
                        "realized_pnl": 8,
                        "gross_exposure": 300,
                        "net_exposure": 150,
                        "positions": [],
                    }
                },
                "positions": {
                    "local_instrument_positions": [],
                },
                "latestDecision": {
                    "decision_id": "dec-e2e-1",
                    "decision_time": "2026-04-17T04:00:00Z",
                    "decision_context": {
                        "as_of_ts": "2026-04-17T04:00:00Z",
                        "symbol": "BTC-USDT-SWAP",
                    },
                    "position_target": {
                        "delta_position_qty": 0.01,
                    },
                    "policy_decision": {
                        "execution_allowed": True,
                    },
                    "risk_decision": {
                        "approved": True,
                    },
                },
                "executionLatest": {
                    "latest_order": {
                        "status": "created",
                        "client_order_id": "ord-e2e-1",
                    },
                    "latest_fill": None,
                },
                "reconciliationLatest": {
                    "reconciliation": {
                        "reconciliation_id": "recon-e2e-1",
                        "severity": "ok",
                        "halt_required": False,
                    }
                },
                "accountState": {
                    "connected": True,
                    "fresh": True,
                    "ready": True,
                    "blockers": [],
                },
            }
            requested_panels = panel or list(panel_data.keys())
            return {
                "view": view,
                "panels": {
                    key: {
                        "data": panel_data.get(key),
                        "error": None,
                    }
                    for key in requested_panels
                },
                "auth": {
                    "auth_enabled": True,
                    "authenticated": True,
                    "request_scheme": "https",
                    "secure_cookie_required": True,
                    "transport_compatible": True,
                    "required_transport": "https",
                    "auth_blocked_reason": None,
                    "protected_panel_keys": requested_panels,
                    "blocked_panel_keys": [],
                    "primary_error": None,
                    "access_state": "granted",
                },
                "timing": {
                    "total_ms": 1.0,
                    "panels": {},
                    "cache_hit": False,
                    "cache_age_ms": 0.0,
                    "deduped": False,
                },
            }

    return app


@contextmanager
def _live_server(app: FastAPI, *, scheme: str = "http", certfile: Path | None = None, keyfile: Path | None = None):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        host, port = sock.getsockname()

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="error",
        ssl_certfile=str(certfile) if certfile else None,
        ssl_keyfile=str(keyfile) if keyfile else None,
    )
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # type: ignore[assignment]
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"{scheme}://{host}:{port}"
    deadline = time.time() + 20
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            response = requests.get(
                f"{base_url}/auth/providers",
                timeout=1,
                verify=False if scheme == "https" else True,
            )
            if response.status_code == 200:
                break
        except Exception as exc:  # pragma: no cover - environment dependent
            last_error = exc
        time.sleep(0.2)
    else:  # pragma: no cover - defensive
        server.should_exit = True
        thread.join(timeout=10)
        raise RuntimeError(f"failed to start auth test server: {last_error}")

    try:
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _write_self_signed_cert(tmp_path: Path) -> tuple[Path, Path]:
    certfile = tmp_path / "operator.crt"
    keyfile = tmp_path / "operator.key"
    certfile.write_text(_SELF_SIGNED_CERT_PEM, encoding="utf-8")
    keyfile.write_text(_SELF_SIGNED_KEY_PEM, encoding="utf-8")
    return certfile, keyfile


@pytest.mark.integration
def test_http_secure_session_login_page_explicitly_blocks_operator_login() -> None:
    try:
        driver = _build_browser()
    except WebDriverException as exc:  # pragma: no cover - env dependent
        pytest.skip(f"Edge WebDriver unavailable: {exc}")

    app = _build_app(secure_cookie_required=True)
    with patch("aats.api.auth_routes._query", return_value=SimpleNamespace(record_operator_login=lambda **_: None)):
        with _live_server(app, scheme="http") as base_url:
            try:
                driver.get(f"{base_url}/login")
                wait = WebDriverWait(driver, 15)
                wait.until(
                    lambda browser: "HTTPS" in browser.find_element(By.ID, "loginMessage").text
                )
                login_button = driver.find_element(By.ID, "loginButton")
                message = driver.find_element(By.ID, "loginMessage").text
                assert not login_button.is_enabled()
                assert "HTTPS" in message
            finally:
                driver.quit()


@pytest.mark.integration
def test_https_secure_session_login_persists_and_can_read_rdp_bundle(tmp_path: Path) -> None:
    try:
        driver = _build_browser(ignore_certificate_errors=True)
    except WebDriverException as exc:  # pragma: no cover - env dependent
        pytest.skip(f"Edge WebDriver unavailable: {exc}")

    certfile, keyfile = _write_self_signed_cert(tmp_path)
    app = _build_app(secure_cookie_required=True, include_bundle=True)
    with patch("aats.api.auth_routes._query", return_value=SimpleNamespace(record_operator_login=lambda **_: None)):
        with _live_server(app, scheme="https", certfile=certfile, keyfile=keyfile) as base_url:
            try:
                driver.get(f"{base_url}/login")
                wait = WebDriverWait(driver, 20)
                wait.until(EC.element_to_be_clickable((By.ID, "loginUsername")))
                wait.until(lambda browser: browser.find_element(By.ID, "loginButton").is_enabled())
                driver.find_element(By.ID, "loginUsername").send_keys("admin")
                driver.find_element(By.ID, "loginPassword").send_keys("admin-pass")
                driver.find_element(By.ID, "loginButton").click()
                wait.until(lambda browser: "/ui" in browser.current_url)
                driver.get(f"{base_url}/e2e/auth-dashboard")
                wait.until(lambda browser: "RDP 核心面板已就绪" in browser.find_element(By.ID, "status").text)
            finally:
                driver.quit()


@pytest.mark.integration
def test_https_secure_session_can_render_overview_without_console_errors(tmp_path: Path) -> None:
    try:
        driver = _build_browser(ignore_certificate_errors=True)
    except WebDriverException as exc:  # pragma: no cover - env dependent
        pytest.skip(f"Edge WebDriver unavailable: {exc}")

    certfile, keyfile = _write_self_signed_cert(tmp_path)
    app = _build_app(secure_cookie_required=True, include_bundle=True)
    with patch("aats.api.auth_routes._query", return_value=SimpleNamespace(record_operator_login=lambda **_: None)):
        with _live_server(app, scheme="https", certfile=certfile, keyfile=keyfile) as base_url:
            try:
                driver.get(f"{base_url}/login")
                wait = WebDriverWait(driver, 20)
                wait.until(EC.element_to_be_clickable((By.ID, "loginUsername")))
                wait.until(lambda browser: browser.find_element(By.ID, "loginButton").is_enabled())
                driver.find_element(By.ID, "loginUsername").send_keys("admin")
                driver.find_element(By.ID, "loginPassword").send_keys("admin-pass")
                driver.find_element(By.ID, "loginButton").click()
                wait.until(lambda browser: "/ui" in browser.current_url)
                driver.get(f"{base_url}/ui/overview")
                wait.until(lambda browser: "资产概览" in browser.page_source)
                severe_logs = [
                    entry for entry in driver.get_log("browser") if entry.get("level") == "SEVERE"
                ]
                assert not severe_logs, severe_logs
            finally:
                driver.quit()
