"""P1-D Phase 1A Stage 2 单元测试 — MicrostructureWSClient 订阅构造。

覆盖 §9 Day 2 的"WS client 订阅 6 channel 正确构造 subscribe payload"验收点:
  1. 单 symbol 默认订阅全部 6 个 channel,每 channel 一个 arg
  2. 多 symbol 时笛卡尔积展开 (N symbol × 6 channel)
  3. instId 与 channel 归属 _subscription_key 可正确归一化
  4. 空 symbol / channel 参数被拒绝
  5. 子类正确继承 OKXWebSocketConsumerBase (ack timeout / reconnect 基础设施)
"""
from __future__ import annotations

import unittest

from aats.bootstrap.settings import AATSSettings
from aats.data_platform.collectors.microstructure_ws_collector import (
    MicrostructureWSClient,
    _CONNECTION,
    _SUBSCRIBE_CHANNELS,
)
from aats.services.market_gateway.okx_websocket import (
    OKXWebSocketConsumerBase,
    _subscription_key,
)


def _ws_settings(**overrides: object) -> AATSSettings:
    defaults: dict[str, object] = {
        "okx_ws_read_timeout_seconds": 0.5,
        "okx_ws_market_data_timeout_seconds": 1.5,
        "okx_market_reconnect_delay_seconds": 0.1,
        "okx_market_reconnect_max_delay_seconds": 0.2,
        "okx_ws_open_timeout_seconds": 5.0,
        "okx_private_ws_idle_ping_interval_seconds": 0.5,
    }
    defaults.update(overrides)
    return AATSSettings.model_validate(defaults)


class TestMicrostructureWSClientConnectionSpecs(unittest.TestCase):
    """_connection_specs 必须返回 1 个 public connection,
    args = N_symbol × N_channel 的笛卡尔积。"""

    def test_single_symbol_all_6_channels_default(self) -> None:
        client = MicrostructureWSClient(settings=_ws_settings())
        specs = client._connection_specs()
        self.assertEqual(len(specs), 1, "should be exactly 1 WS connection")
        name, url, args = specs[0]
        self.assertEqual(name, _CONNECTION, "connection 名字必须与常量一致")
        # Default symbols = ('BTC-USDT-SWAP',), channels = 6
        self.assertEqual(len(args), 6)
        # 每个 arg 至少包含 channel + instId
        for a in args:
            self.assertIn("channel", a)
            self.assertIn("instId", a)
            self.assertEqual(a["instId"], "BTC-USDT-SWAP")
        got_channels = sorted(a["channel"] for a in args)
        self.assertEqual(got_channels, sorted(_SUBSCRIBE_CHANNELS))

    def test_multi_symbol_cartesian_product(self) -> None:
        client = MicrostructureWSClient(
            settings=_ws_settings(),
            symbols=("BTC-USDT-SWAP", "ETH-USDT-SWAP"),
        )
        _name, _url, args = client._connection_specs()[0]
        self.assertEqual(len(args), 2 * 6, "2 symbols × 6 channels = 12 args")
        symbols_seen = {a["instId"] for a in args}
        self.assertEqual(symbols_seen, {"BTC-USDT-SWAP", "ETH-USDT-SWAP"})

    def test_deduplicates_symbols(self) -> None:
        """重复 symbol 应被 dict.fromkeys 去重。"""
        client = MicrostructureWSClient(
            settings=_ws_settings(),
            symbols=("BTC-USDT-SWAP", "BTC-USDT-SWAP"),
        )
        _name, _url, args = client._connection_specs()[0]
        self.assertEqual(len(args), 6)

    def test_subscription_key_compatibility(self) -> None:
        """每个 arg 可被 OKX ACK timeout 追踪用的 _subscription_key
        正确归一化,避免基类静默丢失订阅确认。"""
        client = MicrostructureWSClient(settings=_ws_settings())
        _name, _url, args = client._connection_specs()[0]
        keys = {_subscription_key(a) for a in args}
        # 6 个 channel × 1 symbol
        self.assertEqual(len(keys), 6)
        for channel, instId in keys:
            self.assertIn(channel, _SUBSCRIBE_CHANNELS)
            self.assertEqual(instId, "BTC-USDT-SWAP")


class TestMicrostructureWSClientValidation(unittest.TestCase):
    def test_empty_symbols_rejected(self) -> None:
        with self.assertRaises(ValueError):
            MicrostructureWSClient(settings=_ws_settings(), symbols=())

    def test_empty_channels_rejected(self) -> None:
        with self.assertRaises(ValueError):
            MicrostructureWSClient(
                settings=_ws_settings(),
                channels=(),
            )

    def test_custom_channel_subset_honored(self) -> None:
        """允许只订阅部分 channel,方便 Phase 2 分片化扩展。"""
        client = MicrostructureWSClient(
            settings=_ws_settings(),
            channels=("trades", "mark-price"),
        )
        _name, _url, args = client._connection_specs()[0]
        self.assertEqual(len(args), 2)
        self.assertEqual(
            sorted(a["channel"] for a in args),
            ["mark-price", "trades"],
        )


class TestMicrostructureWSClientInheritance(unittest.TestCase):
    def test_is_consumer_subclass(self) -> None:
        client = MicrostructureWSClient(settings=_ws_settings())
        self.assertIsInstance(client, OKXWebSocketConsumerBase)

    def test_uses_public_ws_url_from_settings(self) -> None:
        settings = _ws_settings(okx_public_ws_url="wss://ws.test:443/ws/v5/public")
        client = MicrostructureWSClient(settings=settings)
        _name, url, _args = client._connection_specs()[0]
        self.assertEqual(url, "wss://ws.test:443/ws/v5/public")

    def test_distinct_logger_namespace(self) -> None:
        """命令线要求 collector 有独立 logger namespace,
        不污染 market 的 aats.okx_market_ws 命名。"""
        client = MicrostructureWSClient(settings=_ws_settings())
        self.assertEqual(client.logger.name, "aats.okx_microstructure_ws")

    def test_distinct_connection_name(self) -> None:
        """connection 名字 = 'microstructure',区别于 aats-market 的
        'public' / 'business',便于 Prometheus / log 标签分流。"""
        client = MicrostructureWSClient(settings=_ws_settings())
        status = client.connection_status(_CONNECTION)
        # register_connection 已预填,所以可以无 run_forever 访问
        self.assertIn("connected", status)


if __name__ == "__main__":
    unittest.main()
