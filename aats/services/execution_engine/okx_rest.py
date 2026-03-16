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


class OKXRequestError(RuntimeError):
    def __init__(
        self,
        *,
        path: str,
        code: str | None = None,
        msg: str | None = None,
        row_code: str | None = None,
        row_message: str | None = None,
        status_code: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.path = path
        self.code = code
        self.msg = msg
        self.row_code = row_code
        self.row_message = row_message
        self.status_code = status_code
        self.payload = payload or {}
        detail_parts = [f"path={path}"]
        if status_code is not None:
            detail_parts.append(f"http_status={status_code}")
        if code is not None:
            detail_parts.append(f"code={code}")
        if msg:
            detail_parts.append(f"msg={msg}")
        if row_code is not None:
            detail_parts.append(f"sCode={row_code}")
        if row_message:
            detail_parts.append(f"sMsg={row_message}")
        super().__init__(f"OKX request failed {' '.join(detail_parts)}")


class OKXRESTClient:
    def __init__(self, *, settings: AATSSettings) -> None:
        self.settings = settings

    def _inst_type(self) -> str:
        return "SWAP" if self.settings.trading_product_type == "derivatives" else "SPOT"

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
        payload = self._parse_json_payload(response)
        if response.status_code >= 400:
            row_code, row_message = self._extract_row_error(payload)
            raise OKXRequestError(
                path=path,
                code=str(payload.get("code")) if payload else None,
                msg=str(payload.get("msg")) if payload and payload.get("msg") else response.reason_phrase,
                row_code=row_code,
                row_message=row_message,
                status_code=response.status_code,
                payload=payload,
            )
        if str(payload.get("code")) != "0":
            row_code, row_message = self._extract_row_error(payload)
            raise OKXRequestError(
                path=path,
                code=str(payload.get("code")) if payload.get("code") is not None else None,
                msg=str(payload.get("msg")) if payload.get("msg") else None,
                row_code=row_code,
                row_message=row_message,
                status_code=response.status_code,
                payload=payload,
            )
        return payload

    async def get_balance(self) -> dict[str, Any]:
        return await self.request(method="GET", path="/api/v5/account/balance", require_auth=True)

    async def get_positions(self) -> dict[str, Any]:
        return await self.request(
            method="GET",
            path="/api/v5/account/positions",
            params={"instType": self._inst_type()},
            require_auth=True,
        )

    async def get_open_orders(self, *, symbol: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"instType": self._inst_type()}
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
            params={"instType": self._inst_type()},
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
                "instType": self._inst_type(),
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

    @staticmethod
    def _parse_json_payload(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _extract_row_error(payload: Mapping[str, Any] | None) -> tuple[str | None, str | None]:
        if not payload:
            return None, None
        rows = payload.get("data", [])
        if not isinstance(rows, list) or not rows:
            return None, None
        first = rows[0]
        if not isinstance(first, Mapping):
            return None, None
        row_code = first.get("sCode")
        row_message = first.get("sMsg")
        return (
            str(row_code) if row_code not in {None, ""} else None,
            str(row_message) if row_message not in {None, ""} else None,
        )
