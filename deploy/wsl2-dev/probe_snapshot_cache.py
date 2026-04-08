"""Stage 6 Slice 6.3 — 4 进程真跑验证驱动脚本。

挂进任意一个 AATS 容器内运行，构造一份 PortfolioSnapshot 并通过
PortfolioSnapshotCache.publish() 写入：本地 dict + best-effort Redis。

验证目标：
- D5: cache.publish 写 Redis 后，redis-cli 能查到 aats:hot:portfolio:latest:spot:cash
- I3: 重启另一个进程，下一次 bootstrap 应该 hydrate（不是 empty）

注意：这个脚本不发 NATS 广播——cache.publish 设计上 "best-effort 本地 + Redis"，
NATS 通路由 outbox publisher 的 flush_pending() 驱动，本脚本不动。

用法（容器内）：
    PYTHONPATH=/app python /workspace/probe_snapshot_cache.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal

from aats.bus.memory_bus import InMemoryEventBus
from aats.schemas.portfolio import PortfolioSnapshot
from aats.services.portfolio_service.snapshot_cache import PortfolioSnapshotCache
from aats.storage.hot_state_store import RedisHotStateConfig, RedisHotStateStore


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logger = logging.getLogger("probe.snapshot_cache")
    logger.setLevel(logging.INFO)

    redis_url = os.environ.get("AATS_HOT_STATE_REDIS_URL", "redis://redis:6379/0")
    print(f"[probe] connecting to redis at {redis_url}", flush=True)
    store = RedisHotStateStore(RedisHotStateConfig(url=redis_url))
    await store.connect()

    bus = InMemoryEventBus()
    cache = PortfolioSnapshotCache(
        hot_state_store=store,
        bus=bus,
        process_role="probe",
        logger=logger,
    )
    # 用 in-memory bus 走默认 subscribe=True 路径，不会撞 NATS durable
    await cache.bootstrap(scope_fingerprint="spot:cash")

    snapshot = PortfolioSnapshot(
        decision_id="probe-decision-1",
        snapshot_origin="fill_derived",
        snapshot_ts=datetime.now(tz=timezone.utc),
        balances={"USDT": Decimal("100000")},
        positions=[],
        cost_basis={},
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        total_equity=Decimal("100000"),
        gross_exposure=Decimal("0"),
        net_exposure=Decimal("0"),
        product_type="spot",
        margin_mode="cash",
        cash_equity=Decimal("100000"),
    )
    print(f"[probe] publishing snapshot ts={snapshot.snapshot_ts.isoformat()}", flush=True)
    await cache.publish(snapshot)

    # 立刻读回 Redis 自检
    raw = await store.get("aats:hot:portfolio:latest:spot:cash")
    print(f"[probe] redis readback type={type(raw).__name__}", flush=True)
    if raw is None:
        print("[probe] FAIL: redis key missing after publish", flush=True)
        return 1
    print("[probe] OK: cache.publish wrote to Redis", flush=True)
    await store.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
