from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aats.services.execution_control.shadow import Phase1ExecutionShadowService
from aats.services.ledger.posting import Phase1LedgerMirrorService
from aats.storage.base import ExecutionObligationRepository, ExecutionRepository
from aats.storage.execution_fill_repo_v2 import ExecutionFillRepositoryV2
from aats.storage.execution_order_repo import ExecutionOrderRepository
from aats.storage.reservation_repo import ReservationRepositoryV2
from aats.services.runtime_scope import RuntimeStateScope, fills_for_scope, order_states_for_scope

if TYPE_CHECKING:
    from aats.services.execution_engine.obligation_cache import ObligationHotStateCache


class Phase1ShadowMonitor:
    def __init__(
        self,
        *,
        execution_repo: ExecutionRepository,
        obligation_repo: ExecutionObligationRepository,
        state_scope: RuntimeStateScope,
        execution_shadow_service: Phase1ExecutionShadowService | None = None,
        ledger_mirror_service: Phase1LedgerMirrorService | None = None,
        execution_order_repo: ExecutionOrderRepository | None = None,
        execution_fill_repo: ExecutionFillRepositoryV2 | None = None,
        reservation_repo: ReservationRepositoryV2 | None = None,
    ) -> None:
        self.execution_repo = execution_repo
        self.obligation_repo = obligation_repo
        self.state_scope = state_scope
        self.execution_shadow_service = execution_shadow_service
        self.ledger_mirror_service = ledger_mirror_service
        self.execution_order_repo = execution_order_repo
        self.execution_fill_repo = execution_fill_repo
        self.reservation_repo = reservation_repo
        # Stage 6 Slice 6.5：跨进程 obligation 缓存。Phase1ShadowMonitor 在
        # _build_shared_runtime_slice 早期构造，此时 obligation_hot_state_cache
        # 还没 bootstrap；所以采用**setter 注入**模式：build_runtime 在 cache
        # bootstrap 后调 attach_obligation_cache(cache)。snapshot() 里的
        # backlog 计算会优先用 cache.all_sync() + repo fallback（I5 miss 不
        # 破坏读）。
        self._obligation_cache: "ObligationHotStateCache | None" = None

    def attach_obligation_cache(
        self, obligation_cache: "ObligationHotStateCache | None"
    ) -> None:
        """Stage 6 Slice 6.5：延迟注入 obligation cache。

        build_runtime 在完成 ``ObligationHotStateCache.bootstrap(...)`` 之后调
        本方法，把 cache 交给 monitor 用于 dashboard backlog 统计。重复调用
        幂等（只是替换引用）。
        """
        self._obligation_cache = obligation_cache

    def snapshot(self) -> dict[str, Any]:
        execution_shadow = (
            self.execution_shadow_service.snapshot()
            if self.execution_shadow_service is not None
            else {
                "configured": False,
                "status": "not_configured",
                "last_outcome": "idle",
                "order_attempt_count": 0,
                "order_success_count": 0,
                "order_failure_count": 0,
                "fill_attempt_count": 0,
                "fill_success_count": 0,
                "fill_failure_count": 0,
                "last_order_sync_ts": None,
                "last_fill_sync_ts": None,
                "last_failure_ts": None,
                "last_error": None,
                "last_synced_order_id": None,
                "last_synced_order_state": None,
                "last_synced_fill_id": None,
            }
        )
        ledger_shadow = (
            self.ledger_mirror_service.snapshot()
            if self.ledger_mirror_service is not None
            else {
                "configured": False,
                "status": "not_configured",
                "last_outcome": "idle",
                "sync_attempt_count": 0,
                "sync_success_count": 0,
                "sync_failure_count": 0,
                "last_sync_ts": None,
                "last_failure_ts": None,
                "last_reason": None,
                "last_synced_order_id": None,
                "last_synced_fill_id": None,
                "last_obligation_status": None,
                "last_error": None,
            }
        )
        order_backlog = (
            None
            if self.execution_order_repo is None
            else max(len(order_states_for_scope(self.execution_repo, self.state_scope)) - self.execution_order_repo.count_orders(), 0)
        )
        fill_backlog = (
            None
            if self.execution_fill_repo is None
            else max(len(fills_for_scope(self.execution_repo, self.state_scope)) - self.execution_fill_repo.count_fills(), 0)
        )
        # Stage 6 Slice 6.5：cache 优先 + repo fallback。cache.all_sync() 返回
        # None 说明未 bootstrap，退化到 obligation_repo.all_obligations()（打 PG）。
        cached_obligations: list | None = None
        if self._obligation_cache is not None:
            cached_obligations = self._obligation_cache.all_sync()
        obligation_count = (
            len(cached_obligations)
            if cached_obligations is not None
            else len(self.obligation_repo.all_obligations())
        )
        obligation_backlog = (
            None
            if self.reservation_repo is None
            else max(obligation_count - self.reservation_repo.count_reservations(), 0)
        )
        lag = {
            "order_backlog": order_backlog,
            "fill_backlog": fill_backlog,
            "obligation_backlog": obligation_backlog,
        }
        backlog_present = any((value or 0) > 0 for value in lag.values() if value is not None)
        if not execution_shadow["configured"] and not ledger_shadow["configured"]:
            status = "not_configured"
        elif execution_shadow["status"] == "degraded" or ledger_shadow["status"] == "degraded":
            status = "degraded"
        elif backlog_present:
            status = "lagging"
        elif execution_shadow["status"] == "healthy" or ledger_shadow["status"] == "healthy":
            status = "healthy"
        else:
            status = "idle"
        blockers: list[str] = []
        if status == "degraded":
            blockers.append("phase1_shadow_degraded")
        elif status == "lagging":
            blockers.append("phase1_shadow_lagging")
        return {
            "configured": bool(execution_shadow["configured"] or ledger_shadow["configured"]),
            "status": status,
            "connected": True,
            "ready": status in {"healthy", "idle", "not_configured"},
            "fresh": status not in {"degraded", "lagging"},
            "detail": (summary_text := self._summary(status=status, lag=lag)),
            "blockers": blockers,
            "summary": summary_text,
            "lag": lag,
            "execution_shadow": execution_shadow,
            "ledger_shadow": ledger_shadow,
        }

    @staticmethod
    def _summary(*, status: str, lag: dict[str, Any]) -> str:
        if status == "not_configured":
            return "Phase 1 shadow compatibility layer is not configured in this runtime."
        if status == "degraded":
            return "Phase 1 shadow compatibility layer has recent write failures and should block automated continuation."
        if status == "lagging":
            return (
                "Phase 1 shadow compatibility layer is behind the legacy runtime. "
                f"order_backlog={lag.get('order_backlog')}, fill_backlog={lag.get('fill_backlog')}, obligation_backlog={lag.get('obligation_backlog')}"
            )
        if status == "healthy":
            return "Phase 1 shadow compatibility layer is tracking legacy execution and obligation flows."
        return "Phase 1 shadow compatibility layer is configured but has not processed shadow traffic yet."
