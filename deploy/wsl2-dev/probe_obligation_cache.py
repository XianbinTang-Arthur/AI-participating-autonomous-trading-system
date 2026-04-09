"""Stage 6 Slice 6.5 — ObligationHotStateCache 4 进程真跑验证驱动脚本。

设计文档
========
docs/task/stage_6_slice_6_5_obligation_hot_state_design.md §12
deploy/wsl2-dev/RUNBOOK.md §9.8

用法（容器内）
==============
    docker exec aats-gateway env \\
        PYTHONPATH=/app \\
        AATS_HOT_STATE_REDIS_URL=redis://redis:6379/0 \\
        AATS_NATS_URL=nats://nats:4222 \\
        python /workspace/probe_obligation_cache.py

验证目标
========
D5  cache.publish 写 Redis 后，redis-cli 能查到：
      - aats:hot:obligation:index  (包含 probe 的 client_order_id + version++)
      - aats:hot:obligation:by_coid:<probe_coid>  (full obligation JSON)
I3  probe 发出的 NATS OBLIGATION_UPDATES 事件应被 4 个容器全部订阅到，
    本脚本结束后立刻 grep 4 个容器 logs 可以看到
    ``obligation_cache_remote_applied client_order_id=<probe_coid>``
I1  脚本即便在 fail-soft 分支下（譬如 Redis / NATS 短暂挂掉）也不能抛错，
    这一点靠 cache 自己的 best_effort 实现；本脚本做 echo 验证而不做
    failure injection（那是集成测试的职责）。

注意：本脚本不 attach NATS durable subscription（与 probe_snapshot_cache.py
同理），只走 publish 路径；broadcast 目标是 4 个已经 subscribe 的 container
cache。script 自身也不读回发布的 obligation（apply_locally 已经把它写进
自己的 self._latest，但 script 进程会随即退出）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal

from aats.bus.nats_bus import NatsBusConfig, NatsEventBus
from aats.schemas.execution import OrderObligation
from aats.services.execution_engine.obligation_cache import (
    OBLIGATION_INDEX_KEY,
    ObligationHotStateCache,
    _obligation_key,
)
from aats.storage.hot_state_store import RedisHotStateConfig, RedisHotStateStore


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger = logging.getLogger("probe.obligation_cache")
    logger.setLevel(logging.INFO)

    redis_url = os.environ.get(
        "AATS_HOT_STATE_REDIS_URL", "redis://redis:6379/0"
    )
    nats_url = os.environ.get("AATS_NATS_URL", "nats://nats:4222")

    print(f"[probe] redis={redis_url} nats={nats_url}", flush=True)

    store = RedisHotStateStore(RedisHotStateConfig(url=redis_url))
    await store.connect()

    bus = NatsEventBus(
        config=NatsBusConfig(servers=(nats_url,)),
        consumer_role="probe",
    )
    # 只连接，不做 ensure_stream（4 个容器已经 ensure 过同一个 stream）
    await bus.connect()

    cache = ObligationHotStateCache(logger=logger)
    # subscribe=False：probe 不 attach 任何 durable consumer，避免和 4 个容器
    # 竞争 delivery（durable name 如果碰撞，NATS 会拒绝或 re-route 到新 client）
    await cache.bootstrap(
        hot_state_store=store,
        bus=bus,
        process_role="probe",
        subscribe=False,
    )
    snap0 = cache.snapshot()
    print(
        f"[probe] bootstrap done: bootstrapped={snap0['bootstrapped']} "
        f"cached_count={snap0['cached_count']} "
        f"index_version={snap0['index_version']}",
        flush=True,
    )

    # 构造一份 probe obligation。
    # client_order_id 里嵌 ts epoch 避免与历史 key 碰撞，便于 grep 容器 logs
    now = datetime.now(timezone.utc)
    probe_coid = f"probe-slice65-coid-{int(now.timestamp())}"
    obligation = OrderObligation(
        client_order_id=probe_coid,
        decision_id=f"probe-slice65-decision-{int(now.timestamp())}",
        intent_id=f"probe-slice65-intent-{int(now.timestamp())}",
        symbol="BTC-USDT",
        side="buy",
        reserve_currency="USDT",
        reserved_amount=Decimal("100"),
        consumed_amount=Decimal("0"),
        released_amount=Decimal("0"),
        status="ACTIVE",
        product_type="spot",
        margin_mode="cash",
        reference_price=Decimal("65000"),
        last_update_ts=now,
    )
    print(
        f"[probe] publishing obligation coid={probe_coid} status=ACTIVE",
        flush=True,
    )
    await cache.publish(obligation)

    # ── D5 自检：Redis per-coid key + index key 都应该被写 ──
    coid_key = _obligation_key(probe_coid)
    coid_raw = await store.get(coid_key)
    if coid_raw is None:
        print(f"[probe] FAIL: redis key missing: {coid_key}", flush=True)
        await bus.close()
        await store.close()
        return 1
    print(f"[probe] OK: redis per-coid key written: {coid_key}", flush=True)

    index_raw = await store.get(OBLIGATION_INDEX_KEY)
    if not isinstance(index_raw, dict):
        print("[probe] FAIL: redis index key missing or not dict", flush=True)
        await bus.close()
        await store.close()
        return 1
    all_coids = index_raw.get("all_coids") or []
    active_coids = index_raw.get("active_coids") or []
    version = index_raw.get("version")
    writer = index_raw.get("writer_role")
    print(
        f"[probe] OK: redis index key written: version={version} "
        f"writer_role={writer} all_coids={len(all_coids)} "
        f"active_coids={len(active_coids)}",
        flush=True,
    )
    if probe_coid not in all_coids:
        print(
            f"[probe] FAIL: probe coid {probe_coid} not in index.all_coids",
            flush=True,
        )
        await bus.close()
        await store.close()
        return 1
    if probe_coid not in active_coids:
        print(
            f"[probe] FAIL: probe coid {probe_coid} not in index.active_coids",
            flush=True,
        )
        await bus.close()
        await store.close()
        return 1
    print(
        f"[probe] OK: probe coid {probe_coid} in both all_coids and active_coids",
        flush=True,
    )

    # 让 NATS deliver 有充足时间跨进程到 4 个订阅者
    await asyncio.sleep(1.0)

    # 现在主动发一次 status=RELEASED 的 follow-up，验证 idempotent
    # version 递增（index 里同一个 coid 但 active_coids 里应消失）
    # 注意：合法 ObligationStatus 是 ACTIVE / PARTIALLY_CONSUMED / RELEASED /
    # CANCELED / FAILED，这里选 RELEASED 作为"obligation 完全消费掉、释放"的
    # 自然终态。active_coids filter 只收 {ACTIVE, PARTIALLY_CONSUMED}。
    now2 = datetime.now(timezone.utc)
    obligation2 = obligation.model_copy(
        update={
            "status": "RELEASED",
            "consumed_amount": Decimal("100"),
            "released_amount": Decimal("100"),
            "last_update_ts": now2,
        }
    )
    print(
        f"[probe] publishing follow-up obligation coid={probe_coid} "
        f"status=RELEASED",
        flush=True,
    )
    await cache.publish(obligation2)

    index_raw2 = await store.get(OBLIGATION_INDEX_KEY)
    assert isinstance(index_raw2, dict)
    active_coids2 = index_raw2.get("active_coids") or []
    all_coids2 = index_raw2.get("all_coids") or []
    version2 = index_raw2.get("version")
    print(
        f"[probe] index after follow-up: version={version2} "
        f"all_coids={len(all_coids2)} active_coids={len(active_coids2)}",
        flush=True,
    )
    if probe_coid in active_coids2:
        print(
            f"[probe] FAIL: probe coid {probe_coid} should be removed "
            f"from active_coids after CONSUMED",
            flush=True,
        )
        await bus.close()
        await store.close()
        return 1
    if probe_coid not in all_coids2:
        print(
            f"[probe] FAIL: probe coid {probe_coid} should still be "
            f"in all_coids after CONSUMED",
            flush=True,
        )
        await bus.close()
        await store.close()
        return 1
    if not isinstance(version2, int) or not isinstance(version, int):
        print("[probe] FAIL: version type check", flush=True)
        await bus.close()
        await store.close()
        return 1
    if version2 <= version:
        print(
            f"[probe] FAIL: index version did not increment: "
            f"before={version} after={version2}",
            flush=True,
        )
        await bus.close()
        await store.close()
        return 1
    print(
        f"[probe] OK: follow-up publish moved probe coid out of "
        f"active_coids; version {version} -> {version2}",
        flush=True,
    )

    # 再 sleep 让第二个 NATS broadcast 送达
    await asyncio.sleep(1.0)

    print(
        f"[probe] DONE: all D5 write-through + I3 broadcast paths fired; "
        f"grep '{probe_coid}' on 4 container logs next.",
        flush=True,
    )
    await cache.stop()
    await bus.close()
    await store.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
