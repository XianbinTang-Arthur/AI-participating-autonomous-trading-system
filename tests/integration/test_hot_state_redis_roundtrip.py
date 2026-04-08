"""Stage 6 Slice 6.1 集成测试：RedisHotStateStore 真容器 round-trip。

覆盖：

1. **connect → set → get → close** 完整 happy path
   - 用 testcontainers 起 redis:7-alpine
   - 构造 RedisHotStateStore，connect 后 set/get/delete/exists/expire/get_many
   - 验证 JSON 编码 + 全局 prefix 行为

2. **跨实例可见性**：build_hot_state_store factory 走 settings 链路
   - 模拟两个独立 process 各自构造一个 store 对象，连同一台 Redis
   - 一边 set，另一边 get，验证 KV 真的跨进程共享（这是 Stage 6 的核心承诺）

3. **错误路径**：connect 之前就 set 应当抛 RuntimeError
   - 提前用错路径让 build_runtime fail-fast 校验更可靠

⚠️ **运行条件**：
- 需要 docker（Windows 上请确保 Docker Desktop 正在运行）
- 需要安装可选依赖：``pip install -e .[redis-integration]``
- 需要设置环境变量 ``AATS_RUN_REDIS_INTEGRATION=1`` 才会真正运行
- 默认情况下整个文件被 ``unittest.skipUnless`` 跳过，不会拖慢 CI / 单测

⚠️ **scope 注意**：本文件验证的是 RedisHotStateStore 在最底层的正确性。
build_runtime 真正切到 Redis backend 的端到端验证是 Slice 6.1 #4.6 的
"4 进程 docker compose 真跑"，由 runbook 章节记录。本文件用 testcontainers
起的 ad-hoc Redis 是"可移植的最小信任根"。
"""
from __future__ import annotations

import asyncio
import os
import unittest

# 软依赖检查：testcontainers + redis-py 可能没装（属于 redis-integration extra）
try:
    from testcontainers.core.container import DockerContainer  # type: ignore[import-not-found]
    from testcontainers.core.waiting_utils import wait_for_logs  # type: ignore[import-not-found]

    _TESTCONTAINERS_AVAILABLE = True
except ImportError:  # pragma: no cover - 没装就跳过
    DockerContainer = None  # type: ignore[assignment,misc]
    wait_for_logs = None  # type: ignore[assignment]
    _TESTCONTAINERS_AVAILABLE = False

try:
    import redis  # type: ignore[import-not-found]  # noqa: F401

    _REDIS_PY_AVAILABLE = True
except ImportError:  # pragma: no cover - 没装就跳过
    _REDIS_PY_AVAILABLE = False


_INTEGRATION_ENV_FLAG = "AATS_RUN_REDIS_INTEGRATION"
_SHOULD_RUN = (
    os.getenv(_INTEGRATION_ENV_FLAG) == "1"
    and _TESTCONTAINERS_AVAILABLE
    and _REDIS_PY_AVAILABLE
)


def _start_redis_container() -> "DockerContainer":
    """启动一个 redis:7-alpine 容器。

    与生产部署目标版本（Redis 7.x）一致。本文件不需要 RDB / AOF 持久化，
    默认 in-memory 模式即可。
    """
    container = (
        DockerContainer("redis:7-alpine")
        .with_exposed_ports(6379)
        .with_command("redis-server --save '' --appendonly no")
    )
    container.start()
    # 等到 server 真的接受连接，避免 connect 超时
    wait_for_logs(container, "Ready to accept connections", timeout=30)
    return container


def _client_url(container: "DockerContainer") -> str:
    host = container.get_container_host_ip()
    port = container.get_exposed_port(6379)
    return f"redis://{host}:{port}/0"


