"""Stage 6 Slice 6.3 Cache Fix — 4 进程真跑 listener 验证脚本。

挂进任一个 AATS 容器内跑，在容器的真实环境下：

1. 直接 new 一个 ``InMemoryPortfolioRepository`` 实例（与 4 进程真跑拓扑
   默认 ``storage_mode=memory`` 对齐；Postgres 分支单测已覆盖）
2. 建一个独立 ``PortfolioSnapshotCache``（纯本地 in-memory store + in-memory bus）
3. ``repo.attach_snapshot_listener(cache.apply_sync)``
4. ``repo.save_snapshot(<fresh PortfolioSnapshot>)``
5. 立刻 ``cache.get_sync(scope)`` 读回，断言 decision_id / total_equity 一致

验证目标
========
- S6.3 Cache Fix 设计文档 §D6：listener hook 在容器真实 env（相同的 PYTHONPATH、
  相同的 aats 包版本）下端到端工作
- 不依赖 runtime 单例，只验证 wiring 代码路径 + repo write + listener callback
- 不污染 cache.publish() 的 Redis 路径（listener 只更新本地 dict）

用法（容器内）::

    PYTHONPATH=/app python /tmp/probe_repo_cache_listener.py

⚠️ 本脚本不碰 Postgres / Redis / NATS，安全运行，不污染真实数据。
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from aats.bus.memory_bus import InMemoryEventBus
from aats.schemas.portfolio import PortfolioSnapshot
from aats.services.portfolio_service.snapshot_cache import PortfolioSnapshotCache
from aats.services.runtime_scope import RuntimeStateScope
from aats.storage.hot_state_store import InMemoryHotStateStore
from aats.storage.portfolio_repo import InMemoryPortfolioRepository


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger = logging.getLogger("probe.repo_cache_listener")

    repo = InMemoryPortfolioRepository()
    cache = PortfolioSnapshotCache(
        hot_state_store=InMemoryHotStateStore(),
        bus=InMemoryEventBus(),
        process_role="probe_repo_cache",
        logger=logger,
    )

    attach: Any = getattr(repo, "attach_snapshot_listener", None)
    if not callable(attach):
        print("[probe] FAIL: repo has no attach_snapshot_listener (code regression)", flush=True)
        return 1
    attach(cache.apply_sync)
    print("[probe] listener attached", flush=True)

    marker_ts = datetime.now(tz=timezone.utc)
    decision_id = f"probe-cache-fix-{int(marker_ts.timestamp())}"
    snapshot = PortfolioSnapshot(
        decision_id=decision_id,
        snapshot_origin="fill_derived",
        snapshot_ts=marker_ts,
        balances={"USDT": Decimal("12345.67")},
        positions=[],
        cost_basis={},
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        total_equity=Decimal("12345.67"),
        gross_exposure=Decimal("0"),
        net_exposure=Decimal("0"),
        product_type="spot",
        margin_mode="cash",
        cash_equity=Decimal("12345.67"),
    )

    print(f"[probe] saving snapshot decision_id={decision_id}", flush=True)
    repo.save_snapshot(snapshot)
    print(f"[probe] repo.history().len={len(repo.history())}", flush=True)

    scope = RuntimeStateScope(
        product_type="spot",  # type: ignore[arg-type]
        margin_mode="cash",  # type: ignore[arg-type]
        allowed_symbols=("BTC-USDT",),
        default_symbol="BTC-USDT",
    )
    cached = cache.get_sync(scope)
    if cached is None:
        print("[probe] FAIL: cache.get_sync returned None — listener did not fire", flush=True)
        return 1
    if cached.decision_id != decision_id:
        print(
            f"[probe] FAIL: cache returned older snapshot {cached.decision_id} "
            f"(expected {decision_id}) — noop path mis-triggered",
            flush=True,
        )
        return 1
    if cached.total_equity != Decimal("12345.67"):
        print(
            f"[probe] FAIL: cache total_equity={cached.total_equity}, expected 12345.67",
            flush=True,
        )
        return 1

    print(
        f"[probe] OK: cache hit decision_id={cached.decision_id} "
        f"total_equity={cached.total_equity} snapshot_ts={cached.snapshot_ts}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
