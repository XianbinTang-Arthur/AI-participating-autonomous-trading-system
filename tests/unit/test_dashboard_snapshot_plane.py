from __future__ import annotations

import asyncio
import unittest
from datetime import timedelta
from typing import Any

from aats.schemas.common import utc_now
from aats.services.operator.dashboard_snapshot import (
    DASHBOARD_SNAPSHOT_PANEL_KEYS,
    DashboardSnapshotPlane,
    DashboardSnapshotPolicy,
    P0_DASHBOARD_SNAPSHOT_PANEL_KEYS,
    P1_DASHBOARD_SNAPSHOT_PANEL_KEYS,
    P2_DASHBOARD_SNAPSHOT_PANEL_KEYS,
    dashboard_snapshot_storage_key,
    dashboard_snapshot_storage_parts,
)


def _policy(
    *,
    ttl_seconds: float = 60.0,
    stale_after_seconds: float = 60.0,
    hard_expire_seconds: float = 120.0,
    timeout_seconds: float = 1.0,
) -> DashboardSnapshotPolicy:
    return DashboardSnapshotPolicy(
        panel_key="runtime",
        ttl_seconds=ttl_seconds,
        stale_after_seconds=stale_after_seconds,
        hard_expire_seconds=hard_expire_seconds,
        timeout_seconds=timeout_seconds,
    )


async def _wait_for_snapshot(plane: DashboardSnapshotPlane, expected: dict[str, Any]) -> None:
    for _ in range(100):
        read = await plane.read_panel("runtime")
        if read.data == expected:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"snapshot never reached {expected!r}")


async def _wait_for_panel_snapshot(
    plane: DashboardSnapshotPlane,
    panel_key: str,
    expected: dict[str, Any],
    *,
    variant_key: str | None = None,
) -> None:
    for _ in range(100):
        read = await plane.read_panel(panel_key, variant_key=variant_key)
        if read.data == expected:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{panel_key}:{variant_key} snapshot never reached {expected!r}")


