from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_node_module_script(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


class TestDashboardRefreshInteractivity(unittest.TestCase):
    def test_sync_refresh_disabled_buttons_locks_and_restores_scope_buttons(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { syncRefreshDisabledButtons } from './aats/api/static/modules/refresh-interactivity.js';

function createButton({ disabled = false, title = null } = {}) {
  const attributes = new Map();
  if (title !== null) attributes.set('title', title);
  return {
    disabled,
    dataset: {},
    getAttribute(name) {
      return attributes.has(name) ? attributes.get(name) : null;
    },
    setAttribute(name, value) {
      attributes.set(name, String(value));
    },
    removeAttribute(name) {
      attributes.delete(name);
    },
  };
}

function createRoot(buttons) {
  return {
    querySelectorAll(selector) {
      return selector === 'button' ? buttons : [];
    },
  };
}

const pageButton = createButton({ title: '页面按钮' });
const drawerButton = createButton();
const alreadyDisabledButton = createButton({ disabled: true, title: '原本就不可点' });

syncRefreshDisabledButtons({
  roots: [createRoot([pageButton]), createRoot([drawerButton, alreadyDisabledButton])],
  refreshing: true,
  reason: '当前区域正在刷新，请等待刷新完成后再操作。',
});

const lockedState = {
  pageDisabled: pageButton.disabled === true,
  pageTitle: pageButton.getAttribute('title') === '当前区域正在刷新，请等待刷新完成后再操作。',
  drawerDisabled: drawerButton.disabled === true,
  drawerTitle: drawerButton.getAttribute('title') === '当前区域正在刷新，请等待刷新完成后再操作。',
  preservedDisabled: alreadyDisabledButton.disabled === true,
  preservedTitle: alreadyDisabledButton.getAttribute('title') === '原本就不可点',
};

syncRefreshDisabledButtons({
  roots: [createRoot([pageButton]), createRoot([drawerButton, alreadyDisabledButton])],
  refreshing: false,
});

console.log(JSON.stringify({
  ...lockedState,
  pageRestored: pageButton.disabled === false && pageButton.getAttribute('title') === '页面按钮',
  drawerRestored: drawerButton.disabled === false && drawerButton.getAttribute('title') === null,
  preservedStillDisabled: alreadyDisabledButton.disabled === true,
  preservedStillTitle: alreadyDisabledButton.getAttribute('title') === '原本就不可点',
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn('"pageDisabled":true', result.stdout)
        self.assertIn('"pageTitle":true', result.stdout)
        self.assertIn('"drawerDisabled":true', result.stdout)
        self.assertIn('"drawerTitle":true', result.stdout)
        self.assertIn('"preservedDisabled":true', result.stdout)
        self.assertIn('"preservedTitle":true', result.stdout)
        self.assertIn('"pageRestored":true', result.stdout)
        self.assertIn('"drawerRestored":true', result.stdout)
        self.assertIn('"preservedStillDisabled":true', result.stdout)
        self.assertIn('"preservedStillTitle":true', result.stdout)

    def test_refresh_dashboard_marks_primary_panels_pending_for_card_lock(self) -> None:
        """Regression: cards/drawers being refreshed must lock their action
        buttons.

        The user-visible bug was: clicking the refresh button (or any
        background auto-refresh tick) would visibly shimmer the cards but
        leave the action buttons inside them clickable. The fix is that
        refreshDashboard now marks BOTH the primary AND deferred panels as
        pending in state.pendingPanels at the start of the fetch — which is
        what refresh-interactivity.js consults to decide whether to apply
        the is-refresh-locked class to buttons inside [data-panel-key]
        cards.

        This test verifies the lifecycle:
          1. Before refresh: pendingPanels is empty.
          2. After refreshDashboard() is called but before primary resolves:
             primary panels (e.g. blockers, metrics, portfolio) AND deferred
             panels (e.g. latestDecision) are both pending.
          3. After primary resolves but before deferred resolves: primary
             panels are cleared, deferred panels are still pending.
          4. After deferred resolves: pendingPanels is empty again.
        """
        script = r"""
import { createDashboardRefreshController } from './aats/api/static/modules/dashboard-refresh.js';
import { createState } from './aats/api/static/modules/store.js';

// Stub the browser globals dashboard-refresh.js touches.
globalThis.window = {
  setTimeout: () => null,
  clearTimeout: () => null,
};
globalThis.document = { visibilityState: 'visible' };

const state = createState();
state.activeView = 'home';

// Manually-resolvable promises so we can drive primary/deferred separately.
let primaryResolve;
const primaryPromise = new Promise((resolve) => { primaryResolve = resolve; });
let deferredResolve;
const deferredPromise = new Promise((resolve) => { deferredResolve = resolve; });

let fetchCallCount = 0;
const fetchDashboardBundle = (_path) => {
  fetchCallCount += 1;
  return fetchCallCount === 1 ? primaryPromise : deferredPromise;
};

const renderShell = () => {};
const applyPanelResults = () => {};
const shouldRedirectToLogin = () => false;

const controller = createDashboardRefreshController({
  state,
  nodes: { autoRefreshToggle: { checked: false } },
  fetchDashboardBundle,
  renderShell,
  applyPanelResults,
  shouldRedirectToLogin,
});

const snapshots = [];
const snapshot = (label) => {
  snapshots.push({
    label,
    pendingPanelKeys: Object.keys(state.pendingPanels).sort(),
    refreshPhase: state.refreshPhase,
  });
};

snapshot('before-refresh');

const refreshPromise = controller.refreshDashboard({ manual: true });

// Synchronous snapshot: setPendingPanels runs before the first await,
// so primary + deferred panels should already be in state.pendingPanels.
snapshot('after-refresh-call-sync');

// Drain microtasks until the primary fetch's await has resumed.
primaryResolve({});
for (let i = 0; i < 8; i += 1) {
  await Promise.resolve();
}
snapshot('after-primary-resolves');

// Now drain the deferred fetch.
deferredResolve({});
for (let i = 0; i < 8; i += 1) {
  await Promise.resolve();
}
snapshot('after-deferred-resolves');

await refreshPromise;

console.log(JSON.stringify(snapshots));
"""
        result = _run_node_module_script(script)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        snapshots = json.loads(result.stdout.strip().splitlines()[-1])
        labelled = {snap["label"]: snap for snap in snapshots}

        # 1. Empty before any refresh.
        self.assertEqual(labelled["before-refresh"]["pendingPanelKeys"], [])
        self.assertEqual(labelled["before-refresh"]["refreshPhase"], "idle")

        # 2. After refreshDashboard() returns synchronously (before any await
        #    has resumed), the primary panels for the home view AND the
        #    deferred panels are both marked pending. This is what locks the
        #    buttons inside cards/drawers during the refresh.
        sync_keys = labelled["after-refresh-call-sync"]["pendingPanelKeys"]
        self.assertEqual(
            labelled["after-refresh-call-sync"]["refreshPhase"], "primary"
        )
        # A representative primary panel for "home": blockers / metrics /
        # portfolio / accountState (plus core specs like session/health).
        # The deferred panels for "home" are latestDecision / executionLatest /
        # reconciliationLatest.
        for required_primary in ("blockers", "metrics", "portfolio", "accountState"):
            self.assertIn(
                required_primary,
                sync_keys,
                msg=(
                    f"primary panel {required_primary!r} should be marked "
                    f"pending while the primary fetch is in flight, so the "
                    f"buttons inside its card get the is-refresh-locked "
                    f"class. Without this the user can fire actions against "
                    f"pre-refresh stale data."
                ),
            )
        for required_deferred in (
            "latestDecision",
            "executionLatest",
            "reconciliationLatest",
        ):
            self.assertIn(required_deferred, sync_keys)

        # 3. After primary resolves but before deferred resolves: primary
        #    panels are cleared, deferred panels are still pending. This is
        #    what unlocks the cards whose data has landed while keeping the
        #    deferred-only cards locked.
        after_primary_keys = labelled["after-primary-resolves"]["pendingPanelKeys"]
        for primary_key in ("blockers", "metrics", "portfolio", "accountState"):
            self.assertNotIn(
                primary_key,
                after_primary_keys,
                msg=(
                    f"primary panel {primary_key!r} should be cleared from "
                    f"pendingPanels once the primary fetch resolves, so its "
                    f"card unlocks while the deferred fetch is still going."
                ),
            )
        for deferred_key in (
            "latestDecision",
            "executionLatest",
            "reconciliationLatest",
        ):
            self.assertIn(
                deferred_key,
                after_primary_keys,
                msg=(
                    f"deferred panel {deferred_key!r} should remain pending "
                    f"after primary resolves and until the deferred fetch "
                    f"completes. This is what keeps the deferred-only cards "
                    f"locked while the primary cards are already interactive."
                ),
            )

        # 4. After deferred resolves: pendingPanels is empty again.
        self.assertEqual(
            labelled["after-deferred-resolves"]["pendingPanelKeys"],
            [],
            msg="all panels should be cleared once both fetches resolve",
        )
        self.assertEqual(
            labelled["after-deferred-resolves"]["refreshPhase"],
            "idle",
        )


if __name__ == "__main__":
    unittest.main()
