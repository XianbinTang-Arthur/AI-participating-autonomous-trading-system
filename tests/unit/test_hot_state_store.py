"""Stage 6 单元测试：跨进程热状态存储抽象。

只覆盖：
1. make_key 命名空间组合
2. InMemoryHotStateStore 完整 CRUD + TTL 行为
3. RedisHotStateStore 防御性边界（未连接前不能 I/O）
4. build_hot_state_store 工厂

不测的部分（需要真实 Redis 实例，留给 Stage 6 集成测试）：
- Redis 实际 set/get 跨进程一致性
- TTL 在 Redis 端的精确度
- 多 client 并发写入
"""
from __future__ import annotations

import asyncio

import pytest

from aats.storage.hot_state_store import (
    HOT_STATE_KEY_PREFIX,
    NS_ACCOUNT,
    NS_MARKET,
    NS_SYSTEM,
    InMemoryHotStateStore,
    RedisHotStateConfig,
    RedisHotStateStore,
    build_hot_state_store,
    make_key,
    serialize_for_hot_state,
)


# ─────────────────────────────────────────────────────────────────────
# make_key
# ─────────────────────────────────────────────────────────────────────


def test_make_key_uses_global_prefix() -> None:
    key = make_key(NS_MARKET, "BTC-USDT", "15m")
    assert key.startswith(HOT_STATE_KEY_PREFIX)
    assert "market" in key
    assert "BTC-USDT" in key
    assert "15m" in key


def test_make_key_distinct_namespaces() -> None:
    market_key = make_key(NS_MARKET, "BTC-USDT")
    account_key = make_key(NS_ACCOUNT, "BTC-USDT")
    system_key = make_key(NS_SYSTEM, "kill_switch")
    assert market_key != account_key
    assert market_key != system_key
    assert account_key != system_key


def test_make_key_rejects_empty_part() -> None:
    with pytest.raises(ValueError):
        make_key(NS_MARKET, "")


def test_make_key_rejects_none_part() -> None:
    with pytest.raises(ValueError):
        make_key(NS_MARKET, None)  # type: ignore[arg-type]


# ─────────────────────────────────────────────────────────────────────
# InMemoryHotStateStore CRUD
# ─────────────────────────────────────────────────────────────────────


def test_inmemory_set_and_get() -> None:
    async def run() -> None:
        store = InMemoryHotStateStore()
        await store.set("k", {"px": 1.5})
        assert await store.get("k") == {"px": 1.5}
    asyncio.run(run())


def test_inmemory_get_missing_returns_none() -> None:
    async def run() -> None:
        store = InMemoryHotStateStore()
        assert await store.get("missing") is None
    asyncio.run(run())


def test_inmemory_delete() -> None:
    async def run() -> None:
        store = InMemoryHotStateStore()
        await store.set("k", "v")
        await store.delete("k")
        assert await store.get("k") is None
        # delete 不存在的 key 不抛错
        await store.delete("k")
    asyncio.run(run())


def test_inmemory_exists() -> None:
    async def run() -> None:
        store = InMemoryHotStateStore()
        assert await store.exists("k") is False
        await store.set("k", 1)
        assert await store.exists("k") is True
    asyncio.run(run())


def test_inmemory_get_many_filters_missing_keys() -> None:
    async def run() -> None:
        store = InMemoryHotStateStore()
        await store.set("a", 1)
        await store.set("b", 2)
        result = await store.get_many(["a", "b", "c"])
        assert result == {"a": 1, "b": 2}
    asyncio.run(run())


def test_inmemory_health_check() -> None:
    async def run() -> None:
        store = InMemoryHotStateStore()
        assert await store.health_check() is True
    asyncio.run(run())


# ─────────────────────────────────────────────────────────────────────
# InMemoryHotStateStore TTL
# ─────────────────────────────────────────────────────────────────────


def test_inmemory_ttl_expires_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """模拟时钟前进，验证 TTL 到期后 get 返回 None。"""
    async def run() -> None:
        store = InMemoryHotStateStore()
        fake_now = 1000.0
        monkeypatch.setattr(
            "aats.storage.hot_state_store.time.monotonic", lambda: fake_now
        )
        await store.set("k", "v", ttl_seconds=10)
        assert await store.get("k") == "v"

        # 时钟前进 11 秒，TTL 到期
        new_now = fake_now + 11
        monkeypatch.setattr(
            "aats.storage.hot_state_store.time.monotonic", lambda: new_now
        )
        assert await store.get("k") is None
    asyncio.run(run())