@unittest.skipUnless(
    _SHOULD_RUN,
    f"Set {_INTEGRATION_ENV_FLAG}=1 and install .[redis-integration] to run Redis integration tests",
)
class TestRedisHotStateStoreRoundTrip(unittest.IsolatedAsyncioTestCase):
    """RedisHotStateStore connect → set → get → close 端到端 round-trip。"""

    container: "DockerContainer"  # type: ignore[assignment]
    redis_url: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.container = _start_redis_container()
        cls.redis_url = _client_url(cls.container)

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.container.stop()
        except Exception:
            pass

    async def asyncTearDown(self) -> None:
        # 测试间隔离：用一个 throwaway client FLUSHDB 清空 db0
        from redis.asyncio import Redis as AsyncRedis  # type: ignore[import-not-found]

        client = AsyncRedis.from_url(self.redis_url)
        try:
            await client.flushdb()
        finally:
            await client.aclose()

    async def test_connect_set_get_round_trip(self) -> None:
        """connect → set → get 必须返回完全等价的 dict。"""
        from aats.storage.hot_state_store import (
            RedisHotStateConfig,
            RedisHotStateStore,
            make_key,
        )

        store = RedisHotStateStore(
            config=RedisHotStateConfig(url=self.redis_url),
        )
        try:
            await store.connect()
            self.assertTrue(await store.health_check())

            key = make_key("system", "kill_switch")
            payload = {
                "halted": True,
                "reason": "trial_guard_threshold_breached",
                "since_ts": 1712489600.123,
            }
            await store.set(key, payload)
            got = await store.get(key)
            self.assertEqual(got, payload)
        finally:
            await store.close()

    async def test_set_with_ttl_expires(self) -> None:
        """set 时传 ttl_seconds 必须真的让 key 在 Redis 上过期。"""
        from aats.storage.hot_state_store import (
            RedisHotStateConfig,
            RedisHotStateStore,
            make_key,
        )

        store = RedisHotStateStore(
            config=RedisHotStateConfig(url=self.redis_url),
        )
        try:
            await store.connect()
            key = make_key("gw_hb", "gateway")
            await store.set(key, {"ts": 1.0}, ttl_seconds=0.5)
            self.assertTrue(await store.exists(key))
            await asyncio.sleep(0.7)
            self.assertFalse(await store.exists(key))
            self.assertIsNone(await store.get(key))
        finally:
            await store.close()

    async def test_delete_removes_key(self) -> None:
        from aats.storage.hot_state_store import (
            RedisHotStateConfig,
            RedisHotStateStore,
            make_key,
        )

        store = RedisHotStateStore(
            config=RedisHotStateConfig(url=self.redis_url),
        )
        try:
            await store.connect()
            key = make_key("market", "BTC-USDT", "15m")
            await store.set(key, {"px": 65000})
            self.assertTrue(await store.exists(key))
            await store.delete(key)
            self.assertFalse(await store.exists(key))
        finally:
            await store.close()

    async def test_get_many_returns_only_present_keys(self) -> None:
        from aats.storage.hot_state_store import (
            RedisHotStateConfig,
            RedisHotStateStore,
            make_key,
        )

        store = RedisHotStateStore(
            config=RedisHotStateConfig(url=self.redis_url),
        )
        try:
            await store.connect()
            k1 = make_key("market", "BTC-USDT")
            k2 = make_key("market", "ETH-USDT")
            k3 = make_key("market", "MISSING")
            await store.set(k1, {"px": 65000})
            await store.set(k2, {"px": 3500})
            got = await store.get_many([k1, k2, k3])
            self.assertEqual(got, {k1: {"px": 65000}, k2: {"px": 3500}})
        finally:
            await store.close()

    async def test_global_prefix_isolates_namespaces(self) -> None:
        """同一台 Redis 上 dev / prod 两个 prefix 的 store 互不可见。"""
        from aats.storage.hot_state_store import (
            RedisHotStateConfig,
            RedisHotStateStore,
            make_key,
        )

        dev_store = RedisHotStateStore(
            config=RedisHotStateConfig(url=self.redis_url, global_prefix="dev:"),
        )
        prod_store = RedisHotStateStore(
            config=RedisHotStateConfig(url=self.redis_url, global_prefix="prod:"),
        )
        try:
            await dev_store.connect()
            await prod_store.connect()

            shared_key = make_key("system", "kill_switch")
            await dev_store.set(shared_key, {"halted": True, "env": "dev"})
            await prod_store.set(shared_key, {"halted": False, "env": "prod"})

            dev_view = await dev_store.get(shared_key)
            prod_view = await prod_store.get(shared_key)
            self.assertEqual(dev_view, {"halted": True, "env": "dev"})
            self.assertEqual(prod_view, {"halted": False, "env": "prod"})
        finally:
            await dev_store.close()
            await prod_store.close()

    async def test_two_stores_share_state_across_logical_processes(self) -> None:
        """同一台 Redis 上两个独立 store 实例必须能互看到对方写入。

        这是 Stage 6 的核心承诺：当 4 进程拓扑里的 gateway 与 execution
        各自构造自己的 RedisHotStateStore 时，gateway.set("kill_switch")
        必须能被 execution.get("kill_switch") 看到。
        """
        from aats.storage.hot_state_store import (
            RedisHotStateConfig,
            RedisHotStateStore,
            make_key,
        )

        gateway_store = RedisHotStateStore(
            config=RedisHotStateConfig(url=self.redis_url),
        )
        execution_store = RedisHotStateStore(
            config=RedisHotStateConfig(url=self.redis_url),
        )
        try:
            await gateway_store.connect()
            await execution_store.connect()

            key = make_key("system", "kill_switch")
            await execution_store.set(
                key,
                {
                    "halted": True,
                    "reason": "operator_manual_pause",
                    "since_ts": 1712489700.0,
                },
            )

            seen_by_gateway = await gateway_store.get(key)
            self.assertEqual(
                seen_by_gateway,
                {
                    "halted": True,
                    "reason": "operator_manual_pause",
                    "since_ts": 1712489700.0,
                },
            )
        finally:
            await gateway_store.close()
            await execution_store.close()

    async def test_set_before_connect_raises(self) -> None:
        """没 connect 就 set 必须抛 RuntimeError——避免 build_runtime
        漏 connect 时延迟到第一次写才崩。"""
        from aats.storage.hot_state_store import (
            RedisHotStateConfig,
            RedisHotStateStore,
            make_key,
        )

        store = RedisHotStateStore(
            config=RedisHotStateConfig(url=self.redis_url),
        )
        try:
            with self.assertRaises(RuntimeError):
                await store.set(make_key("system", "x"), {"v": 1})
        finally:
            await store.close()


