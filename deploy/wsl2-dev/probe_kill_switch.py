"""Stage 6 Slice 6.4 — 4 进程真跑 halt/resume drill 驱动脚本。

挂进任意一个 AATS 容器（推荐 gateway）内运行，构造一个独立的 KillSwitch
实例（不复用运行时单例），通过真实 Redis + 真实 NATS JetStream 触发一次
halt → Redis readback → resume → Redis readback。

验证目标
========
- S6.4 设计文档 §7.1 I1/I2/I3：
  - halt_async 写入 Redis ``aats:hot:system:kill_switch``（halted=true）
  - halt_async 通过 NATS ``aats.system.kill_switch_state`` 广播
  - 4 个运行时容器（gateway/market/decision/execution）订阅了该 topic，
    会在日志里打印 ``kill_switch_remote_applied process_role=<self> source_role=drill_probe``
- 本脚本不动任何真实运行时单例，不影响 4 进程内部状态；对它们来说这相当于
  "有第 5 个进程临时加入 kill_switch 广播圈，halt 完就消失"。

用法（容器内）::

    PYTHONPATH=/app python /workspace/probe_kill_switch.py halt
    PYTHONPATH=/app python /workspace/probe_kill_switch.py resume
    PYTHONPATH=/app python /workspace/probe_kill_switch.py status

⚠️ 本脚本用 ``source_role=drill_probe``，运行时容器不会把这个事件当作自己发的
回环（自己的 process_role 是 gateway/market/decision/execution），所以远端
订阅者会真正 apply。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any

from aats.bus.nats_bus import NatsBusConfig, NatsEventBus
from aats.events import topics
from aats.services.governance_engine.kill_switch import (
    KILL_SWITCH_REDIS_KEY,
    KillSwitch,
)
from aats.storage.event_store import InMemoryEventStore
from aats.storage.hot_state_store import RedisHotStateConfig, RedisHotStateStore


_PROCESS_ROLE = "drill_probe"
_CRITICAL_TOPICS = [topics.KILL_SWITCH_STATE]


async def _build_deps() -> tuple[RedisHotStateStore, NatsEventBus]:
    redis_url = os.environ.get("AATS_HOT_STATE_REDIS_URL", "redis://redis:6379/0")
    nats_url = os.environ.get("AATS_NATS_URL", "nats://nats:4222")

    print(f"[probe] redis={redis_url} nats={nats_url}", flush=True)

    store = RedisHotStateStore(RedisHotStateConfig(url=redis_url))
    await store.connect()

    bus = NatsEventBus(
        config=NatsBusConfig(servers=[nats_url]),
        event_store=InMemoryEventStore(),
        persistence_mode="best_effort",
        consumer_role=_PROCESS_ROLE,
    )
    # 只 connect，不 ensure_stream——AATS_EVENTS stream 已经由 gateway 启动时
    # 建好，这里重跑 add_stream 会因 config 冲突而报错。
    await bus.connect()
    return store, bus


async def _cmd_halt(reason: str) -> int:
    store, bus = await _build_deps()
    ks = KillSwitch()
    logger = logging.getLogger("probe.kill_switch")
    await ks.bootstrap(
        hot_state_store=store,
        bus=bus,
        process_role=_PROCESS_ROLE,
        logger=logger,
    )
    print(f"[probe] bootstrap done, initial halted={ks.halted}", flush=True)
    await ks.halt_async(reason)
    print(f"[probe] halt_async returned, local halted={ks.halted}", flush=True)
    raw: Any = await store.get(KILL_SWITCH_REDIS_KEY)
    print(f"[probe] redis readback: {raw}", flush=True)
    await bus.close()
    await store.close()
    if not isinstance(raw, dict) or not raw.get("halted"):
        print("[probe] FAIL: redis did not reflect halt", flush=True)
        return 1
    print("[probe] OK: halt persisted + broadcast", flush=True)
    return 0


async def _cmd_resume() -> int:
    store, bus = await _build_deps()
    ks = KillSwitch()
    logger = logging.getLogger("probe.kill_switch")
    await ks.bootstrap(
        hot_state_store=store,
        bus=bus,
        process_role=_PROCESS_ROLE,
        logger=logger,
    )
    print(f"[probe] bootstrap done, initial halted={ks.halted}", flush=True)
    await ks.resume_async()
    print(f"[probe] resume_async returned, local halted={ks.halted}", flush=True)
    raw: Any = await store.get(KILL_SWITCH_REDIS_KEY)
    print(f"[probe] redis readback: {raw}", flush=True)
    await bus.close()
    await store.close()
    if isinstance(raw, dict) and raw.get("halted"):
        print("[probe] FAIL: redis still halted after resume", flush=True)
        return 1
    print("[probe] OK: resume persisted + broadcast", flush=True)
    return 0


async def _cmd_status() -> int:
    store, _bus = await _build_deps()
    raw: Any = await store.get(KILL_SWITCH_REDIS_KEY)
    print(f"[probe] redis state: {raw}", flush=True)
    await _bus.close()
    await store.close()
    return 0


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if len(sys.argv) < 2:
        print("usage: probe_kill_switch.py <halt|resume|status> [reason]", file=sys.stderr)
        return 2
    cmd = sys.argv[1]
    if cmd == "halt":
        reason = sys.argv[2] if len(sys.argv) > 2 else "slice-6.4-14b-drill"
        return await _cmd_halt(reason)
    if cmd == "resume":
        return await _cmd_resume()
    if cmd == "status":
        return await _cmd_status()
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
