from __future__ import annotations

import unittest
from types import SimpleNamespace

from apps.api_gateway import main


class _FakeSnapshotPlane:
    def __init__(self) -> None:
        self.invalidations: list[str] = []
        self.scheduled_refreshes: list[str] = []
        self.eager_refreshes: list[tuple[str, tuple[str, ...]]] = []

    async def invalidate_all(self, *, reason: str) -> None:
        self.invalidations.append(reason)

    async def enqueue_scheduled(self, *, reason: str) -> None:
        self.scheduled_refreshes.append(reason)

    async def enqueue_panels(self, panel_keys, *, reason: str) -> None:
        self.eager_refreshes.append((reason, tuple(panel_keys)))


class ApiGatewayDashboardInvalidationTest(unittest.IsolatedAsyncioTestCase):
    def test_auth_paths_clear_bundle_cache_without_snapshot_refresh(self) -> None:
        self.assertTrue(main._is_successful_mutation("POST", 200))
        self.assertFalse(main._should_refresh_dashboard_snapshots_after_mutation("POST", "/auth/login", 200))
        self.assertFalse(main._should_refresh_dashboard_snapshots_after_mutation("POST", "/auth/logout", 200))
        self.assertFalse(main._should_refresh_dashboard_snapshots_after_mutation("PATCH", "/auth/users/admin", 200))

    def test_dashboard_data_mutations_refresh_snapshots_only_after_success(self) -> None:
        self.assertTrue(
            main._should_refresh_dashboard_snapshots_after_mutation(
                "POST",
                "/strategy-profiles/profiles/trend/activate",
                200,
            )
        )
        self.assertTrue(main._should_refresh_dashboard_snapshots_after_mutation("POST", "/rdp/tasks/trigger", 202))
        self.assertFalse(main._should_refresh_dashboard_snapshots_after_mutation("GET", "/rdp/tasks/trigger", 200))
        self.assertFalse(main._should_refresh_dashboard_snapshots_after_mutation("POST", "/rdp/tasks/trigger", 500))

    def test_mutation_path_selects_only_directly_affected_non_scheduled_panels(self) -> None:
        self.assertEqual(
            main._eager_dashboard_snapshot_panels_for_mutation("/ai/operating-mode/select"),
            ("aiConfigModel",),
        )
        self.assertEqual(
            main._eager_dashboard_snapshot_panels_for_mutation("/rdp/tasks/trigger"),
            ("rdpWorkspace",),
        )
        self.assertEqual(
            main._eager_dashboard_snapshot_panels_for_mutation("/strategy-profiles/profiles/trend/activate"),
            ("profileControlSummary",),
        )
        self.assertEqual(main._eager_dashboard_snapshot_panels_for_mutation("/system/halt"), ())

    async def test_snapshot_enqueue_after_mutation_is_cooled_down_but_invalidation_is_not(self) -> None:
        plane = _FakeSnapshotPlane()
        state = SimpleNamespace(dashboard_snapshot_plane=plane)
        request = SimpleNamespace(
            app=SimpleNamespace(state=state),
            url=SimpleNamespace(path="/system/halt"),
        )

        refreshed = await main._refresh_dashboard_snapshots_after_mutation(request, reason="post_mutation")
        skipped = await main._refresh_dashboard_snapshots_after_mutation(request, reason="post_mutation_again")

        self.assertTrue(refreshed)
        self.assertFalse(skipped)
        self.assertEqual(plane.invalidations, ["post_mutation", "post_mutation_again"])
        self.assertEqual(plane.scheduled_refreshes, ["post_mutation"])
        self.assertEqual(plane.eager_refreshes, [("post_mutation", ())])

        setattr(
            state,
            main._DASHBOARD_SNAPSHOT_REFRESH_LAST_ATTR,
            main.monotonic() - main._DASHBOARD_SNAPSHOT_MUTATION_REFRESH_COOLDOWN_SECONDS - 0.1,
        )
        refreshed_after_cooldown = await main._refresh_dashboard_snapshots_after_mutation(
            request,
            reason="post_mutation_after_cooldown",
        )

        self.assertTrue(refreshed_after_cooldown)
        self.assertEqual(
            plane.invalidations,
            ["post_mutation", "post_mutation_again", "post_mutation_after_cooldown"],
        )
        self.assertEqual(plane.scheduled_refreshes, ["post_mutation", "post_mutation_after_cooldown"])

    async def test_rdp_mutation_enqueues_selected_non_scheduled_panels(self) -> None:
        plane = _FakeSnapshotPlane()
        state = SimpleNamespace(dashboard_snapshot_plane=plane)
        request = SimpleNamespace(
            app=SimpleNamespace(state=state),
            url=SimpleNamespace(path="/rdp/tasks/trigger"),
        )

        refreshed = await main._refresh_dashboard_snapshots_after_mutation(request, reason="post_mutation")

        self.assertTrue(refreshed)
        self.assertEqual(plane.invalidations, ["post_mutation"])
        self.assertEqual(plane.scheduled_refreshes, ["post_mutation"])
        self.assertEqual(plane.eager_refreshes, [("post_mutation", ("rdpWorkspace",))])


if __name__ == "__main__":
    unittest.main()
