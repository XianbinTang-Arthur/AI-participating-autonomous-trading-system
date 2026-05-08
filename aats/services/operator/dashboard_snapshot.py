from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from time import perf_counter
from typing import Any

from aats.bootstrap.logging import get_logger, log_event
from aats.schemas.common import utc_now


DashboardSnapshotLoader = Callable[[str], Any | Awaitable[Any]]
DashboardSnapshotDefaultFactory = Callable[[str], Any]
_SNAPSHOT_VARIANT_SEPARATOR = "::"


def dashboard_snapshot_storage_key(panel_key: str, variant_key: str | None = None) -> str:
    normalized_panel = str(panel_key or "").strip()
    normalized_variant = str(variant_key or "").strip()
    if not normalized_variant:
        return normalized_panel
    return f"{normalized_panel}{_SNAPSHOT_VARIANT_SEPARATOR}{normalized_variant}"


def dashboard_snapshot_storage_parts(snapshot_key: str) -> tuple[str, str | None]:
    normalized = str(snapshot_key or "").strip()
    if _SNAPSHOT_VARIANT_SEPARATOR not in normalized:
        return normalized, None
    panel_key, variant_key = normalized.split(_SNAPSHOT_VARIANT_SEPARATOR, 1)
    return panel_key, variant_key or None


@dataclass(frozen=True, slots=True)
class DashboardSnapshotPolicy:
    panel_key: str
    ttl_seconds: float
    stale_after_seconds: float
    hard_expire_seconds: float
    timeout_seconds: float
    priority: str = "p0"


@dataclass(slots=True)
class DashboardPanelSnapshot:
    panel_key: str
    snapshot_key: str
    variant_key: str | None
    data: Any
    generated_at: Any
    last_success_at: Any
    duration_ms: float
    last_error: str | None = None
    last_error_at: Any | None = None


@dataclass(frozen=True, slots=True)
class DashboardSnapshotRead:
    data: Any
    error: str | None
    meta: dict[str, Any]
    duration_ms: float


@dataclass(frozen=True, slots=True)
class _DashboardSnapshotTarget:
    panel_key: str
    snapshot_key: str
    variant_key: str | None
    priority: str


_LOGGER = get_logger("aats.operator.dashboard_snapshot")


def _is_pending_refresh_timeout(last_error: str | None, *, refreshing: bool) -> bool:
    return bool(refreshing and str(last_error or "").startswith("dashboard_snapshot_refresh_timeout:"))


P0_DASHBOARD_SNAPSHOT_POLICIES: dict[str, DashboardSnapshotPolicy] = {
    "runtime": DashboardSnapshotPolicy(
        panel_key="runtime",
        ttl_seconds=3.0,
        stale_after_seconds=5.0,
        hard_expire_seconds=120.0,
        timeout_seconds=2.0,
    ),
    "health": DashboardSnapshotPolicy(
        panel_key="health",
        ttl_seconds=3.0,
        stale_after_seconds=5.0,
        hard_expire_seconds=120.0,
        timeout_seconds=2.0,
    ),
    "mode": DashboardSnapshotPolicy(
        panel_key="mode",
        ttl_seconds=5.0,
        stale_after_seconds=10.0,
        hard_expire_seconds=120.0,
        timeout_seconds=1.0,
    ),
    "systemRecovery": DashboardSnapshotPolicy(
        panel_key="systemRecovery",
        ttl_seconds=5.0,
        stale_after_seconds=10.0,
        hard_expire_seconds=120.0,
        timeout_seconds=2.0,
    ),
    "blockerControl": DashboardSnapshotPolicy(
        panel_key="blockerControl",
        ttl_seconds=3.0,
        stale_after_seconds=5.0,
        hard_expire_seconds=120.0,
        timeout_seconds=3.0,
    ),
    "blockers": DashboardSnapshotPolicy(
        panel_key="blockers",
        ttl_seconds=3.0,
        stale_after_seconds=5.0,
        hard_expire_seconds=120.0,
        timeout_seconds=3.0,
    ),
    "aiRuntime": DashboardSnapshotPolicy(
        panel_key="aiRuntime",
        ttl_seconds=10.0,
        stale_after_seconds=20.0,
        hard_expire_seconds=180.0,
        timeout_seconds=3.0,
    ),
    "metrics": DashboardSnapshotPolicy(
        panel_key="metrics",
        ttl_seconds=5.0,
        stale_after_seconds=10.0,
        hard_expire_seconds=120.0,
        timeout_seconds=3.0,
    ),
    "accountState": DashboardSnapshotPolicy(
        panel_key="accountState",
        ttl_seconds=5.0,
        stale_after_seconds=10.0,
        hard_expire_seconds=120.0,
        timeout_seconds=2.0,
    ),
}

