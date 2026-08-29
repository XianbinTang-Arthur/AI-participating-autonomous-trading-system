"""B1 契约测试：跨进程 readiness barrier。

现行 protocol v2 在任何 NATS I/O 前取得 global-role PROVISIONING owner，
build 完整返回后 CAS READY，再等待同 generation peer READY。

覆盖语义：
- _announce_runtime_ready 取得 global-role、instance-fenced lease
- optional/in-memory 兼容调用可 no-op；四主进程 NATS/hybrid 严格调用失败关闭
- 独立 FS-016 测试覆盖 generation、原子 claim/refresh/delete、Redis failure 与 strict timeout
- 无 peer（monolith）路径立即返回
- hot_state_store=None 仅在非严格兼容调用中 no-op
- strict Redis 异常绝不 fallback；publisher 不得在 barrier 或 lease 失败后启动
"""
from __future__ import annotations

import asyncio
import logging
import unittest
from unittest.mock import AsyncMock, MagicMock

from aats.bootstrap.process_lifecycle import (
    _RUNTIME_READY_LEASE_PROTOCOL,
    _PEER_READINESS_MAP,
    _announce_runtime_ready,
    _ready_key,
    _wait_for_peer_roles_ready,
)


class _RecordingHotStateStore:
    """最小 HotStateStore 假实现：dict-backed，支持 set/get/get_many/delete。"""

    def __init__(self) -> None:
        self._store: dict[str, object] = {}
        self.set_calls: list[tuple[str, object, float | None]] = []

    async def get(self, key: str) -> object | None:
        return self._store.get(key)

    async def set(self, key: str, value: object, *, ttl_seconds: float | None = None) -> None:
        self._store[key] = value
        self.set_calls.append((key, value, ttl_seconds))

    async def set_if_absent(
        self,
        key: str,
        value: object,
        *,
        ttl_seconds: float | None = None,
    ) -> bool:
        if key in self._store:
            return False
        self._store[key] = value
        self.set_calls.append((key, value, ttl_seconds))
        return True

    async def compare_refresh(
        self,
        key: str,
        expected_value: object,
        *,
        ttl_seconds: float,
    ) -> bool:
        return self._store.get(key) == expected_value

    async def compare_delete(self, key: str, expected_value: object) -> bool:
        if self._store.get(key) != expected_value:
            return False
        self._store.pop(key, None)
        return True

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)

    async def get_many(self, keys: list[str]) -> dict[str, object]:
        return {k: self._store[k] for k in keys if k in self._store}


class TestAnnounceRuntimeReady(unittest.IsolatedAsyncioTestCase):
    async def test_writes_ready_key_with_ttl(self) -> None:
        store = _RecordingHotStateStore()
        logger = logging.getLogger("test.announce")
        await _announce_runtime_ready(
            role="decision",
            hot_state_store=store,
            logger=logger,
        )
        key = _ready_key("decision")
        self.assertIn(key, store._store)
        val = store._store[key]
        self.assertEqual(val["process_role"], "decision")
        self.assertIn("announced_ts", val)
        self.assertEqual(val["lease_protocol"], _RUNTIME_READY_LEASE_PROTOCOL)
        self.assertEqual(len(val["instance_id"]), 32)
        self.assertIn("pid", val)
        # TTL 必须设上（防僵尸 key）
        (_, _, ttl) = store.set_calls[0]
        self.assertIsNotNone(ttl)
        self.assertGreater(ttl, 0)

    async def test_none_store_is_noop(self) -> None:
        logger = logging.getLogger("test.announce.none")
        # 不抛，不写 Redis——InMemory / monolith 场景兼容
        await _announce_runtime_ready(
            role="market",
            hot_state_store=None,
            logger=logger,
        )

    async def test_redis_set_exception_does_not_raise(self) -> None:
        """Optional/in-memory 兼容调用的 Redis set 失败仍可 warning 返回。"""
        store = MagicMock()
        store.set_if_absent = AsyncMock(side_effect=RuntimeError("redis down"))
        logger = logging.getLogger("test.announce.fail")
        # 应该吞掉异常
        await _announce_runtime_ready(
            role="execution",
            hot_state_store=store,
            logger=logger,
        )