def test_inmemory_expire_on_existing_key() -> None:
    async def run() -> None:
        store = InMemoryHotStateStore()
        await store.set("k", "v")
        ok = await store.expire("k", ttl_seconds=5)
        assert ok is True
    asyncio.run(run())


def test_inmemory_expire_on_missing_key_returns_false() -> None:
    async def run() -> None:
        store = InMemoryHotStateStore()
        ok = await store.expire("missing", ttl_seconds=5)
        assert ok is False
    asyncio.run(run())


def test_inmemory_close_clears_state() -> None:
    async def run() -> None:
        store = InMemoryHotStateStore()
        await store.set("k", "v")
        await store.close()
        assert await store.get("k") is None
    asyncio.run(run())


# ─────────────────────────────────────────────────────────────────────
# RedisHotStateStore 防御性边界
# ─────────────────────────────────────────────────────────────────────


def test_redis_construction_does_not_require_redis_py() -> None:
    """构造时不应触发 import redis；只有 connect() 才会。"""
    store = RedisHotStateStore(config=RedisHotStateConfig())
    assert store._client is None


def test_redis_get_before_connect_raises() -> None:
    async def run() -> None:
        store = RedisHotStateStore(config=RedisHotStateConfig())
        with pytest.raises(RuntimeError, match="connect"):
            await store.get("k")
    asyncio.run(run())


def test_redis_set_before_connect_raises() -> None:
    async def run() -> None:
        store = RedisHotStateStore(config=RedisHotStateConfig())
        with pytest.raises(RuntimeError, match="connect"):
            await store.set("k", "v")
    asyncio.run(run())


def test_redis_delete_before_connect_raises() -> None:
    async def run() -> None:
        store = RedisHotStateStore(config=RedisHotStateConfig())
        with pytest.raises(RuntimeError, match="connect"):
            await store.delete("k")
    asyncio.run(run())


def test_redis_close_when_never_connected_is_noop() -> None:
    async def run() -> None:
        store = RedisHotStateStore(config=RedisHotStateConfig())
        await store.close()  # 不应抛错
    asyncio.run(run())


# ─────────────────────────────────────────────────────────────────────
# RedisHotStateConfig
# ─────────────────────────────────────────────────────────────────────


def test_redis_config_apply_prefix_no_prefix() -> None:
    cfg = RedisHotStateConfig()
    assert cfg.apply_prefix("aats:hot:market:BTC-USDT") == "aats:hot:market:BTC-USDT"


def test_redis_config_apply_prefix_with_env_prefix() -> None:
    cfg = RedisHotStateConfig(global_prefix="dev:")
    assert cfg.apply_prefix("aats:hot:market:BTC-USDT") == "dev:aats:hot:market:BTC-USDT"


# ─────────────────────────────────────────────────────────────────────
# build_hot_state_store 工厂
# ─────────────────────────────────────────────────────────────────────


def test_build_default_returns_in_memory() -> None:
    store = build_hot_state_store()
    assert isinstance(store, InMemoryHotStateStore)


def test_build_memory_explicit() -> None:
    store = build_hot_state_store(backend="memory")
    assert isinstance(store, InMemoryHotStateStore)


def test_build_redis_returns_redis_store_without_connecting() -> None:
    store = build_hot_state_store(backend="redis")
    assert isinstance(store, RedisHotStateStore)
    # connect() 没被调用，所以 _client 仍是 None
    assert store._client is None  # type: ignore[attr-defined]


def test_build_unknown_backend_raises() -> None:
    with pytest.raises(ValueError, match="unknown hot_state_backend"):
        build_hot_state_store(backend="kafka")


# ─────────────────────────────────────────────────────────────────────
# serialize_for_hot_state
# ─────────────────────────────────────────────────────────────────────


def test_serialize_dict_passthrough() -> None:
    out = serialize_for_hot_state({"px": 1.5, "qty": 2})
    assert out == {"px": 1.5, "qty": 2}


def test_serialize_pydantic_model_uses_model_dump() -> None:
    from pydantic import BaseModel

    class Foo(BaseModel):
        x: int
        y: str

    out = serialize_for_hot_state(Foo(x=1, y="abc"))  # type: ignore[arg-type]
    assert out == {"x": 1, "y": "abc"}