P0_DASHBOARD_SNAPSHOT_PANEL_KEYS = frozenset(P0_DASHBOARD_SNAPSHOT_POLICIES)

P1_DASHBOARD_SNAPSHOT_POLICIES: dict[str, DashboardSnapshotPolicy] = {
    "latestDecision": DashboardSnapshotPolicy(
        panel_key="latestDecision",
        ttl_seconds=5.0,
        stale_after_seconds=15.0,
        hard_expire_seconds=180.0,
        timeout_seconds=5.0,
        priority="p1",
    ),
    "strategyRuntime": DashboardSnapshotPolicy(
        panel_key="strategyRuntime",
        ttl_seconds=10.0,
        stale_after_seconds=30.0,
        hard_expire_seconds=240.0,
        timeout_seconds=5.0,
        priority="p1",
    ),
    "executionLatest": DashboardSnapshotPolicy(
        panel_key="executionLatest",
        ttl_seconds=5.0,
        stale_after_seconds=15.0,
        hard_expire_seconds=180.0,
        timeout_seconds=3.0,
        priority="p1",
    ),
    "portfolio": DashboardSnapshotPolicy(
        panel_key="portfolio",
        ttl_seconds=5.0,
        stale_after_seconds=10.0,
        hard_expire_seconds=180.0,
        timeout_seconds=2.0,
        priority="p1",
    ),
    "positions": DashboardSnapshotPolicy(
        panel_key="positions",
        ttl_seconds=5.0,
        stale_after_seconds=10.0,
        hard_expire_seconds=180.0,
        timeout_seconds=2.0,
        priority="p1",
    ),
    "reconciliationLatest": DashboardSnapshotPolicy(
        panel_key="reconciliationLatest",
        ttl_seconds=10.0,
        stale_after_seconds=30.0,
        hard_expire_seconds=240.0,
        timeout_seconds=3.0,
        priority="p1",
    ),
}

P1_DASHBOARD_SNAPSHOT_PANEL_KEYS = frozenset(P1_DASHBOARD_SNAPSHOT_POLICIES)

