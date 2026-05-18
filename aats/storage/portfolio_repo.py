from __future__ import annotations

import logging
from collections.abc import Callable

from aats.schemas.portfolio import PortfolioSnapshot, is_baseline_snapshot, is_trusted_baseline_snapshot
from aats.services.runtime_scope import RuntimeStateScope, filter_snapshots, latest_matching_snapshot

_logger = logging.getLogger("aats.storage.portfolio_repo")


class InMemoryPortfolioRepository:
    def __init__(self) -> None:
        self._snapshots: list[PortfolioSnapshot] = []
        # Stage 6 Slice 6.3 hot-fix：portfolio_repo → snapshot_cache 同进程 listener。
        # 设计文档 docs/task/stage_6_slice_6_3_cache_listener_fix_design.md。
        # 所有绕过 outbox publisher 直接 save_snapshot 的路径（recovery/repair/
        # projections/positions/tests）通过这个钩子把 snapshot 同步到 cache 本地
        # dict，修复 operator UI 读到 stale bootstrap snapshot 的资金安全 bug。
        self._snapshot_listener: Callable[[PortfolioSnapshot], None] | None = None

    def attach_snapshot_listener(
        self, listener: Callable[[PortfolioSnapshot], None]
    ) -> None:
        """注入 snapshot listener，每次 save_snapshot 后同步调用。

        listener 抛异常会被捕获并 log warning，不会拖垮 save_snapshot 的主路径。
        """
        self._snapshot_listener = listener

    def save_snapshot(self, snapshot: PortfolioSnapshot) -> None:
        self._snapshots.append(snapshot)
        self._notify_listener(snapshot)

    def _notify_listener(self, snapshot: PortfolioSnapshot) -> None:
        listener = self._snapshot_listener
        if listener is None:
            return
        try:
            listener(snapshot)
        except Exception as exc:  # pragma: no cover - best-effort
            _logger.warning(
                "portfolio_repo_snapshot_listener_failed error_type=%s error=%s",
                type(exc).__name__,
                exc,
            )

    def latest(self) -> PortfolioSnapshot | None:
        return self._snapshots[-1] if self._snapshots else None

    def history(self) -> list[PortfolioSnapshot]:
        return list(self._snapshots)

    def recent_history(self, *, limit: int) -> list[PortfolioSnapshot]:
        if limit <= 0:
            return []
        return list(self._snapshots[-limit:])

    def history_for_scope(
        self,
        *,
        scope: RuntimeStateScope,
        limit: int | None = None,
    ) -> list[PortfolioSnapshot]:
        rows = filter_snapshots(self._snapshots, scope)
        if limit is not None:
            rows = rows[-limit:]
        return list(rows)

    def latest_for_scope(self, *, scope: RuntimeStateScope) -> PortfolioSnapshot | None:
        return latest_matching_snapshot(self._snapshots, scope)

    def latest_baseline_for_scope(self, *, scope: RuntimeStateScope) -> PortfolioSnapshot | None:
        scoped = filter_snapshots(self._snapshots, scope)
        for snapshot in reversed(scoped):
            if is_baseline_snapshot(snapshot):
                return snapshot
        return None

    def latest_trusted_baseline_for_scope(self, *, scope: RuntimeStateScope) -> PortfolioSnapshot | None:
        scoped = filter_snapshots(self._snapshots, scope)
        for snapshot in reversed(scoped):
            if is_trusted_baseline_snapshot(snapshot):
                return snapshot
        return None