class TestWaitForPeerRolesReady(unittest.IsolatedAsyncioTestCase):
    async def test_empty_peers_returns_immediately(self) -> None:
        store = _RecordingHotStateStore()
        logger = logging.getLogger("test.wait.empty")
        # monolith peer 列表为空
        await _wait_for_peer_roles_ready(
            role="monolith",
            hot_state_store=store,
            logger=logger,
        )
        # （不应阻塞）

    async def test_none_store_returns_immediately(self) -> None:
        logger = logging.getLogger("test.wait.none")
        await _wait_for_peer_roles_ready(
            role="decision",
            hot_state_store=None,
            logger=logger,
        )

    async def test_returns_when_all_peers_ready(self) -> None:
        store = _RecordingHotStateStore()
        # 预先设置所有 peer ready
        for peer in ("market", "execution", "gateway"):
            await store.set(
                _ready_key(peer),
                {
                    "lease_protocol": _RUNTIME_READY_LEASE_PROTOCOL,
                    "process_role": peer,
                    "phase": "READY",
                    "generation": None,
                    "instance_id": "a" * 32,
                    "announced_ts": "now",
                    "pid": 1,
                },
            )
        logger = logging.getLogger("test.wait.all_ready")
        # decision 等 market/execution/gateway
        await _wait_for_peer_roles_ready(
            role="decision",
            hot_state_store=store,
            logger=logger,
            timeout_seconds=2.0,
            poll_interval=0.02,
        )

    async def test_timeout_falls_back_without_raising(self) -> None:
        """Optional/in-memory 兼容调用可在 peer timeout 后返回。"""
        store = _RecordingHotStateStore()
        logger = logging.getLogger("test.wait.timeout")
        # store 是空的，peer 永远没 ready
        await _wait_for_peer_roles_ready(
            role="decision",
            hot_state_store=store,
            logger=logger,
            peers=("market",),  # 注入单 peer 保持测试快
            timeout_seconds=0.15,
            poll_interval=0.02,
        )
        # 没抛异常即成功

    async def test_peer_ready_arrives_during_wait(self) -> None:
        """模拟 peer 在我们轮询期间写入 ready key，应该及时发现并返回。"""
        store = _RecordingHotStateStore()
        logger = logging.getLogger("test.wait.race")

        async def writer() -> None:
            await asyncio.sleep(0.1)
            await store.set(
                _ready_key("market"),
                {
                    "lease_protocol": _RUNTIME_READY_LEASE_PROTOCOL,
                    "process_role": "market",
                    "phase": "READY",
                    "generation": None,
                    "instance_id": "a" * 32,
                    "announced_ts": "now",
                    "pid": 42,
                },
            )

        writer_task = asyncio.create_task(writer())
        try:
            await _wait_for_peer_roles_ready(
                role="decision",
                hot_state_store=store,
                logger=logger,
                peers=("market",),
                timeout_seconds=2.0,
                poll_interval=0.02,
            )
        finally:
            await writer_task

    async def test_get_many_exception_falls_back(self) -> None:
        """Optional/in-memory 兼容调用的 Redis 轮询异常可返回。"""
        store = MagicMock()
        store.get_many = AsyncMock(side_effect=ConnectionError("redis unreachable"))
        logger = logging.getLogger("test.wait.redis_err")
        # 应该吞异常 warn log 后返回
        await _wait_for_peer_roles_ready(
            role="decision",
            hot_state_store=store,
            logger=logger,
            peers=("market",),
            timeout_seconds=0.5,
            poll_interval=0.02,
        )

    async def test_hung_get_many_obeys_strict_overall_timeout(self) -> None:
        """单次 Redis read 永久挂起也不能越过 peer barrier 总 deadline。"""

        store = MagicMock()

        async def _hang(_keys: list[str]) -> dict[str, object]:
            await asyncio.Event().wait()
            return {}

        store.get_many = AsyncMock(side_effect=_hang)
        logger = logging.getLogger("test.wait.redis_hung")
        with self.assertRaisesRegex(
            RuntimeError,
            r"runtime_ready_gate_timeout:decision:market",
        ):
            await asyncio.wait_for(
                _wait_for_peer_roles_ready(
                    role="decision",
                    hot_state_store=store,
                    logger=logger,
                    peers=("market",),
                    generation="test-generation",
                    timeout_seconds=0.05,
                    poll_interval=0.01,
                    required=True,
                ),
                timeout=0.25,
            )


class TestPeerReadinessMap(unittest.TestCase):
    """锚点：防止未来 peer 映射被误改（比如漏配 market 的 peer 导致
    market publisher 不等 consumer 就启动）。"""

    def test_four_main_roles_cross_depend(self) -> None:
        """4 主 role 都等其他 3 role ready。"""
        for role in ("market", "decision", "execution", "gateway"):
            peers = set(_PEER_READINESS_MAP[role])
            expected = {"market", "decision", "execution", "gateway"} - {role}
            self.assertEqual(
                peers, expected,
                f"role {role} peer 映射错误 (got {peers}, expected {expected})",
            )

    def test_monolith_has_no_peers(self) -> None:
        self.assertEqual(_PEER_READINESS_MAP["monolith"], ())


if __name__ == "__main__":
    unittest.main()