class DashboardSnapshotPlaneTest(unittest.IsolatedAsyncioTestCase):
    def test_default_registry_contains_p0_and_p1_panel_keys(self) -> None:
        self.assertTrue(P0_DASHBOARD_SNAPSHOT_PANEL_KEYS.issubset(DASHBOARD_SNAPSHOT_PANEL_KEYS))
        self.assertTrue(P1_DASHBOARD_SNAPSHOT_PANEL_KEYS.issubset(DASHBOARD_SNAPSHOT_PANEL_KEYS))
        self.assertTrue(P2_DASHBOARD_SNAPSHOT_PANEL_KEYS.issubset(DASHBOARD_SNAPSHOT_PANEL_KEYS))
        self.assertIn("latestDecision", DASHBOARD_SNAPSHOT_PANEL_KEYS)
        self.assertIn("strategyRuntime", DASHBOARD_SNAPSHOT_PANEL_KEYS)
        self.assertIn("guardedLiveRunPacket", DASHBOARD_SNAPSHOT_PANEL_KEYS)
        self.assertIn("rdpWorkbenchOverview", DASHBOARD_SNAPSHOT_PANEL_KEYS)

    async def test_missing_read_returns_default_and_enqueues_refresh(self) -> None:
        loader_called = asyncio.Event()

        async def loader(panel_key: str) -> dict[str, Any]:
            loader_called.set()
            return {"panel": panel_key, "ready": True}

        plane = DashboardSnapshotPlane(
            loader=loader,
            default_factory=lambda panel_key: {"panel": panel_key, "ready": False},
            policies={"runtime": _policy()},
            scheduler_interval_seconds=60.0,
        )
        try:
            read = await plane.read_panel("runtime")

            self.assertEqual(read.data, {"panel": "runtime", "ready": False})
            self.assertIsNone(read.error)
            self.assertEqual(read.meta["source"], "dashboard_snapshot")
            self.assertEqual(read.meta["status"], "missing")
            self.assertTrue(read.meta["loading"])
            self.assertTrue(read.meta["refreshing"])

            await asyncio.wait_for(loader_called.wait(), timeout=1.0)
            await _wait_for_snapshot(plane, {"panel": "runtime", "ready": True})
        finally:
            await plane.stop()

    async def test_enqueue_is_singleflight_per_panel(self) -> None:
        loader_entered = asyncio.Event()
        release_loader = asyncio.Event()

        async def loader(_panel_key: str) -> dict[str, Any]:
            loader_entered.set()
            await release_loader.wait()
            return {"ready": True}

        plane = DashboardSnapshotPlane(
            loader=loader,
            default_factory=lambda _panel_key: {},
            policies={"runtime": _policy()},
            scheduler_interval_seconds=60.0,
        )
        try:
            first = await plane.enqueue("runtime", reason="test_first")
            second = await plane.enqueue("runtime", reason="test_second")

            self.assertTrue(first)
            self.assertFalse(second)

            await asyncio.wait_for(loader_entered.wait(), timeout=1.0)
            release_loader.set()
            await _wait_for_snapshot(plane, {"ready": True})
        finally:
            await plane.stop()

    async def test_priority_concurrency_limits_refresh_workers(self) -> None:
        active = 0
        max_active = 0

        async def loader(panel_key: str) -> dict[str, Any]:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.02)
            active -= 1
            return {"panel": panel_key}

        plane = DashboardSnapshotPlane(
            loader=loader,
            default_factory=lambda _panel_key: {},
            policies={
                "runtime": DashboardSnapshotPolicy(
                    panel_key="runtime",
                    ttl_seconds=60.0,
                    stale_after_seconds=60.0,
                    hard_expire_seconds=120.0,
                    timeout_seconds=1.0,
                    priority="p1",
                ),
                "health": DashboardSnapshotPolicy(
                    panel_key="health",
                    ttl_seconds=60.0,
                    stale_after_seconds=60.0,
                    hard_expire_seconds=120.0,
                    timeout_seconds=1.0,
                    priority="p1",
                ),
            },
            scheduler_interval_seconds=60.0,
            priority_concurrency={"p1": 1},
        )
        try:
            self.assertTrue(await plane.enqueue("runtime", reason="test"))
            self.assertTrue(await plane.enqueue("health", reason="test"))

            await _wait_for_panel_snapshot(plane, "runtime", {"panel": "runtime"})
            await _wait_for_panel_snapshot(plane, "health", {"panel": "health"})
            self.assertEqual(max_active, 1)
        finally:
            await plane.stop()

    async def test_refresh_timeout_releases_priority_slot_for_queued_panel(self) -> None:
        slow_started = asyncio.Event()
        cancel_seen = asyncio.Event()
        release_slow = asyncio.Event()
        slow_finished = asyncio.Event()

        async def loader(panel_key: str) -> dict[str, Any]:
            if panel_key == "runtime":
                slow_started.set()
                try:
                    await asyncio.sleep(10.0)
                except asyncio.CancelledError:
                    cancel_seen.set()
                    await release_slow.wait()
                    slow_finished.set()
                return {"panel": panel_key, "unexpected": True}
            return {"panel": panel_key, "ready": True}

        plane = DashboardSnapshotPlane(
            loader=loader,
            default_factory=lambda panel_key: {"panel": panel_key, "ready": False},
            policies={
                "runtime": DashboardSnapshotPolicy(
                    panel_key="runtime",
                    ttl_seconds=60.0,
                    stale_after_seconds=60.0,
                    hard_expire_seconds=120.0,
                    timeout_seconds=0.02,
                    priority="p1",
                ),
                "health": DashboardSnapshotPolicy(
                    panel_key="health",
                    ttl_seconds=60.0,
                    stale_after_seconds=60.0,
                    hard_expire_seconds=120.0,
                    timeout_seconds=1.0,
                    priority="p1",
                ),
            },
            scheduler_interval_seconds=60.0,
            priority_concurrency={"p1": 1},
        )
        try:
            self.assertTrue(await plane.enqueue("runtime", reason="test_slow"))
            self.assertTrue(await plane.enqueue("health", reason="test_fast"))
            await asyncio.wait_for(slow_started.wait(), timeout=1.0)
            await asyncio.wait_for(cancel_seen.wait(), timeout=1.0)

            await asyncio.wait_for(
                _wait_for_panel_snapshot(plane, "health", {"panel": "health", "ready": True}),
                timeout=1.0,
            )
            read = await plane.read_panel("runtime")

            self.assertEqual(read.data, {"panel": "runtime", "ready": False})
            self.assertEqual(read.error, "dashboard_snapshot_refresh_failed")
            self.assertEqual(read.meta["status"], "error")
            self.assertIn("dashboard_snapshot_refresh_timeout", read.meta["last_error"])
        finally:
            release_slow.set()
            if cancel_seen.is_set() and not slow_finished.is_set():
                await asyncio.wait_for(slow_finished.wait(), timeout=1.0)
            await plane.stop()

    async def test_stale_snapshot_is_served_while_refresh_runs(self) -> None:
        loader_called = asyncio.Event()

        async def loader(_panel_key: str) -> dict[str, Any]:
            loader_called.set()
            return {"version": 2}

        plane = DashboardSnapshotPlane(
            loader=loader,
            default_factory=lambda _panel_key: {},
            policies={
                "runtime": _policy(
                    ttl_seconds=0.01,
                    stale_after_seconds=0.01,
                    hard_expire_seconds=60.0,
                )
            },
            scheduler_interval_seconds=60.0,
        )
        try:
            await plane.seed_panel(
                "runtime",
                {"version": 1},
                generated_at=utc_now() - timedelta(seconds=1),
                duration_ms=5.0,
            )

            read = await plane.read_panel("runtime")

            self.assertEqual(read.data, {"version": 1})
            self.assertIsNone(read.error)
            self.assertEqual(read.meta["status"], "stale")
            self.assertTrue(read.meta["stale"])
            self.assertFalse(read.meta["loading"])
            self.assertTrue(read.meta["refreshing"])

            await asyncio.wait_for(loader_called.wait(), timeout=1.0)
            await _wait_for_snapshot(plane, {"version": 2})
        finally:
            await plane.stop()

    async def test_invalidate_marks_snapshot_stale_without_dropping_data(self) -> None:
        loader_called = asyncio.Event()

        async def loader(_panel_key: str) -> dict[str, Any]:
            loader_called.set()
            return {"version": 2}

        plane = DashboardSnapshotPlane(
            loader=loader,
            default_factory=lambda _panel_key: {},
            policies={"runtime": _policy(stale_after_seconds=60.0)},
            scheduler_interval_seconds=60.0,
        )
        try:
            await plane.seed_panel("runtime", {"version": 1})

            await plane.invalidate_all_and_refresh(reason="test_mutation")
            read = await plane.read_panel("runtime")

            self.assertEqual(read.data, {"version": 1})
            self.assertIsNone(read.error)
            self.assertEqual(read.meta["status"], "stale")
            self.assertTrue(read.meta["stale"])
            self.assertFalse(read.meta["loading"])
            self.assertTrue(read.meta["refreshing"])

            await asyncio.wait_for(loader_called.wait(), timeout=1.0)
            await _wait_for_snapshot(plane, {"version": 2})
        finally:
            await plane.stop()

    async def test_variant_snapshots_are_isolated_by_storage_key(self) -> None:
        calls: list[str] = []

        async def loader(snapshot_key: str) -> dict[str, Any]:
            calls.append(snapshot_key)
            panel_key, variant_key = dashboard_snapshot_storage_parts(snapshot_key)
            return {"panel": panel_key, "variant": variant_key}

        plane = DashboardSnapshotPlane(
            loader=loader,
            default_factory=lambda snapshot_key: {"snapshot_key": snapshot_key, "ready": False},
            policies={"runtime": _policy()},
            scheduler_interval_seconds=60.0,
        )
        try:
            read_a = await plane.read_panel("runtime", variant_key="limit=8")
            read_b = await plane.read_panel("runtime", variant_key="limit=20")

            self.assertEqual(read_a.data, {"snapshot_key": "runtime::limit=8", "ready": False})
            self.assertEqual(read_b.data, {"snapshot_key": "runtime::limit=20", "ready": False})
            await _wait_for_panel_snapshot(
                plane,
                "runtime",
                {"panel": "runtime", "variant": "limit=8"},
                variant_key="limit=8",
            )
            await _wait_for_panel_snapshot(
                plane,
                "runtime",
                {"panel": "runtime", "variant": "limit=20"},
                variant_key="limit=20",
            )
            self.assertIn(dashboard_snapshot_storage_key("runtime", "limit=8"), calls)
            self.assertIn(dashboard_snapshot_storage_key("runtime", "limit=20"), calls)
        finally:
            await plane.stop()

    async def test_default_variants_are_preheated_by_enqueue_all(self) -> None:
        calls: list[str] = []

        async def loader(snapshot_key: str) -> dict[str, Any]:
            calls.append(snapshot_key)
            return {"snapshot_key": snapshot_key}

        plane = DashboardSnapshotPlane(
            loader=loader,
            default_factory=lambda _snapshot_key: {},
            policies={"runtime": _policy()},
            default_variants={"runtime": ("limit=8", "limit=20")},
            scheduler_interval_seconds=60.0,
        )
        try:
            await plane.enqueue_all(reason="test")
            await _wait_for_panel_snapshot(
                plane,
                "runtime",
                {"snapshot_key": "runtime::limit=8"},
                variant_key="limit=8",
            )
            read = await plane.read_panel("runtime", variant_key="limit=20")
            self.assertEqual(read.data, {"snapshot_key": "runtime::limit=20"})
            self.assertEqual(set(calls), {"runtime::limit=8", "runtime::limit=20"})
        finally:
            await plane.stop()


if __name__ == "__main__":
    unittest.main()
