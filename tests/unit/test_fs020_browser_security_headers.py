from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException, Response
from fastapi.testclient import TestClient

from aats.api.security_headers import (
    BROWSER_SECURITY_HEADERS,
    CONTENT_SECURITY_POLICY,
    DEFAULT_GATEWAY_ALLOWED_HOSTS,
    STRICT_TRANSPORT_SECURITY,
    GatewayBrowserSecurityMiddleware,
    normalized_host_header,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


def _minimal_app() -> FastAPI:
    app = FastAPI()

    @app.get("/html")
    async def html() -> Response:
        return Response(
            "<html></html>",
            media_type="text/html",
            headers={"Content-Security-Policy": "default-src *", "X-Frame-Options": "SAMEORIGIN"},
        )

    @app.get("/json")
    async def json_response() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/denied")
    async def denied() -> None:
        raise HTTPException(status_code=403, detail="denied")

    app.add_middleware(
        GatewayBrowserSecurityMiddleware,
        allowed_hosts=DEFAULT_GATEWAY_ALLOWED_HOSTS,
    )
    return app


def _assert_fixed_headers(response) -> None:
    for name, expected in BROWSER_SECURITY_HEADERS.items():
        assert response.headers[name] == expected


@pytest.mark.parametrize("path", ["/html", "/json", "/denied"])
def test_security_headers_cover_html_json_and_http_errors(path: str) -> None:
    with TestClient(_minimal_app(), base_url="http://127.0.0.1") as client:
        response = client.get(path)

    _assert_fixed_headers(response)
    assert "strict-transport-security" not in response.headers


def test_security_middleware_overwrites_weaker_route_headers() -> None:
    with TestClient(_minimal_app(), base_url="http://127.0.0.1") as client:
        response = client.get("/html")

    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert response.headers["x-frame-options"] == "DENY"


def test_hsts_is_emitted_only_for_actual_https_scope() -> None:
    app = _minimal_app()
    with TestClient(app, base_url="http://127.0.0.1") as http_client:
        http_response = http_client.get("/json")
    with TestClient(app, base_url="https://127.0.0.1") as https_client:
        https_response = https_client.get("/json")

    assert "strict-transport-security" not in http_response.headers
    assert https_response.headers["strict-transport-security"] == STRICT_TRANSPORT_SECURITY


@pytest.mark.parametrize(
    "host_header",
    [
        "127.0.0.1",
        "127.0.0.1:8001",
        "LOCALHOST.",
        "localhost:8001",
        "[::1]:8001",
        "[0:0:0:0:0:0:0:1]",
        "testserver",
    ],
)
def test_allowed_local_hosts_reach_downstream(host_header: str) -> None:
    with TestClient(_minimal_app()) as client:
        response = client.get("/json", headers={"Host": host_header})

    assert response.status_code == 200
    _assert_fixed_headers(response)


@pytest.mark.parametrize(
    "host_header",
    [
        "",
        "0.0.0.0",
        "[::]",
        "localhost.evil.example",
        "localhost..",
        "evil.example",
        "user@127.0.0.1",
        "127.0.0.1/path",
        "127.0.0.1:0",
        "127.0.0.1:65536",
        "[::1",
        "[localhost]",
        "[127.0.0.1]",
    ],
)
def test_untrusted_or_malformed_hosts_fail_closed_with_security_headers(host_header: str) -> None:
    with TestClient(_minimal_app()) as client:
        response = client.get("/json", headers={"Host": host_header})

    assert response.status_code == 400
    assert response.text == "Host 请求头不受信任。"
    _assert_fixed_headers(response)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("LOCALHOST.", "localhost"),
        ("127.0.0.1:8001", "127.0.0.1"),
        ("::1", "::1"),
        ("[::1]:8001", "::1"),
        ("bad host", None),
        (None, None),
    ],
)
def test_host_parser_contract(raw: str | None, expected: str | None) -> None:
    assert normalized_host_header(raw) == expected


def test_current_ui_requires_no_unsafe_inline_csp_exceptions() -> None:
    for filename in ("login.html", "dashboard-shell.html"):
        source = (REPO_ROOT / "aats" / "api" / "static" / filename).read_text(encoding="utf-8")
        assert re.search(r"<script(?![^>]*\bsrc=)", source, flags=re.IGNORECASE) is None
        assert re.search(r"<style\b", source, flags=re.IGNORECASE) is None
        assert re.search(r"\sstyle\s*=", source, flags=re.IGNORECASE) is None
        assert re.search(r"<[^>]+\son[a-z]+\s*=", source, flags=re.IGNORECASE) is None
        assert "javascript:" not in source.lower()

    assert "'unsafe-inline'" not in CONTENT_SECURITY_POLICY
    assert "'unsafe-eval'" not in CONTENT_SECURITY_POLICY


def test_gateway_main_registers_security_middleware() -> None:
    source = (REPO_ROOT / "apps" / "api_gateway" / "main.py").read_text(encoding="utf-8")

    assert "app.add_middleware(" in source
    assert "GatewayBrowserSecurityMiddleware" in source
