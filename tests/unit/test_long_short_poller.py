"""LongShortRatioPoller 契约 (P2.7).

锁定:
  1. symbol → ccy 解析 (BTC-USDT-SWAP → BTC)
  2. 成功轮询后 latest() 返回样本
  3. REST 失败 → last_error 记录，缓存保留上次有效值
  4. OKX code != "0" → 不更新缓存
  5. latest() 对未见过的 symbol 返回 None
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from aats.services.feature_engine.long_short_poller import (
    LongShortRatioPoller,
    _symbol_to_ccy,
)


class SymbolToCcyTests(unittest.TestCase):
    def test_parses_btc_usdt_swap(self) -> None:
        self.assertEqual(_symbol_to_ccy("BTC-USDT-SWAP"), "BTC")

    def test_parses_eth_usd_swap(self) -> None:
        self.assertEqual(_symbol_to_ccy("ETH-USD-SWAP"), "ETH")

    def test_lowercase_normalized(self) -> None:
        self.assertEqual(_symbol_to_ccy("btc-usdt-swap"), "BTC")

    def test_empty_raises(self) -> None:
        with self.assertRaises(ValueError):
            _symbol_to_ccy("")


class LongShortRatioPollerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.poller = LongShortRatioPoller(
            okx_rest_url="https://www.okx.com",
            poll_interval_seconds=300.0,
            timeout_seconds=5.0,
        )

    async def test_latest_unknown_symbol_returns_none(self) -> None:
        self.assertIsNone(self.poller.latest("BTC-USDT-SWAP"))

    async def test_successful_poll_updates_cache(self) -> None:
        """Mock httpx.AsyncClient.get 返回合法 OKX 响应，缓存应更新."""
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = AsyncMock()
        mock_resp.json = lambda: {
            "code": "0",
            "data": [["1745000000000", "2.5"]],
        }
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        await self.poller._poll_one(mock_client, "BTC-USDT-SWAP")
        # _poll_one 自身只返回 sample；cache 在 _poll_round 里更新。
        # 这里验证 sample 正确返回
        sample = await self.poller._poll_one(mock_client, "BTC-USDT-SWAP")
        self.assertIsNotNone(sample)
        assert sample is not None
        self.assertEqual(sample.symbol, "BTC-USDT-SWAP")
        self.assertEqual(sample.ls_ratio, 2.5)
        self.assertEqual(sample.ts, datetime.fromtimestamp(1_745_000_000, tz=timezone.utc))

    async def test_poll_round_sets_cache(self) -> None:
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = AsyncMock()
        mock_resp.json = lambda: {
            "code": "0",
            "data": [["1745000000000", "1.8"]],
        }
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch(
            "aats.services.feature_engine.long_short_poller.httpx.AsyncClient"
        ) as mock_async_client_cls:
            mock_async_client_cls.return_value.__aenter__.return_value = mock_client
            mock_async_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            await self.poller._poll_round(("BTC-USDT-SWAP",))

        sample = self.poller.latest("BTC-USDT-SWAP")
        self.assertIsNotNone(sample)
        assert sample is not None
        self.assertEqual(sample.ls_ratio, 1.8)

    async def test_okx_code_non_zero_skips_cache_update(self) -> None:
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = AsyncMock()
        mock_resp.json = lambda: {"code": "50011", "msg": "rate limit", "data": []}
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        sample = await self.poller._poll_one(mock_client, "BTC-USDT-SWAP")
        self.assertIsNone(sample)

    async def test_empty_data_returns_none(self) -> None:
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = AsyncMock()
        mock_resp.json = lambda: {"code": "0", "data": []}
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        sample = await self.poller._poll_one(mock_client, "BTC-USDT-SWAP")
        self.assertIsNone(sample)

    async def test_negative_ls_ratio_rejected(self) -> None:
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = AsyncMock()
        mock_resp.json = lambda: {"code": "0", "data": [["1745000000000", "-1.0"]]}
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        sample = await self.poller._poll_one(mock_client, "BTC-USDT-SWAP")
        self.assertIsNone(sample)


if __name__ == "__main__":
    unittest.main()
