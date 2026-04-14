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
    # ── Backlog 稳定性阈值 ───────────────────────────────────────────
    #
    # Phase 1 shadow 的 primary 写（obligation / order_state / fill_event）
    # 和 shadow 写（reservation / execution_order / execution_fill）分属
    # 不同 DB 事务。snapshot() 在两个 SELECT 之间运行时，会观测到瞬态
    # 幻影 backlog（primary TX 已 commit，shadow TX 尚未执行）。
    #
    # 实际案例：
    #   outbox TX commit → obligation_count=N+1
    #   _shadow_sync_obligation 尚未执行 → reservation_count=N
    #   snapshot() 此时运行 → obligation_backlog=1 → "lagging"
    #   ~12 秒后 shadow sync 完成 → backlog 归零
    #   但 "lagging" 已触发 reconciliation_halt → BLOCKED legs → bundle 停摆
    #
    # 修复策略：所有三类 backlog（order / fill / obligation）统一要求
    # 连续 _BACKLOG_STABLE_TICKS 个 snapshot 周期都检测到 backlog > 0
    # 才判定 "lagging"。瞬态 1-2 tick 幻影不会触发阻断。
    _BACKLOG_STABLE_TICKS = 3

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
        # 连续检测到 backlog > 0 的 tick 计数（跨 snapshot 调用持久化）
        self._consecutive_backlog_ticks: int = 0

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
        # obligation backlog 比较必须与 reservation_repo（DB 来源）同源。
        # 旧逻辑用 cache.all_sync()，但 cache 从 Redis hydrate，重启后
        # 可能包含 DB 已无的陈旧条目（已 RELEASED 但 Redis 未清理），
        # 造成 cache(1) vs DB(0) 的幻影 backlog → 永久阻断。
        # 改为始终用 obligation_repo（DB）作为 backlog 计数的权威来源。
        obligation_count = len(self.obligation_repo.all_obligations())
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
        raw_backlog_present = any((value or 0) > 0 for value in lag.values() if value is not None)

        # ── Backlog 稳定性去抖 ──────────────────────────────────────
        # primary→shadow 写分属不同 DB 事务。snapshot() 在两者之间运行
        # 会看到瞬态 1-tick 幻影 backlog。仅当连续 N 个 tick 都检测到
        # backlog > 0 才认定真正 "lagging"，防止瞬态竞态触发级联停摆。
        if raw_backlog_present:
            self._consecutive_backlog_ticks += 1
        else:
            self._consecutive_backlog_ticks = 0
        backlog_present = (
            raw_backlog_present
            and self._consecutive_backlog_ticks >= self._BACKLOG_STABLE_TICKS
        )

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
            "backlog_stability": {
                "consecutive_ticks": self._consecutive_backlog_ticks,
                "threshold": self._BACKLOG_STABLE_TICKS,
                "raw_backlog_present": raw_backlog_present,
                "stable_backlog_present": backlog_present,
            },
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
