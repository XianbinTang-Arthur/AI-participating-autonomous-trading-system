from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_node_module_script(script: str) -> subprocess.CompletedProcess[str]:
    # Explicit encoding="utf-8" is REQUIRED — Node.js writes UTF-8 to stdout
    # but text=True alone defaults to the system locale (GBK on Chinese
    # Windows), which crashes on UTF-8 Chinese bytes the moment a test
    # script echoes any localized string.
    return subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


class TestDashboardRefreshInteractivity(unittest.TestCase):
    def test_rebaseline_actions_use_long_request_timeout(self) -> None:
        script = r"""
import {
  REBASELINE_REQUEST_TIMEOUT_MS,
  createRiskActionHandlers,
} from './aats/api/static/modules/actions/risk-actions.js';

globalThis.window = { confirm: () => true };

const dangerousCalls = [];
const actionCalls = [];
const noop = () => {};

const handlers = createRiskActionHandlers({
  activeExitExecutionHistoryState: () => ({}),
  activeExitExecutionHistoryView: () => 'risk',
  activePhase1ShadowBlocker: () => null,
  beginAction: () => () => {},
  controlPermissionMessage: () => '',
  ensureExitExecutionHistoryState: () => ({}),
  localizedRecoveryReasons: () => '',
  openDrawer: noop,
  refreshDashboard: async () => {},
  renderBanners: noop,
  requestJson: async () => ({}),
  runAction: async (...args) => {
    actionCalls.push(args);
    return { ok: true };
  },
  runDangerousAction: async (payload) => {
    dangerousCalls.push(payload);
    return { ok: true };
  },
  scrollExitExecutionWorkspaceIntoView: noop,
  state: { data: { blockerControl: { panel_version: 'panel-v1' } } },
  syncActiveViewLocationState: noop,
  syncExitExecutionHistoryFilterRoots: noop,
  syncExitExecutionHistoryFiltersAcrossViews: noop,
});

await handlers['trigger-rebaseline']('', { dataset: {} });
await handlers['trigger-blocker-action']('accept-rebaseline::operator_rebaseline_required', { dataset: {} });

console.log(JSON.stringify({
  timeoutConstant: REBASELINE_REQUEST_TIMEOUT_MS,
  directTimeout: dangerousCalls[0]?.requestOptions?.timeout,
  blockerPath: actionCalls[0]?.[0],
  blockerTimeout: actionCalls[0]?.[3]?.requestOptions?.timeout,
}));
"""
        result = _run_node_module_script(script)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["timeoutConstant"], 120_000)
        self.assertEqual(payload["directTimeout"], 120_000)
        self.assertEqual(payload["blockerPath"], "/system/blocker-actions/accept-rebaseline")
        self.assertEqual(payload["blockerTimeout"], 120_000)

    def test_app_action_runner_forwards_request_options_to_request_json(self) -> None:
        app_js = (REPO_ROOT / "aats" / "api" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("requestOptions = {}", app_js)
        self.assertIn('requestJson(path, { method: "POST", body, ...requestOptions })', app_js)
        self.assertIn("return runAction(path, body, successMessage, { target, pendingLabel, requestOptions });", app_js)

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

    def test_render_shell_locks_buttons_outside_data_panel_key_during_primary_phase(self) -> None:
        """Regression: buttons in cards WITHOUT data-panel-key must also lock
        during the PRIMARY refresh phase.

        The user-visible bug was: only ~14 of ~86 surface cards across the
        workspace views set a panelKey, and the open detail drawer never sets
        one at all. The first-attempt fix (mark primary panels pending in
        pendingPanels) therefore only locked buttons inside those ~14 cards,
        leaving the other ~72 cards + the drawer clickable during refresh —
        exactly what the user reported when they said 卡片在刷新 但是按钮还是
        可以点击.

        The final fix is at the shell-renderer level: syncRefreshInteractivity
        passes refreshing = (viewIsLoading || isPrimaryRefreshing), which
        locks EVERY button inside the active view section + open drawer
        whenever state.refreshPhase === REFRESH_PHASE_PRIMARY — regardless of
        whether the button's enclosing card has a data-panel-key attribute.

        This test drives a real createDashboardShellRenderer with a minimal
        DOM shim and verifies:
          1. At rest (refreshPhase = idle): the button is NOT locked.
          2. Primary phase (refreshPhase = primary): the button IS locked
             even though it has NO data-panel-key ancestor.
          3. Deferred phase (refreshPhase = deferred): the button is NOT
             locked — deferred is a background fill-in and must not block
             the view-wide interaction.
          4. Back to idle: the button is restored.
        """
        script = r"""
import { createDashboardShellRenderer } from './aats/api/static/modules/shell-renderer.js';
import {
  createState,
  REFRESH_PHASE_DEFERRED,
  REFRESH_PHASE_IDLE,
  REFRESH_PHASE_PRIMARY,
} from './aats/api/static/modules/store.js';

globalThis.document = {
  title: '',
  visibilityState: 'visible',
};
globalThis.window = {
  setTimeout: () => null,
  clearTimeout: () => null,
};
globalThis.Date = Date;

// --- Minimal DOM shims -------------------------------------------------
function createClassList(initial = '') {
  const classes = new Set(
    String(initial).split(/\s+/).filter(Boolean),
  );
  return {
    _classes: classes,
    add(name) { classes.add(name); },
    remove(name) { classes.delete(name); },
    toggle(name, force) {
      const should = force === undefined ? !classes.has(name) : Boolean(force);
      if (should) classes.add(name);
      else classes.delete(name);
    },
    contains(name) { return classes.has(name); },
    toString() { return Array.from(classes).join(' '); },
  };
}

// Button that is NOT wrapped in a [data-panel-key] card. closest() returns
// null for [data-panel-key] but returns the button itself for 'button'.
function createBareButton(label) {
  const attrs = new Map();
  return {
    _label: label,
    disabled: false,
    dataset: {},
    classList: createClassList(),
    getAttribute(name) { return attrs.has(name) ? attrs.get(name) : null; },
    setAttribute(name, value) { attrs.set(name, String(value)); },
    removeAttribute(name) { attrs.delete(name); },
    hasAttribute(name) { return attrs.has(name); },
    closest(selector) {
      // The critical property: this button is NOT inside any
      // [data-panel-key] card. That's the whole point of the regression.
      if (selector === '[data-panel-key]') return null;
      return null;
    },
  };
}

const loneButton = createBareButton('查看影子详情');

// Fake view section. querySelectorAll('button') → [loneButton];
// querySelectorAll('[data-panel-key]') → [] (no panel-key cards at all).
function createViewSection(viewName, buttons) {
  return {
    dataset: { view: viewName },
    classList: createClassList(),
    querySelectorAll(selector) {
      if (selector === 'button') return buttons;
      if (selector === '[data-panel-key]') return [];
      return [];
    },
  };
}

const homeSection = createViewSection('home', [loneButton]);
const viewSections = [homeSection];
const viewLinks = [
  { dataset: { view: 'home' }, classList: createClassList() },
];

// Minimal nodes map. Most shell-renderer helpers are null-safe, so nulls
// are fine for things we don't care about in this test.
const nodes = {
  pageEyebrow: null,
  pageHeading: null,
  pageCopy: null,
  pageHead: null,
  sessionIdentityValue: null,
  sessionRoleValue: null,
  authStateChip: null,
  statusRibbon: null,
  bannerContainer: null,
  homeContent: null,
  overviewContent: null,
  strategyContent: null,
  executionContent: null,
  riskContent: null,
  exitExecutionContent: null,
  replayContent: null,
  aiAnalysisContent: null,
  aiConfigContent: null,
  adminContent: null,
  resumeButton: null,
  haltButton: null,
  refreshButton: null,
  logoutButton: null,
  actionPermissionHint: null,
  refreshStateChip: null,
  lastRefreshLabel: null,
  detailDrawer: null,
};

const state = createState();
state.activeView = 'home';

const renderer = createDashboardShellRenderer({
  state,
  nodes,
  viewLinks,
  viewSections,
  renderActiveView: () => {},
  shouldRenderLoadingState: () => false,
  isBackgroundRefreshingView: () => false,
  isBootstrapping: () => false,
  hasResolvedPanel: () => true,
  hasResolvedAuthContext: () => true,
  operatorCanWrite: () => true,
  controlPermissionMessage: () => '',
  resumeActionAvailable: () => false,
  resumeActionHintText: () => '',
  syncExitExecutionNavigationLinks: () => {},
  localizedRecoveryReasons: () => '',
  isPausedAwaitingResume: () => false,
});

function snapshot(label) {
  return {
    label,
    disabled: loneButton.disabled === true,
    hasLockedClass: loneButton.classList.contains('is-refresh-locked'),
    title: loneButton.getAttribute('title'),
  };
}

const snapshots = [];

// 1. At rest: refreshPhase = idle → button NOT locked.
state.refreshPhase = REFRESH_PHASE_IDLE;
renderer.renderShell();
snapshots.push(snapshot('idle-initial'));

// 2. Primary phase: refreshPhase = primary → button IS locked, even
//    though it has no [data-panel-key] ancestor.
state.refreshPhase = REFRESH_PHASE_PRIMARY;
renderer.renderShell();
snapshots.push(snapshot('primary-phase'));

// 3. Deferred phase: refreshPhase = deferred → button NOT locked (deferred
//    is a background fill-in, the user must be able to interact with cards
//    whose primary data already landed).
state.refreshPhase = REFRESH_PHASE_DEFERRED;
renderer.renderShell();
snapshots.push(snapshot('deferred-phase'));

// 4. Back to idle: button fully restored.
state.refreshPhase = REFRESH_PHASE_IDLE;
renderer.renderShell();
snapshots.push(snapshot('idle-final'));

console.log(JSON.stringify(snapshots));
"""
        result = _run_node_module_script(script)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        snapshots = json.loads(result.stdout.strip().splitlines()[-1])
        labelled = {snap["label"]: snap for snap in snapshots}

        # 1. Idle: button is interactive.
        self.assertFalse(
            labelled["idle-initial"]["disabled"],
            msg="button should be interactive before any refresh starts",
        )
        self.assertFalse(
            labelled["idle-initial"]["hasLockedClass"],
            msg="is-refresh-locked class should not be present at rest",
        )

        # 2. PRIMARY phase: button MUST be locked even though it has no
        #    [data-panel-key] ancestor. This is the regression that the
        #    user reported: 卡片在刷新 但是按钮还是可以点击. The view-level
        #    lock in syncRefreshInteractivity is what prevents that.
        self.assertTrue(
            labelled["primary-phase"]["disabled"],
            msg=(
                "button OUTSIDE [data-panel-key] must be locked while "
                "refreshPhase === REFRESH_PHASE_PRIMARY. If this fails, the "
                "view-wide lock in shell-renderer.syncRefreshInteractivity "
                "has regressed and buttons in the ~72 cards without "
                "panelKey + the open drawer will stay clickable during "
                "refresh."
            ),
        )
        self.assertTrue(
            labelled["primary-phase"]["hasLockedClass"],
            msg="is-refresh-locked class must be applied during PRIMARY phase",
        )
        self.assertEqual(
            labelled["primary-phase"]["title"],
            "当前区域正在刷新，请等待刷新完成后再操作。",
            msg=(
                "PRIMARY-phase lock reason should be the view-level reason, "
                "not the per-panel reason, because the button isn't inside "
                "a [data-panel-key] card."
            ),
        )

        # 3. DEFERRED phase: the view-wide lock must NOT apply. Deferred is a
        #    background fill-in; locking the whole view for it would mean
        #    every 30s auto-refresh tick leaves the user unable to interact
        #    with cards whose data already landed during the primary phase.
        self.assertFalse(
            labelled["deferred-phase"]["disabled"],
            msg=(
                "button must NOT be locked during DEFERRED phase — that "
                "phase is a background fill-in for late-landing panels and "
                "locking the whole view would break the interactive"
                "contract for the cards whose primary data already landed."
            ),
        )
        self.assertFalse(
            labelled["deferred-phase"]["hasLockedClass"],
            msg="is-refresh-locked class should not linger in DEFERRED phase",
        )

        # 4. Back to idle: button restored and locked class cleared.
        self.assertFalse(labelled["idle-final"]["disabled"])
        self.assertFalse(labelled["idle-final"]["hasLockedClass"])


if __name__ == "__main__":
    unittest.main()