P2_DASHBOARD_SNAPSHOT_POLICIES: dict[str, DashboardSnapshotPolicy] = {
    "trialGuard": DashboardSnapshotPolicy(
        panel_key="trialGuard",
        ttl_seconds=10.0,
        stale_after_seconds=30.0,
        hard_expire_seconds=240.0,
        timeout_seconds=5.0,
        priority="p2",
    ),
    "guardedLivePreflight": DashboardSnapshotPolicy(
        panel_key="guardedLivePreflight",
        ttl_seconds=10.0,
        stale_after_seconds=30.0,
        hard_expire_seconds=240.0,
        timeout_seconds=8.0,
        priority="p2",
    ),
    "guardedLiveRunPacket": DashboardSnapshotPolicy(
        panel_key="guardedLiveRunPacket",
        ttl_seconds=30.0,
        stale_after_seconds=60.0,
        hard_expire_seconds=300.0,
        timeout_seconds=20.0,
        priority="p2",
    ),
    "replayStatus": DashboardSnapshotPolicy(
        panel_key="replayStatus",
        ttl_seconds=15.0,
        stale_after_seconds=60.0,
        hard_expire_seconds=300.0,
        timeout_seconds=8.0,
        priority="p2",
    ),
    "aiOverview": DashboardSnapshotPolicy(
        panel_key="aiOverview",
        ttl_seconds=30.0,
        stale_after_seconds=60.0,
        hard_expire_seconds=300.0,
        timeout_seconds=10.0,
        priority="p2",
    ),
    "aiLatest": DashboardSnapshotPolicy(
        panel_key="aiLatest",
        ttl_seconds=30.0,
        stale_after_seconds=60.0,
        hard_expire_seconds=300.0,
        timeout_seconds=10.0,
        priority="p2",
    ),
    "aiShadowLatest": DashboardSnapshotPolicy(
        panel_key="aiShadowLatest",
        ttl_seconds=30.0,
        stale_after_seconds=60.0,
        hard_expire_seconds=300.0,
        timeout_seconds=10.0,
        priority="p2",
    ),
    "profileControlSummary": DashboardSnapshotPolicy(
        panel_key="profileControlSummary",
        ttl_seconds=60.0,
        stale_after_seconds=120.0,
        hard_expire_seconds=360.0,
        timeout_seconds=20.0,
        priority="p2",
    ),
    "aiConfigModel": DashboardSnapshotPolicy(
        panel_key="aiConfigModel",
        ttl_seconds=60.0,
        stale_after_seconds=120.0,
        hard_expire_seconds=360.0,
        timeout_seconds=15.0,
        priority="p2",
    ),
    "rdpControl": DashboardSnapshotPolicy(
        panel_key="rdpControl",
        ttl_seconds=30.0,
        stale_after_seconds=120.0,
        hard_expire_seconds=360.0,
        timeout_seconds=15.0,
        priority="p2",
    ),
    "rdpWorkbenchOverview": DashboardSnapshotPolicy(
        panel_key="rdpWorkbenchOverview",
        ttl_seconds=30.0,
        stale_after_seconds=120.0,
        hard_expire_seconds=360.0,
        timeout_seconds=15.0,
        priority="p2",
    ),
    "rdpWorkbenchItems": DashboardSnapshotPolicy(
        panel_key="rdpWorkbenchItems",
        ttl_seconds=60.0,
        stale_after_seconds=180.0,
        hard_expire_seconds=420.0,
        timeout_seconds=20.0,
        priority="p2",
    ),
    "rdpWorkbenchAlerts": DashboardSnapshotPolicy(
        panel_key="rdpWorkbenchAlerts",
        ttl_seconds=60.0,
        stale_after_seconds=180.0,
        hard_expire_seconds=420.0,
        timeout_seconds=20.0,
        priority="p2",
    ),
    "rdpTuningOverview": DashboardSnapshotPolicy(
        panel_key="rdpTuningOverview",
        ttl_seconds=60.0,
        stale_after_seconds=180.0,
        hard_expire_seconds=420.0,
        timeout_seconds=20.0,
        priority="p2",
    ),
    "rdpTuningProposals": DashboardSnapshotPolicy(
        panel_key="rdpTuningProposals",
        ttl_seconds=60.0,
        stale_after_seconds=180.0,
        hard_expire_seconds=420.0,
        timeout_seconds=20.0,
        priority="p2",
    ),
    "recentDecisions": DashboardSnapshotPolicy(
        panel_key="recentDecisions",
        ttl_seconds=30.0,
        stale_after_seconds=90.0,
        hard_expire_seconds=420.0,
        timeout_seconds=25.0,
        priority="p2",
    ),
}

P2_DASHBOARD_SNAPSHOT_PANEL_KEYS = frozenset(P2_DASHBOARD_SNAPSHOT_POLICIES)

