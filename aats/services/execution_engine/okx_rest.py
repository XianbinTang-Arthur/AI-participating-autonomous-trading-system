from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Mapping
from typing import Any

import httpx
import orjson

from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now


class OKXRESTClient:
    def __init__(self, *, settings: AATSSettings) -> None:
        self.settings = settings

    async def request(
        self,
        *,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        require_auth: bool = False,
    ) -> dict[str, Any]:
        request_path = self._request_path(path=path, params=params)
        headers = {"Content-Type": "application/json"}
        body_text = orjson.dumps(json_body).decode("utf-8") if json_body is not None else ""
        if require_auth:
            headers.update(self._auth_headers(method=method, request_path=request_path, body=body_text))
        if self.settings.okx_simulated_trading:
            headers["x-simulated-trading"] = "1"

        async with httpx.AsyncClient(
            base_url=self.settings.okx_rest_url,
            timeout=self.settings.okx_timeout_seconds,
        ) as client:
            response = await client.request(
                method=method.upper(),
                url=request_path,
                headers=headers,
                content=body_text if body_text else None,
            )
        response.raise_for_status()
        payload = response.json()
        if str(payload.get("code")) != "0":
            raise RuntimeError(f"OKX request failed path={path} code={payload.get('code')} msg={payload.get('msg')}")
        return payload

    async def get_balance(self) -> dict[str, Any]:
        return await self.request(method="GET", path="/api/v5/account/balance", require_auth=True)

    async def get_positions(self) -> dict[str, Any]:
        return await self.request(
            method="GET",
            path="/api/v5/account/positions",
            params={"instType": "SPOT"},
            require_auth=True,
        )

    async def get_open_orders(self, *, symbol: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"instType": "SPOT"}
        if symbol is not None:
            params["instId"] = symbol
        return await self.request(
            method="GET",
            path="/api/v5/trade/orders-pending",
            params=params,
            require_auth=True,
        )

    async def get_instruments(self) -> dict[str, Any]:
        return await self.request(
            method="GET",
            path="/api/v5/account/instruments",
            params={"instType": "SPOT"},
            require_auth=True,
        )

    async def get_account_config(self) -> dict[str, Any]:
        return await self.request(
            method="GET",
            path="/api/v5/account/config",
            require_auth=True,
        )

    async def place_order(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return await self.request(
            method="POST",
            path="/api/v5/trade/order",
            json_body=payload,
            require_auth=True,
        )

    async def cancel_order(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return await self.request(
            method="POST",
            path="/api/v5/trade/cancel-order",
            json_body=payload,
            require_auth=True,
        )

    async def get_order(
        self,
        *,
        symbol: str,
        order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        if order_id is None and client_order_id is None:
            raise ValueError("order_id or client_order_id must be provided")
        return await self.request(
            method="GET",
            path="/api/v5/trade/order",
            params={
                "instId": symbol,
                "ordId": order_id,
                "clOrdId": client_order_id,
            },
            require_auth=True,
        )

    async def get_fills(
        self,
        *,
        symbol: str | None = None,
        order_id: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        return await self.request(
            method="GET",
            path="/api/v5/trade/fills",
            params={
                "instType": "SPOT",
                "instId": symbol,
                "ordId": order_id,
                "limit": limit,
            },
            require_auth=True,
        )

    def _auth_headers(self, *, method: str, request_path: str, body: str) -> dict[str, str]:
        if not self.settings.okx_credentials_configured:
            raise RuntimeError("OKX credentials are not configured")
        timestamp = utc_now().isoformat(timespec="milliseconds").replace("+00:00", "Z")
        prehash = f"{timestamp}{method.upper()}{request_path}{body}"
        digest = hmac.new(
            (self.settings.okx_api_secret or "").encode("utf-8"),
            prehash.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        signature = base64.b64encode(digest).decode("utf-8")
        return {
            "OK-ACCESS-KEY": str(self.settings.okx_api_key),
            "OK-ACCESS-SIGN": signature,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": str(self.settings.okx_api_passphrase),
        }

    @staticmethod
    def _request_path(path: str, params: Mapping[str, Any] | None) -> str:
        if not params:
            return path
        encoded = httpx.QueryParams({key: value for key, value in params.items() if value is not None})
        query = str(encoded)
        return f"{path}?{query}" if query else path