@unittest.skipUnless(
    _SHOULD_RUN,
    f"Set {_INTEGRATION_ENV_FLAG}=1 and install .[redis-integration] to run Redis integration tests",
)
class TestBuildRuntimeWithRedisHotStateBackend(unittest.IsolatedAsyncioTestCase):
    """Slice 6.1：build_runtime 走 redis backend 时端到端能起来 + 关掉。

    这是 #4.3 配线最关键的端到端断言：把 AATSSettings 的 hot_state_backend
    设成 redis、URL 指向 testcontainer，build_runtime 必须正常返回一个
    runtime，hot_state_store 字段必须是已 connect 的 RedisHotStateStore，
    monolith 流转端到端可用。
    """

    container: "DockerContainer"  # type: ignore[assignment]
    redis_url: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.container = _start_redis_container()
        cls.redis_url = _client_url(cls.container)

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.container.stop()
        except Exception:
            pass

    async def asyncTearDown(self) -> None:
        from redis.asyncio import Redis as AsyncRedis  # type: ignore[import-not-found]

        client = AsyncRedis.from_url(self.redis_url)
        try:
            await client.flushdb()
        finally:
            await client.aclose()

    async def test_build_runtime_redis_backend_round_trip(self) -> None:
        from aats.bootstrap.config import build_runtime
        from aats.bootstrap.settings import AATSSettings
        from aats.storage.hot_state_store import RedisHotStateStore, make_key

        settings = AATSSettings.model_validate(
            {
                "hot_state_backend": "redis",
                "hot_state_redis_url": self.redis_url,
            }
        )
        runtime = await build_runtime(settings)
        try:
            self.assertIsInstance(runtime.hot_state_store, RedisHotStateStore)
            self.assertTrue(await runtime.hot_state_store.health_check())

            key = make_key("system", "build_runtime_smoke")
            await runtime.hot_state_store.set(key, {"ok": True})
            got = await runtime.hot_state_store.get(key)
            self.assertEqual(got, {"ok": True})
        finally:
            await runtime.stop_background_tasks()

    async def test_build_runtime_redis_backend_unreachable_fails_fast(self) -> None:
        """指向不可达 Redis 时 build_runtime 必须抛错而不是悄悄半起来。"""
        from aats.bootstrap.config import build_runtime
        from aats.bootstrap.settings import AATSSettings

        # 使用 RFC5737 文档保留地址 + 非常短的超时（默认 3s 也够），
        # 任何机器都连不上，避免误连本机服务。
        unreachable_url = "redis://192.0.2.1:6379/0"
        settings = AATSSettings.model_validate(
            {
                "hot_state_backend": "redis",
                "hot_state_redis_url": unreachable_url,
            }
        )
        with self.assertRaises(Exception):
            await build_runtime(settings)


if __name__ == "__main__":
    unittest.main()