P3_DASHBOARD_SNAPSHOT_POLICIES: dict[str, DashboardSnapshotPolicy] = {
    "strategyAttribution": DashboardSnapshotPolicy(
        panel_key="strategyAttribution",
        ttl_seconds=120.0,
        stale_after_seconds=300.0,
        hard_expire_seconds=900.0,
        timeout_seconds=35.0,
        priority="p3",
    ),
    "positionLifecycleAttribution": DashboardSnapshotPolicy(
        panel_key="positionLifecycleAttribution",
        ttl_seconds=120.0,
        stale_after_seconds=300.0,
        hard_expire_seconds=900.0,
        timeout_seconds=35.0,
        priority="p3",
    ),
    "trialReviewSummary": DashboardSnapshotPolicy(
        panel_key="trialReviewSummary",
        ttl_seconds=120.0,
        stale_after_seconds=300.0,
        hard_expire_seconds=900.0,
        timeout_seconds=35.0,
        priority="p3",
    ),
}

P3_DASHBOARD_SNAPSHOT_PANEL_KEYS = frozenset(P3_DASHBOARD_SNAPSHOT_POLICIES)

DASHBOARD_SNAPSHOT_POLICIES: dict[str, DashboardSnapshotPolicy] = {
    **P0_DASHBOARD_SNAPSHOT_POLICIES,
    **P1_DASHBOARD_SNAPSHOT_POLICIES,
    **P2_DASHBOARD_SNAPSHOT_POLICIES,
    **P3_DASHBOARD_SNAPSHOT_POLICIES,
}

DASHBOARD_SNAPSHOT_PANEL_KEYS = frozenset(DASHBOARD_SNAPSHOT_POLICIES)


