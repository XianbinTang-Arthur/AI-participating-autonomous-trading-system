"""Stage 6 代码准备：跨进程热状态存储抽象。

监督原则
========
"热状态" 指的是高频读写、不需要长期持久化的运行时缓存：
- 最新行情快照（latest_market_snapshot[symbol]）
- 最新账户余额（latest_account_state[account_id]）
- KillSwitch 当前状态
- 网关健康心跳（gateway_heartbeat[role]）

monolith 模式下这些都是 dict in-process。多进程拆分后必须有共享存储，
否则 decision_proc 看不到 gateway_proc 收到的最新行情。Redis 是默认选项：
单实例足够（毫秒级延迟），有 TTL 和 atomic ops，运维简单。

本模块只提供：
1. HotStateStore 抽象接口（async）
2. InMemoryHotStateStore — 进程内 dict 实现，monolith 复用
3. RedisHotStateStore — Redis 后端实现，nats-py / redis-py 都是可选依赖
4. 命名空间约定 helper：分离 market / account / system 等键空间，
   防止多进程互相覆盖

⚠️ 本模块不会被 build_runtime 自动启用；它是 Stage 6 切片化迁移的基座。
集成会在 docker-compose 起 Redis、并把现有 in-process dict 替换为 store
调用之后才发生。
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查
    from redis.asyncio import Redis as AsyncRedis  # type: ignore[import-not-found]


# ─────────────────────────────────────────────────────────────────────
# 命名空间约定
# ─────────────────────────────────────────────────────────────────────

# 顶层 prefix，避免和 NATS subject / Postgres key 冲突
HOT_STATE_KEY_PREFIX = "aats:hot:"

# 二级命名空间
NS_MARKET = "market"  # latest_market_snapshot
NS_ACCOUNT = "account"  # latest_account_state
NS_SYSTEM = "system"  # kill_switch / health
NS_GATEWAY_HEARTBEAT = "gw_hb"  # gateway 心跳，TTL 短


def make_key(namespace: str, *parts: str) -> str:
    """生成全局唯一 hot state key。

    例：make_key('market', 'BTC-USDT', '15m') -> 'aats:hot:market:BTC-USDT:15m'
    """
    safe_parts = []
    for part in parts:
        if part is None:
            raise ValueError("hot_state key parts cannot be None")
        text = str(part)
        if not text:
            raise ValueError("hot_state key parts cannot be empty")
        safe_parts.append(text)
    return f"{HOT_STATE_KEY_PREFIX}{namespace}:" + ":".join(safe_parts)


# ─────────────────────────────────────────────────────────────────────
# 抽象接口
# ─────────────────────────────────────────────────────────────────────


@runtime_checkable
class HotStateStore(Protocol):
    """跨进程共享的热状态 KV 存储。

    所有方法都是 async（即使内存实现也用 async def），以便 decision/execution
    模块统一以 await 调用，未来切到 Redis 不需要改 caller。
    """

    async def get(self, key: str) -> Any | None:
        """读取 key。不存在返回 None。"""
        ...

    async def set(self, key: str, value: Any, *, ttl_seconds: float | None = None) -> None:
        """写入 key。ttl_seconds 为 None 表示不过期。"""
        ...

    async def delete(self, key: str) -> None:
        """删除 key。不存在视为成功。"""
        ...

    async def expire(self, key: str, ttl_seconds: float) -> bool:
        """对已存在的 key 设置 TTL。返回 True 表示设置成功。"""
        ...

    async def exists(self, key: str) -> bool: ...

    async def get_many(self, keys: list[str]) -> dict[str, Any]:
        """批量读取，返回存在的 key→value 映射。"""
        ...

    async def health_check(self) -> bool:
        """ping 后端。失败抛异常或返回 False。"""
        ...

    async def close(self) -> None:
        """优雅关闭。"""
        ...


# ─────────────────────────────────────────────────────────────────────
# 内存实现（monolith 默认）
# ─────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class _InMemoryEntry:
    value: Any
    expires_at: float | None  # monotonic seconds


class InMemoryHotStateStore(HotStateStore):
    """单进程 dict 实现。multi-process 模式下不应使用。

    保留 TTL 语义以便在 monolith / 单元测试下仍能验证 expire 行为。
    """

    def __init__(self) -> None:
        self._data: dict[str, _InMemoryEntry] = {}
        self._lock = asyncio.Lock()

    def _is_expired(self, entry: _InMemoryEntry, now: float) -> bool:
        return entry.expires_at is not None and entry.expires_at <= now

    async def get(self, key: str) -> Any | None:
        async with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            if self._is_expired(entry, time.monotonic()):
                self._data.pop(key, None)
                return None
            return entry.value

    async def set(
        self,
        key: str,
        value: Any,
        *,
        ttl_seconds: float | None = None,
    ) -> None:
        expires_at = time.monotonic() + ttl_seconds if ttl_seconds is not None else None
        async with self._lock:
            self._data[key] = _InMemoryEntry(value=value, expires_at=expires_at)

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._data.pop(key, None)

    async def expire(self, key: str, ttl_seconds: float) -> bool:
        async with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return False
            entry.expires_at = time.monotonic() + ttl_seconds
            return True

    async def exists(self, key: str) -> bool:
        return (await self.get(key)) is not None

    async def get_many(self, keys: list[str]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key in keys:
            value = await self.get(key)
            if value is not None:
                result[key] = value
        return result

    async def health_check(self) -> bool:
        return True

    async def close(self) -> None:
        async with self._lock:
            self._data.clear()


# ─────────────────────────────────────────────────────────────────────
# Redis 实现
# ─────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class RedisHotStateConfig:
    """RedisHotStateStore 实例化所需的配置。"""

    url: str = "redis://127.0.0.1:6379/0"
    socket_connect_timeout_seconds: float = 3.0
    socket_timeout_seconds: float = 3.0
    health_check_interval_seconds: float = 10.0
    # value 编解码：默认 JSON。如需更高效（msgpack/orjson）可在 Stage 6+
    # 落地时替换。
    encoding: str = "utf-8"
    # 全局 namespace 前缀：用于多环境（dev/staging/prod）共享同一台 Redis
    # 时避免冲突。例如 dev 环境用 "dev:"，prod 用 "prod:"。
    global_prefix: str = ""

    def apply_prefix(self, key: str) -> str:
        if not self.global_prefix:
            return key
        return f"{self.global_prefix}{key}"


class RedisHotStateStore(HotStateStore):
    """Redis 后端的 HotStateStore 实现。

    Stage 6 落地后会被 build_runtime 在多进程模式下注入。本类的 __init__
    不做任何 I/O，可以被 monolith 安全 import 而不必装 redis-py。

    使用方式::

        store = RedisHotStateStore(config=RedisHotStateConfig())
        await store.connect()
        await store.set(make_key("market", "BTC-USDT", "15m"), {"px": 65000}, ttl_seconds=30)
        latest = await store.get(make_key("market", "BTC-USDT", "15m"))
        await store.close()
    """

    def __init__(self, config: RedisHotStateConfig) -> None:
        self._config = config
        self._client: AsyncRedis | None = None

    async def connect(self) -> None:
        """惰性连接 Redis，并 ping 验证可用。"""
        if self._client is not None:
            return
        try:
            from redis.asyncio import Redis as AsyncRedis  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "redis is required for RedisHotStateStore. "
                "Install with: pip install 'redis>=5,<6'"
            ) from exc
        client = AsyncRedis.from_url(
            self._config.url,
            socket_connect_timeout=self._config.socket_connect_timeout_seconds,
            socket_timeout=self._config.socket_timeout_seconds,
            health_check_interval=self._config.health_check_interval_seconds,
            decode_responses=False,
        )
        await client.ping()
        self._client = client

    def _ensure_client(self) -> "AsyncRedis":
        if self._client is None:
            raise RuntimeError(
                "RedisHotStateStore.connect() must be called before any I/O operation"
            )
        return self._client

    def _encode(self, value: Any) -> bytes:
        return json.dumps(value, default=str).encode(self._config.encoding)

    def _decode(self, raw: bytes | None) -> Any | None:
        if raw is None:
            return None
        return json.loads(raw.decode(self._config.encoding))

    async def get(self, key: str) -> Any | None:
        client = self._ensure_client()
        raw = await client.get(self._config.apply_prefix(key))
        return self._decode(raw)

    async def set(
        self,
        key: str,
        value: Any,
        *,
        ttl_seconds: float | None = None,
    ) -> None:
        client = self._ensure_client()
        encoded = self._encode(value)
        full_key = self._config.apply_prefix(key)
        if ttl_seconds is None:
            await client.set(full_key, encoded)
        else:
            # Redis EX 是整数秒，PX 是整数毫秒；对小于 1 秒的 TTL 用 PX
            ms = max(int(round(ttl_seconds * 1000)), 1)
            await client.set(full_key, encoded, px=ms)

    async def delete(self, key: str) -> None:
        client = self._ensure_client()
        await client.delete(self._config.apply_prefix(key))

    async def expire(self, key: str, ttl_seconds: float) -> bool:
        client = self._ensure_client()
        ms = max(int(round(ttl_seconds * 1000)), 1)
        result = await client.pexpire(self._config.apply_prefix(key), ms)
        return bool(result)

    async def exists(self, key: str) -> bool:
        client = self._ensure_client()
        return bool(await client.exists(self._config.apply_prefix(key)))

    async def get_many(self, keys: list[str]) -> dict[str, Any]:
        if not keys:
            return {}
        client = self._ensure_client()
        full_keys = [self._config.apply_prefix(k) for k in keys]
        raws = await client.mget(full_keys)
        out: dict[str, Any] = {}
        for original_key, raw in zip(keys, raws, strict=True):
            decoded = self._decode(raw)
            if decoded is not None:
                out[original_key] = decoded
        return out

    async def health_check(self) -> bool:
        client = self._ensure_client()
        try:
            return bool(await client.ping())
        except Exception:
            return False

    async def close(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.aclose()
        except Exception:
            pass
        self._client = None


# ─────────────────────────────────────────────────────────────────────
# 工厂方法
# ─────────────────────────────────────────────────────────────────────


def build_hot_state_store(
    *,
    backend: str = "memory",
    redis_config: RedisHotStateConfig | None = None,
) -> HotStateStore:
    """根据配置选择合适的 HotStateStore 实现。

    backend = "memory"：返回 InMemoryHotStateStore（monolith 默认）
    backend = "redis"：返回 RedisHotStateStore，需调 connect() 才能使用
    """
    backend_normalized = backend.strip().lower()
    if backend_normalized == "memory":
        return InMemoryHotStateStore()
    if backend_normalized == "redis":
        return RedisHotStateStore(config=redis_config or RedisHotStateConfig())
    raise ValueError(f"unknown hot_state_backend: {backend!r}")


def serialize_for_hot_state(value: Mapping[str, Any]) -> dict[str, Any]:
    """把 pydantic / dataclass / dict 统一序列化成 JSON-friendly dict。

    Caller 在 set() 之前应当用这个把复杂对象拍平。
    """
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")  # type: ignore[no-any-return]
    return dict(value)
