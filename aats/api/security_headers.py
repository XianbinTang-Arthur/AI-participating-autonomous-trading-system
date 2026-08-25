from __future__ import annotations

from collections.abc import Sequence
from ipaddress import AddressValueError, IPv6Address

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


DEFAULT_GATEWAY_ALLOWED_HOSTS: tuple[str, ...] = (
    "127.0.0.1",
    "localhost",
    "::1",
    "testserver",
)

CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'none'",
        "base-uri 'none'",
        "connect-src 'self'",
        "font-src 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "img-src 'self' data:",
        "manifest-src 'self'",
        "object-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "worker-src 'self'",
    )
)

BROWSER_SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=(), serial=()"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}

STRICT_TRANSPORT_SECURITY = "max-age=31536000"
_INVALID_HOST_CHARS = frozenset("/@?#\\")


def _valid_port_suffix(value: str) -> bool:
    if not value or not value.isdigit():
        return False
    port = int(value)
    return 1 <= port <= 65535


def normalized_host_header(value: str | None) -> str | None:
    if value is None or value != value.strip():
        return None
    raw = value.lower()
    if not raw or any(char in raw for char in _INVALID_HOST_CHARS):
        return None

    # ``allowed_hosts`` stores the IPv6 loopback address without URI brackets,
    # while an HTTP Host header normally carries it as ``[::1]``. Normalize
    # both representations to the same exact loopback value.
    if raw == "::1":
        return raw

    if raw.startswith("["):
        closing = raw.find("]")
        if closing <= 1:
            return None
        host = raw[1:closing]
        try:
            host = IPv6Address(host).compressed
        except AddressValueError:
            return None
        suffix = raw[closing + 1 :]
        if suffix and (not suffix.startswith(":") or not _valid_port_suffix(suffix[1:])):
            return None
        if "]" in suffix:
            return None
        return host

    if raw.count(":") > 1:
        return None
    if ":" in raw:
        host, port = raw.rsplit(":", maxsplit=1)
        if not _valid_port_suffix(port):
            return None
    else:
        host = raw
    if host.endswith(".."):
        return None
    host = host.rstrip(".")
    if not host or any(char.isspace() for char in host):
        return None
    return host


def _apply_browser_security_headers(message: Message, *, is_https: bool) -> None:
    headers = MutableHeaders(scope=message)
    for name, value in BROWSER_SECURITY_HEADERS.items():
        headers[name] = value
    if is_https:
        headers["Strict-Transport-Security"] = STRICT_TRANSPORT_SECURITY
    elif "strict-transport-security" in headers:
        del headers["strict-transport-security"]


class GatewayBrowserSecurityMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        allowed_hosts: Sequence[str] = DEFAULT_GATEWAY_ALLOWED_HOSTS,
    ) -> None:
        normalized_hosts = frozenset(normalized_host_header(host) for host in allowed_hosts)
        if None in normalized_hosts or not normalized_hosts:
            raise ValueError("invalid_gateway_allowed_hosts")
        self.app = app
        self.allowed_hosts = normalized_hosts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        is_https = scope.get("scheme") == "https"

        async def send_with_security_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                _apply_browser_security_headers(message, is_https=is_https)
            await send(message)

        host = normalized_host_header(Headers(scope=scope).get("host"))
        if host not in self.allowed_hosts:
            response = PlainTextResponse("Host 请求头不受信任。", status_code=400)
            await response(scope, receive, send_with_security_headers)
            return

        await self.app(scope, receive, send_with_security_headers)