class DashboardSnapshotPlane:
    """Background-produced dashboard panel snapshots for the API gateway.

    The critical contract is that request-time bundle assembly only reads
    snapshots and enqueues refresh work. It must not call the configured
    loader from ``read_panel``.
    """

    def __init__(
        self,
        *,
        loader: DashboardSnapshotLoader,
        default_factory: DashboardSnapshotDefaultFactory,
        policies: Mapping[str, DashboardSnapshotPolicy] | None = None,
        default_variants: Mapping[str, Sequence[str]] | None = None,
        scheduler_interval_seconds: float = 1.0,
        priority_concurrency: Mapping[str, int] | None = None,
        startup_panel_interval_seconds: float = 0.5,
        startup_priority_pause_seconds: float = 1.0,
    ) -> None:
        self._loader = loader
        self._default_factory = default_factory
        self._policies = dict(policies or DASHBOARD_SNAPSHOT_POLICIES)
        self._default_variants = {
            panel_key: tuple(str(item).strip() for item in variants if str(item).strip())
            for panel_key, variants in dict(default_variants or {}).items()
            if panel_key in self._policies
        }
        self._scheduler_interval_seconds = max(float(scheduler_interval_seconds), 0.2)
        concurrency = {
            "p0": 4,
            "p1": 2,
            "p2": 1,
            "p3": 1,
            **(dict(priority_concurrency or {})),
        }
        self._priority_semaphores = {
            priority: asyncio.Semaphore(max(int(limit), 1))
            for priority, limit in concurrency.items()
        }
        self._startup_panel_interval_seconds = max(float(startup_panel_interval_seconds), 0.0)
        self._startup_priority_pause_seconds = max(float(startup_priority_pause_seconds), 0.0)
        self._snapshots: dict[str, DashboardPanelSnapshot] = {}
        self._last_errors: dict[str, tuple[str, Any]] = {}
        self._inflight: dict[str, asyncio.Task[None]] = {}
        self._startup_pending_snapshot_keys: set[str] = set()
        self._lock = asyncio.Lock()
        self._scheduler_task: asyncio.Task[None] | None = None
        self._startup_task: asyncio.Task[None] | None = None
        self._stopped = False

    @property
    def panel_keys(self) -> frozenset[str]:
        return frozenset(self._policies)

    async def start(self) -> None:
        if self._scheduler_task is not None and not self._scheduler_task.done():
            return
        self._stopped = False
        startup_targets = self._startup_targets()
        async with self._lock:
            self._startup_pending_snapshot_keys = {target.snapshot_key for target in startup_targets}
        self._startup_task = asyncio.create_task(
            self._startup_prewarm_loop(startup_targets),
            name="dashboard-snapshot-startup-prewarm",
        )
        self._scheduler_task = asyncio.create_task(self._scheduler_loop(), name="dashboard-snapshot-scheduler")

    async def stop(self) -> None:
        self._stopped = True
        if self._startup_task is not None:
            self._startup_task.cancel()
            await asyncio.gather(self._startup_task, return_exceptions=True)
            self._startup_task = None
        if self._scheduler_task is not None:
            self._scheduler_task.cancel()
            await asyncio.gather(self._scheduler_task, return_exceptions=True)
            self._scheduler_task = None
        async with self._lock:
            tasks = list(self._inflight.values())
            self._inflight.clear()
            self._startup_pending_snapshot_keys.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def invalidate_all_and_refresh(self, *, reason: str = "mutation") -> None:
        now = utc_now()
        async with self._lock:
            for snapshot in self._snapshots.values():
                policy = self._policies.get(snapshot.panel_key)
                if policy is None:
                    continue
                snapshot.generated_at = now - timedelta(seconds=policy.stale_after_seconds + 0.001)
        await self.enqueue_all(reason=reason)

    async def seed_panel(
        self,
        panel_key: str,
        data: Any,
        *,
        variant_key: str | None = None,
        generated_at: Any | None = None,
        duration_ms: float = 0.0,
    ) -> None:
        if panel_key not in self._policies:
            raise KeyError(f"dashboard_snapshot_panel_not_registered:{panel_key}")
        timestamp = generated_at or utc_now()
        snapshot_key = dashboard_snapshot_storage_key(panel_key, variant_key)
        async with self._lock:
            self._snapshots[snapshot_key] = DashboardPanelSnapshot(
                panel_key=panel_key,
                snapshot_key=snapshot_key,
                variant_key=variant_key,
                data=data,
                generated_at=timestamp,
                last_success_at=timestamp,
                duration_ms=round(float(duration_ms), 3),
            )
            self._last_errors.pop(snapshot_key, None)

    async def enqueue_all(self, *, reason: str) -> None:
        for target in self._iter_snapshot_targets():
            await self.enqueue(target.panel_key, variant_key=target.variant_key, reason=reason)

    async def enqueue(self, panel_key: str, *, reason: str, variant_key: str | None = None) -> bool:
        if self._stopped or panel_key not in self._policies:
            return False
        snapshot_key = dashboard_snapshot_storage_key(panel_key, variant_key)
        async with self._lock:
            task = self._inflight.get(snapshot_key)
            if task is not None and not task.done():
                return False
            task = asyncio.create_task(
                self._refresh_panel(panel_key, variant_key=variant_key, reason=reason),
                name=f"dashboard-snapshot-{snapshot_key}",
            )
            self._inflight[snapshot_key] = task
            task.add_done_callback(lambda _task, key=snapshot_key: self._forget_inflight(key, _task))
            return True

    async def read_panel(self, panel_key: str, *, variant_key: str | None = None) -> DashboardSnapshotRead:
        started_at = perf_counter()
        policy = self._policies.get(panel_key)
        if policy is None:
            raise KeyError(f"dashboard_snapshot_panel_not_registered:{panel_key}")
        snapshot_key = dashboard_snapshot_storage_key(panel_key, variant_key)
        snapshot = await self._snapshot(snapshot_key)
        age_seconds = self._snapshot_age_seconds(snapshot)
        hard_expired = snapshot is None or age_seconds is None or age_seconds > policy.hard_expire_seconds
        stale = snapshot is None or age_seconds is None or age_seconds > policy.stale_after_seconds
        refreshing = await self._is_refreshing(snapshot_key)
        startup_pending = await self._is_startup_pending(snapshot_key)

        if stale or hard_expired:
            if startup_pending:
                refreshing = True
            else:
                enqueued = await self.enqueue(
                    panel_key,
                    variant_key=variant_key,
                    reason="read_stale" if snapshot is not None else "read_missing",
                )
                refreshing = refreshing or enqueued

        if snapshot is None or hard_expired:
            data = self._default_factory(snapshot_key)
            last_error, last_error_at = await self._last_error(snapshot_key)
            soft_timeout_pending = _is_pending_refresh_timeout(last_error, refreshing=refreshing)
            error = None if last_error is None or soft_timeout_pending else "dashboard_snapshot_refresh_failed"
            return DashboardSnapshotRead(
                data=data,
                error=error,
                meta=self._meta(
                    policy=policy,
                    snapshot=None,
                    snapshot_key=snapshot_key,
                    variant_key=variant_key,
                    stale=True,
                    loading=True,
                    refreshing=refreshing,
                    age_seconds=None,
                    last_error=last_error,
                    last_error_at=last_error_at,
                    status="missing" if error is None else "error",
                ),
                duration_ms=round((perf_counter() - started_at) * 1000.0, 3),
            )

        return DashboardSnapshotRead(
            data=snapshot.data,
            error=None,
            meta=self._meta(
                policy=policy,
                snapshot=snapshot,
                snapshot_key=snapshot_key,
                variant_key=variant_key,
                stale=stale,
                loading=False,
                refreshing=refreshing,
                age_seconds=age_seconds,
                last_error=snapshot.last_error,
                last_error_at=snapshot.last_error_at,
                status="stale" if stale else "fresh",
            ),
            duration_ms=round((perf_counter() - started_at) * 1000.0, 3),
        )

    async def _scheduler_loop(self) -> None:
        while not self._stopped:
            try:
                await self._enqueue_due_panels()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_event(
                    _LOGGER,
                    "dashboard_snapshot_scheduler_failed",
                    level="warning",
                    error=type(exc).__name__,
                    message=str(exc),
                )
            await asyncio.sleep(self._scheduler_interval_seconds)

    async def _enqueue_due_panels(self) -> None:
        now = utc_now()
        for target in self._iter_snapshot_targets():
            policy = self._policies[target.panel_key]
            snapshot = await self._snapshot(target.snapshot_key)
            if snapshot is None:
                if await self._is_startup_pending(target.snapshot_key):
                    continue
                await self.enqueue(target.panel_key, variant_key=target.variant_key, reason="scheduler_missing")
                continue
            age_seconds = max((now - snapshot.generated_at).total_seconds(), 0.0)
            if age_seconds >= policy.ttl_seconds:
                await self.enqueue(target.panel_key, variant_key=target.variant_key, reason="scheduler_ttl")

    async def _startup_prewarm_loop(self, targets: Sequence[_DashboardSnapshotTarget]) -> None:
        if not targets:
            return
        log_event(
            _LOGGER,
            "dashboard_snapshot_startup_prewarm_enqueue_start",
            target_count=len(targets),
            panel_interval_seconds=self._startup_panel_interval_seconds,
            priority_pause_seconds=self._startup_priority_pause_seconds,
        )
        try:
            current_priority: str | None = None
            for target in targets:
                if self._stopped:
                    return
                if current_priority is not None and target.priority != current_priority:
                    await self._sleep_startup_interval(self._startup_priority_pause_seconds)
                    if self._stopped:
                        return
                current_priority = target.priority
                await self._mark_startup_target_reached(target.snapshot_key)
                await self.enqueue(target.panel_key, variant_key=target.variant_key, reason="startup")
                await self._sleep_startup_interval(self._startup_panel_interval_seconds)
            log_event(_LOGGER, "dashboard_snapshot_startup_prewarm_enqueue_complete", target_count=len(targets))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_event(
                _LOGGER,
                "dashboard_snapshot_startup_prewarm_enqueue_failed",
                level="warning",
                error=type(exc).__name__,
                message=str(exc),
            )
        finally:
            if not self._stopped:
                async with self._lock:
                    self._startup_pending_snapshot_keys.clear()

    @staticmethod
    async def _sleep_startup_interval(seconds: float) -> None:
        if seconds > 0:
            await asyncio.sleep(seconds)

    async def _mark_startup_target_reached(self, snapshot_key: str) -> None:
        async with self._lock:
            self._startup_pending_snapshot_keys.discard(snapshot_key)

    async def _is_startup_pending(self, snapshot_key: str) -> bool:
        async with self._lock:
            return snapshot_key in self._startup_pending_snapshot_keys

    def _iter_snapshot_targets(self) -> tuple[_DashboardSnapshotTarget, ...]:
        targets: list[_DashboardSnapshotTarget] = []
        for panel_key, policy in self._policies.items():
            variants = self._default_variants.get(panel_key)
            for variant_key in (variants if variants else (None,)):
                targets.append(
                    _DashboardSnapshotTarget(
                        panel_key=panel_key,
                        snapshot_key=dashboard_snapshot_storage_key(panel_key, variant_key),
                        variant_key=variant_key,
                        priority=policy.priority,
                    )
                )
        return tuple(targets)

    def _startup_targets(self) -> tuple[_DashboardSnapshotTarget, ...]:
        priority_order = {"p0": 0, "p1": 1, "p2": 2, "p3": 3}
        indexed_targets = tuple(enumerate(self._iter_snapshot_targets()))
        return tuple(
            target
            for _, target in sorted(
                indexed_targets,
                key=lambda item: (
                    priority_order.get(item[1].priority, len(priority_order)),
                    item[0],
                ),
            )
        )

    async def _refresh_panel(self, panel_key: str, *, reason: str, variant_key: str | None = None) -> None:
        policy = self._policies[panel_key]
        semaphore = self._priority_semaphores.setdefault(policy.priority, asyncio.Semaphore(1))
        async with semaphore:
            await self._refresh_panel_locked(panel_key, variant_key=variant_key, policy=policy, reason=reason)

    async def _refresh_panel_locked(
        self,
        panel_key: str,
        *,
        variant_key: str | None,
        policy: DashboardSnapshotPolicy,
        reason: str,
    ) -> None:
        started_at = perf_counter()
        snapshot_key = dashboard_snapshot_storage_key(panel_key, variant_key)
        log_event(
            _LOGGER,
            "dashboard_snapshot_refresh_start",
            panel_key=panel_key,
            snapshot_key=snapshot_key,
            variant_key=variant_key,
            priority=policy.priority,
            reason=reason,
        )
        loader_task = asyncio.create_task(self._call_loader(snapshot_key), name=f"dashboard-snapshot-loader-{snapshot_key}")
        done, _pending = await asyncio.wait({loader_task}, timeout=policy.timeout_seconds)
        if not done:
            await self._record_error(
                snapshot_key,
                error_code="dashboard_snapshot_refresh_timeout",
                message=f"snapshot refresh exceeded {policy.timeout_seconds:.3f}s",
            )
            log_event(
                _LOGGER,
                "dashboard_snapshot_refresh_timeout",
                level="warning",
                panel_key=panel_key,
                snapshot_key=snapshot_key,
                variant_key=variant_key,
                timeout_seconds=policy.timeout_seconds,
            )
            # ``asyncio.to_thread`` cannot stop the underlying sync DB work when
            # its coroutine is cancelled. Keep this refresh inflight until the
            # loader settles so the scheduler/read path cannot start duplicate
            # full-panel reads that saturate the DB and default thread pool.
        try:
            payload = await loader_task
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._record_error(
                snapshot_key,
                error_code=type(exc).__name__,
                message=str(exc),
            )
            log_event(
                _LOGGER,
                "dashboard_snapshot_refresh_failed",
                level="warning",
                panel_key=panel_key,
                snapshot_key=snapshot_key,
                variant_key=variant_key,
                error=type(exc).__name__,
                message=str(exc),
            )
            return

        duration_ms = round((perf_counter() - started_at) * 1000.0, 3)
        generated_at = utc_now()
        async with self._lock:
            self._snapshots[snapshot_key] = DashboardPanelSnapshot(
                panel_key=panel_key,
                snapshot_key=snapshot_key,
                variant_key=variant_key,
                data=payload,
                generated_at=generated_at,
                last_success_at=generated_at,
                duration_ms=duration_ms,
            )
            self._last_errors.pop(snapshot_key, None)
        log_event(
            _LOGGER,
            "dashboard_snapshot_refresh_success",
            panel_key=panel_key,
            snapshot_key=snapshot_key,
            variant_key=variant_key,
            priority=policy.priority,
            duration_ms=duration_ms,
            late=duration_ms > policy.timeout_seconds * 1000.0,
        )

    async def _call_loader(self, snapshot_key: str) -> Any:
        payload = self._loader(snapshot_key)
        if inspect.isawaitable(payload):
            return await payload
        return payload

    async def _snapshot(self, snapshot_key: str) -> DashboardPanelSnapshot | None:
        async with self._lock:
            return self._snapshots.get(snapshot_key)

    async def _last_error(self, snapshot_key: str) -> tuple[str | None, Any | None]:
        async with self._lock:
            return self._last_errors.get(snapshot_key, (None, None))

    async def _record_error(self, snapshot_key: str, *, error_code: str, message: str) -> None:
        text = f"{error_code}:{message}" if message else error_code
        now = utc_now()
        async with self._lock:
            self._last_errors[snapshot_key] = (text, now)
            snapshot = self._snapshots.get(snapshot_key)
            if snapshot is not None:
                snapshot.last_error = text
                snapshot.last_error_at = now

    async def _is_refreshing(self, snapshot_key: str) -> bool:
        async with self._lock:
            task = self._inflight.get(snapshot_key)
            return task is not None and not task.done()

    def _forget_inflight(self, snapshot_key: str, task: asyncio.Task[None]) -> None:
        try:
            current = self._inflight.get(snapshot_key)
            if current is task:
                self._inflight.pop(snapshot_key, None)
        except Exception:
            pass
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            panel_key, variant_key = dashboard_snapshot_storage_parts(snapshot_key)
            log_event(
                _LOGGER,
                "dashboard_snapshot_task_failed",
                level="warning",
                panel_key=panel_key,
                snapshot_key=snapshot_key,
                variant_key=variant_key,
                error=type(exc).__name__,
                message=str(exc),
            )

    @staticmethod
    def _snapshot_age_seconds(snapshot: DashboardPanelSnapshot | None) -> float | None:
        if snapshot is None:
            return None
        return max((utc_now() - snapshot.generated_at).total_seconds(), 0.0)

    @staticmethod
    def _iso(value: Any | None) -> str | None:
        if value is None:
            return None
        iso = getattr(value, "isoformat", None)
        return iso() if callable(iso) else str(value)

    def _meta(
        self,
        *,
        policy: DashboardSnapshotPolicy,
        snapshot: DashboardPanelSnapshot | None,
        snapshot_key: str,
        variant_key: str | None,
        stale: bool,
        loading: bool,
        refreshing: bool,
        age_seconds: float | None,
        last_error: str | None,
        last_error_at: Any | None,
        status: str,
    ) -> dict[str, Any]:
        return {
            "source": "dashboard_snapshot",
            "status": status,
            "priority": policy.priority,
            "snapshot_key": snapshot_key,
            "variant_key": variant_key,
            "materialized": True,
            "snapshot_generated_at": self._iso(snapshot.generated_at if snapshot is not None else None),
            "snapshot_age_ms": None if age_seconds is None else round(age_seconds * 1000.0, 3),
            "stale": stale,
            "loading": loading,
            "refreshing": refreshing,
            "last_success_at": self._iso(snapshot.last_success_at if snapshot is not None else None),
            "last_error": last_error,
            "last_error_at": self._iso(last_error_at),
            "ttl_seconds": policy.ttl_seconds,
            "stale_after_seconds": policy.stale_after_seconds,
            "hard_expire_seconds": policy.hard_expire_seconds,
            "refresh_duration_ms": snapshot.duration_ms if snapshot is not None else None,
        }
