import { fetchPanels, requestJson } from "./modules/api-client.js";
import { createAdminActions } from "./modules/actions/admin-actions.js";
import { fetchDashboardBundle } from "./modules/api-client.js";
import { createExecutionActionHandlers } from "./modules/actions/execution-actions.js";
import { createRiskActionHandlers } from "./modules/actions/risk-actions.js";
import { createDashboardRefreshController } from "./modules/dashboard-refresh.js";
import { ensureNotBusy, setFlash } from "./modules/flash.js";
import {
  listOrDash,
} from "./modules/formatters.js";
import { buildDecisionDrawer } from "./modules/detail-drawers.js";
import { createDashboardShellRenderer } from "./modules/shell-renderer.js";
import {
  DEFAULT_EXIT_EXECUTION_HISTORY_FILTERS,
  DEFAULT_PAGE_LIMITS,
  EXIT_EXECUTION_FILTER_AFFECTED_VIEWS,
  PAGE_LIMIT_AFFECTED_VIEWS,
  PAGE_LOAD_STEP,
  createState,
  invalidateCachedViews,
} from "./modules/store.js";
import {
  localizeError,
  readableState,
} from "./modules/terms.js";
import {
  EXIT_EXECUTION_HISTORY_ACTION_FILTERS,
  EXIT_EXECUTION_HISTORY_WINDOW_FILTERS,
  coerceReplayParentFilter,
  createNavigationStateController,
  exitExecutionHistoryWindowThresholdMs,
  normalizeExitExecutionHistoryFilterValue,
} from "./modules/navigation-state.js";
import {
  VIEW_LABELS,
  resolveKnownView,
  resolveViewFromLocation,
} from "./modules/view-router.js";
import { renderAIAnalysisView } from "./modules/views/ai-analysis-view.js";
import { renderAIConfigView } from "./modules/views/ai-config-view.js";
import { renderAdminView } from "./modules/views/admin-view.js";
import { renderExecutionSections, renderExecutionView } from "./modules/views/execution-view.js";
import { renderExitExecutionView } from "./modules/views/exit-execution-view.js";
import { renderHomeView } from "./modules/views/home-view.js";
import { renderOverviewView } from "./modules/views/overview-view.js";
import { renderReplaySections, renderReplayView } from "./modules/views/replay-view.js";
import { renderRiskSections, renderRiskView } from "./modules/views/risk-view.js";
import { renderStrategySections, renderStrategyView } from "./modules/views/strategy-view.js";

const state = createState();

const viewLinks = Array.from(document.querySelectorAll(".workspace-link[data-view]"));
const viewSections = Array.from(document.querySelectorAll(".workspace-view"));

const nodes = {
  pageHead: document.getElementById("pageHead"),
  pageEyebrow: document.getElementById("pageEyebrow"),
  pageHeading: document.getElementById("pageHeading"),
  pageCopy: document.getElementById("pageCopy"),
  statusRibbon: document.getElementById("statusRibbon"),
  bannerContainer: document.getElementById("bannerContainer"),
  sessionIdentityValue: document.getElementById("sessionIdentityValue"),
  sessionRoleValue: document.getElementById("sessionRoleValue"),
  authStateChip: document.getElementById("authStateChip"),
  logoutButton: document.getElementById("logoutButton"),
  refreshButton: document.getElementById("refreshButton"),
  resumeButton: document.getElementById("resumeButton"),
  haltButton: document.getElementById("haltButton"),
  autoRefreshToggle: document.getElementById("autoRefreshToggle"),
  actionPermissionHint: document.getElementById("actionPermissionHint"),
  lastRefreshLabel: document.getElementById("lastRefreshLabel"),
  refreshStateChip: document.getElementById("refreshStateChip"),
  homeContent: document.getElementById("homeContent"),
  overviewContent: document.getElementById("overviewContent"),
  strategyContent: document.getElementById("strategyContent"),
  executionContent: document.getElementById("executionContent"),
  riskContent: document.getElementById("riskContent"),
  exitExecutionContent: document.getElementById("exitExecutionContent"),
  replayContent: document.getElementById("replayContent"),
  aiAnalysisContent: document.getElementById("aiAnalysisContent"),
  aiConfigContent: document.getElementById("aiConfigContent"),
  adminContent: document.getElementById("adminContent"),
  detailDrawer: document.getElementById("detailDrawer"),
  drawerBackdrop: document.getElementById("drawerBackdrop"),
  closeDrawerButton: document.getElementById("closeDrawerButton"),
  drawerEyebrow: document.getElementById("drawerEyebrow"),
  drawerTitle: document.getElementById("drawerTitle"),
  drawerSummary: document.getElementById("drawerSummary"),
  drawerBody: document.getElementById("drawerBody"),
};

const navigationState = createNavigationStateController({ state, viewLinks });
const {
  activeExitExecutionHistoryState,
  activeExitExecutionHistoryView,
  buildViewPath,
  ensureExitExecutionHistoryState,
  hydrateViewStateFromLocation,
  syncActiveViewLocationState,
  syncExitExecutionHistoryFiltersAcrossViews,
  syncExitExecutionNavigationLinks,
} = navigationState;

let refreshController = null;
const shellRenderer = createDashboardShellRenderer({
  state,
  nodes,
  viewLinks,
  viewSections,
  renderActiveView,
  shouldRenderLoadingState: (view) => refreshController?.shouldRenderLoadingState(view) ?? false,
  isBackgroundRefreshingView: (view) => refreshController?.isBackgroundRefreshingView(view) ?? false,
  isBootstrapping: () => refreshController?.isBootstrapping() ?? false,
  hasResolvedPanel,
  hasResolvedAuthContext,
  operatorCanWrite,
  controlPermissionMessage,
  resumeActionAvailable,
  resumeActionHintText,
  syncExitExecutionNavigationLinks,
  localizedRecoveryReasons,
  isPausedAwaitingResume,
});
const {
  currentRefreshInteractivityRoots,
  patchClassName,
  patchHtml,
  patchRenderedSections,
  patchText,
  renderBanners,
  renderLoadingView,
  renderShell,
  tickFlashExpiry,
  updateLastRefreshRelativeTime,
} = shellRenderer;

state.activeView = resolveViewFromLocation();
hydrateViewStateFromLocation(state.activeView);
state.loadingView = state.activeView;

refreshController = createDashboardRefreshController({
  state,
  nodes,
  fetchDashboardBundle,
  renderShell,
  applyPanelResults,
  shouldRedirectToLogin,
});
const {
  cancelScheduledRefresh,
  handleVisibilityChange,
  isBackgroundRefreshingView,
  isBootstrapping,
  isRefreshInFlight,
  isViewFresh,
  refreshDashboard,
  scheduleRefresh,
  shouldRenderLoadingState,
} = refreshController;

const riskActionHandlers = createRiskActionHandlers({
  activeExitExecutionHistoryState,
  activeExitExecutionHistoryView,
  activePhase1ShadowBlocker,
  beginAction,
  controlPermissionMessage,
  ensureExitExecutionHistoryState,
  localizedRecoveryReasons,
  openDrawer,
  refreshDashboard,
  renderBanners,
  requestJson,
  runAction,
  runDangerousAction,
  scrollExitExecutionWorkspaceIntoView,
  state,
  syncActiveViewLocationState,
  syncExitExecutionHistoryFilterRoots,
  syncExitExecutionHistoryFiltersAcrossViews,
});
const executionActionHandlers = createExecutionActionHandlers({
  pageLoadStep: PAGE_LOAD_STEP,
  requestJson,
  renderBanners,
  openDrawer,
  runDangerousAction,
  state,
  adjustPageLimit,
  resetPageLimit,
});
const adminActions = createAdminActions({
  beginAction,
  renderBanners,
  refreshDashboard,
  requestJson,
  state,
});
const adminActionHandlers = adminActions.actionHandlers;

init();

function init() {
  bindEvents();
  renderShell();
  void refreshDashboard();
  // Tick the "最近刷新" relative-age label once a second so users see "5 秒
  // 前" roll forward without waiting for the next renderShell() call (which
  // only fires on state transitions). The tick bails out during the PRIMARY
  // phase so it does not clobber "正在刷新最新状态…" mid-fetch.
  // The same tick also expires sticky flash banners (state.flash) so an
  // 8-second-old success notice actually disappears from the DOM instead of
  // waiting for the next state-driven render.
  window.setInterval(() => {
    updateLastRefreshRelativeTime();
    tickFlashExpiry();
  }, 1000);
}

function bindEvents() {
  viewLinks.forEach((link) => {
    link.addEventListener("click", (event) => {
      if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      setActiveView(link.dataset.view || "overview", { pushHistory: true });
    });
  });

  window.addEventListener("popstate", () => {
    const nextView = resolveViewFromLocation();
    hydrateViewStateFromLocation(nextView);
    // hydrateViewStateFromLocation only mutates filter state when nextView
    // is "exitExecution" (it short-circuits otherwise — see
    // navigation-state.js). However the mutation it performs propagates the
    // URL filters into BOTH the exitExecution view state AND the risk view
    // state (via syncExitExecutionHistoryFiltersAcrossViews), because the
    // two views share the exit-execution history panel. So:
    //
    //   * Going back/forward INTO exitExecution rewrites both views' filter
    //     state from the URL → both cached bundles can be stale.
    //   * Going back/forward INTO any other view rewrites nothing → invalidating
    //     here is a defensive no-op that costs us essentially nothing.
    //
    // Either way, invalidating EXIT_EXECUTION_FILTER_AFFECTED_VIEWS (the
    // {exitExecution, risk} pair) covers the only views whose bundle URLs
    // actually embed those filters, so unrelated views keep their fast path.
    invalidateCachedViews(state, nextView, EXIT_EXECUTION_FILTER_AFFECTED_VIEWS);
    setActiveView(nextView, { refresh: true });
  });

  window.addEventListener("pageshow", (event) => {
    if (!event.persisted) return;
    const nextView = resolveViewFromLocation();
    hydrateViewStateFromLocation(nextView);
    // Same reasoning as the popstate handler above. bfcache restore can
    // bring back a page whose in-memory exitExecution filter state diverged
    // from the URL during the previous visit; the hydrate call rewrites it
    // back from the URL when re-entering exitExecution, and the invalidate
    // ensures the next bundle fetch reflects the freshly hydrated filters
    // rather than serving the stale cached bundle.
    invalidateCachedViews(state, nextView, EXIT_EXECUTION_FILTER_AFFECTED_VIEWS);
    setActiveView(nextView, { refresh: true });
  });
  document.addEventListener("visibilitychange", handleVisibilityChange);

  nodes.refreshButton?.addEventListener("click", () => void refreshDashboard({ manual: true }));
  nodes.resumeButton?.addEventListener("click", () => void dispatchAction("trigger-resume", "", nodes.resumeButton));
  nodes.haltButton?.addEventListener("click", () => void dispatchAction("trigger-halt", "", nodes.haltButton));
  nodes.logoutButton?.addEventListener("click", () => void logoutOperator());
  nodes.autoRefreshToggle?.addEventListener("change", () => {
    if (nodes.autoRefreshToggle.checked) {
      scheduleRefresh();
    } else {
      cancelScheduledRefresh();
    }
  });
  nodes.closeDrawerButton?.addEventListener("click", closeDrawer);
  nodes.drawerBackdrop?.addEventListener("click", closeDrawer);

  document.addEventListener("click", (event) => {
    const target = event.target instanceof HTMLElement ? event.target.closest("[data-action]") : null;
    if (!target) return;
    const action = target.dataset.action;
    const value = target.dataset.value || "";
    if (!action) return;
    void dispatchAction(action, value, target);
  });

  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (form.id === "operatorCreateForm") {
      event.preventDefault();
      void adminActions.createOperatorUser();
    }
  });

  document.addEventListener("input", handleExitExecutionHistoryFilterEvent);
  document.addEventListener("change", handleExitExecutionHistoryFilterEvent);
}

function applyPanelResults(results) {
  for (const [key, result] of Object.entries(results || {})) {
    state.data[key] = result.data;
    state.errors[key] = result.error;
  }
}

function isPausedAwaitingResume(recovery = state.data.systemRecovery?.recovery || {}) {
  return Boolean(recovery.halted && recovery.resume_eligible && !recovery.safe_to_trade);
}

function resumeActionAvailable() {
  const recovery = state.data.systemRecovery?.recovery || {};
  return Boolean(recovery.resume_eligible);
}

function resumeActionHintText() {
  if (resumeActionAvailable()) {
    return isPausedAwaitingResume()
      ? operationalStatusCopy({ recovery: state.data.systemRecovery?.recovery || {} })
      : "";
  }
  const reasons = localizedRecoveryReasons();
  return operationalStatusCopy({
    recovery: state.data.systemRecovery?.recovery || {},
    recoveryReasonText: reasons,
  });
}

function renderActiveView() {
  if (shouldRenderLoadingState(state.activeView)) {
    renderLoadingView();
    return;
  }

  const viewData = {
    ...state.data,
    errors: state.errors,
    uiHints: {
      recoveryReasonsText: localizedRecoveryReasons(),
      controlPermissionMessage: controlPermissionMessage(),
      pendingPanels: state.pendingPanels,
    },
  };
  if (state.activeView === "overview" && nodes.overviewContent) {
    patchHtml(nodes.overviewContent, renderOverviewView(viewData));
    return;
  }
  if (state.activeView === "home" && nodes.homeContent) {
    patchHtml(nodes.homeContent, renderHomeView(viewData));
    return;
  }
  if (state.activeView === "strategy") {
    patchRenderedSections(renderStrategySections(viewData), () => nodes.strategyContent, () => renderStrategyView(viewData));
    return;
  }
  if (state.activeView === "execution") {
    patchRenderedSections(renderExecutionSections(viewData), () => nodes.executionContent, () => renderExecutionView(viewData));
    return;
  }
  if (state.activeView === "risk") {
    patchRenderedSections(
      renderRiskSections(viewData, state.ui.risk),
      () => nodes.riskContent,
      () => renderRiskView(viewData, state.ui.risk),
    );
    return;
  }
  if (state.activeView === "exitExecution" && nodes.exitExecutionContent) {
    patchHtml(
      nodes.exitExecutionContent,
      renderExitExecutionView(viewData, state.ui.exitExecution || {}),
    );
    return;
  }
  if (state.activeView === "replay") {
    patchRenderedSections(
      renderReplaySections(viewData, state.ui.replay, {
        recentReplayValidationsLimit: state.pageLimits.recentReplayValidations,
        defaultReplayValidationsLimit: DEFAULT_PAGE_LIMITS.recentReplayValidations,
      }),
      () => nodes.replayContent,
      () => renderReplayView(viewData, state.ui.replay, {
        recentReplayValidationsLimit: state.pageLimits.recentReplayValidations,
        defaultReplayValidationsLimit: DEFAULT_PAGE_LIMITS.recentReplayValidations,
      }),
    );
    return;
  }
  if (state.activeView === "aiAnalysis" && nodes.aiAnalysisContent) {
    patchHtml(nodes.aiAnalysisContent, renderAIAnalysisView(viewData));
    return;
  }
  if (state.activeView === "aiConfig" && nodes.aiConfigContent) {
    patchHtml(
      nodes.aiConfigContent,
      renderAIConfigView({
        session: state.data.session || {},
        aiRuntime: state.data.aiRuntime || {},
        summary: state.data.aiConfigModel || {},
        error: state.errors.aiConfigModel || null,
        uiState: state.ui.aiConfig,
      }),
    );
    return;
  }
  if (state.activeView === "admin" && nodes.adminContent) {
    patchHtml(nodes.adminContent, renderAdminView(viewData));
  }
}


function setActiveView(view, { pushHistory = false, refresh = true } = {}) {
  const nextView = resolveKnownView(view);
  const changed = state.activeView !== nextView;
  if (changed) {
    state.activeView = nextView;
    // loadingView drives the skeleton: show it for a fresh transition into
    // a view whose bundle has never landed, hide it when the target view is
    // already in the readyViews cache (renderShell will paint cached data).
    state.loadingView = state.readyViews[nextView] ? null : nextView;
  }
  if (pushHistory) {
    const targetPath = buildViewPath(nextView);
    const currentPath = `${window.location.pathname}${window.location.search}`;
    if (currentPath !== targetPath) {
      window.history.pushState({ view: nextView }, "", targetPath);
    }
  } else if (nextView === "exitExecution") {
    syncActiveViewLocationState({ pushHistory: false });
  }
  viewLinks.forEach((link) => link.classList.toggle("is-active", link.dataset.view === nextView));
  viewSections.forEach((section) => section.classList.toggle("is-active", section.dataset.view === nextView));
  renderShell();
  if (!refresh) return;
  // Stale-while-revalidate fast path: cached bundle for this view is still
  // within its freshness window, skip the network round-trip entirely.
  // renderShell above already painted the cached data.
  if (changed && isViewFresh(nextView) && state.readyViews[nextView]) {
    return;
  }
  void refreshDashboard();
}

function beginAction(target, pendingLabel) {
  cancelScheduledRefresh();
  state.actionInFlight = true;
  const clearPending = setActionPending(target, pendingLabel);
  renderShell();
  return () => {
    clearPending();
    state.actionInFlight = false;
    renderShell();
    // Drain a queued refresh request if any was stashed during the action.
    // Carry the manual flag through so a queued manual refresh still shows
    // its "已刷新" flash on the drained run.
    if (state.pendingRefresh && !isRefreshInFlight()) {
      const drained = state.pendingRefresh;
      state.pendingRefresh = null;
      void refreshDashboard(drained);
      return;
    }
    scheduleRefresh();
  };
}

async function runAction(path, body, successMessage, { target = null, pendingLabel = "正在提交请求…" } = {}) {
  // ensureNotBusy surfaces an explicit "请等待上一次完成" flash instead of
  // silently dropping the click. Without this guard the user has no way to
  // tell the request was ignored.
  if (!ensureNotBusy(state, renderBanners)) return;
  const finishAction = beginAction(target, pendingLabel);
  try {
    const result = await requestJson(path, { method: "POST", body });
    setFlash(state, "info", result?.message || successMessage);
    await refreshDashboard({ manual: true });
  } catch (error) {
    setFlash(state, "danger", error instanceof Error ? error.message : String(error));
    renderBanners();
  } finally {
    finishAction();
  }
}

async function runDangerousAction({ path, body, successMessage, confirmMessage, target = null, pendingLabel = "正在提交请求…" }) {
  if (!window.confirm(confirmMessage)) return;
  await runAction(path, body, successMessage, { target, pendingLabel });
}


// Local in-flight latch instead of going through ensureNotBusy(): logout does
// NOT call beginAction (it never wants to flip the global actionInFlight
// because it's about to navigate away), so the shared actionInFlight guard
// would never see it and a fast double-click would happily fire two POST
// /auth/logout requests. The logoutButton is also intentionally kept
// clickable in shell-renderer.js even while another action is in flight, so
// we have to defend at this layer. The success path leaves logoutInFlight at
// true on purpose — the page is about to be replaced by /login anyway and
// we don't want a slow location.assign to allow another click in the gap.
let logoutInFlight = false;

async function logoutOperator() {
  if (logoutInFlight) return;
  logoutInFlight = true;
  try {
    await requestJson("/auth/logout", { method: "POST" });
    window.location.assign("/login");
  } catch (error) {
    setFlash(state, "danger", error instanceof Error ? error.message : String(error));
    renderBanners();
    // Failure path: clear the latch so the user can retry. Success path
    // intentionally leaves it at true (see comment above).
    logoutInFlight = false;
  }
}

async function dispatchAction(action, value, target = null) {
  if (action === "refresh-dashboard") return refreshDashboard({ manual: true });
  if (action === "navigate-view") {
    const nextView = resolveKnownView(value);
    if (state.activeView === nextView) {
      // NB: this flash path is intentionally DIFFERENT from the top nav link
      // click handler (which silently calls setActiveView on same-view). Nav
      // links are visually obvious as navigation controls, so users don't
      // need feedback when clicking them. This dispatchAction path, on the
      // other hand, is triggered from in-card "jump to X" buttons where the
      // user expects a visible navigation; when the target happens to be the
      // current view we owe them an explicit "why did nothing visually move"
      // explanation. Keep the flash + scroll + manual refresh combo.
      //
      // Tense note: this flash now lives across the entire ~8s sticky-flash
      // TTL (see shell-renderer.js renderBanners), so the message must still
      // make sense AFTER refreshDashboard finishes. Past-tense / completed
      // phrasing is the safest choice. The `&& !state.flash` guard inside
      // refreshDashboard prevents the generic "页面数据已刷新" notice from
      // clobbering this more specific message at end of refresh.
      setFlash(state, "info", `当前已在${VIEW_LABELS[nextView] || "当前页面"}，已为你重新拉取最新数据。`);
      window.scrollTo({ top: 0, behavior: "smooth" });
      return refreshDashboard({ manual: true });
    }
    return setActiveView(nextView, { pushHistory: true });
  }
  if (action === "inspect-decision") return inspectDecision(value);
  if (action === "select-ai-operating-mode") return selectAIOperatingMode(value, target);
  if (action === "manual-activate-strategy-profile") return activateStrategyProfile(value, target);
  if (action === "restore-strategy-profile-auto") return restoreStrategyProfileAutomaticControl(target);
  if (action === "pause-strategy-profile-auto") return pauseStrategyProfileAutomaticControl(target);
  if (action === "set-profile-control-mode") return setStrategyProfileControlMode(value, target);
  if (action === "load-more-decisions") return adjustPageLimit("recentDecisions", PAGE_LOAD_STEP);
  if (action === "collapse-decisions") return resetPageLimit("recentDecisions");
  if (action === "load-more-ai-assessments") return adjustPageLimit("recentAIAssessments", PAGE_LOAD_STEP);
  if (action === "collapse-ai-assessments") return resetPageLimit("recentAIAssessments");
  if (action === "load-more-ai-shadow-decisions") return adjustPageLimit("recentAIShadowDecisions", PAGE_LOAD_STEP);
  if (action === "collapse-ai-shadow-decisions") return resetPageLimit("recentAIShadowDecisions");
  if (action === "load-more-ai-shadow-evaluations") return adjustPageLimit("recentAIShadowEvaluations", PAGE_LOAD_STEP);
  if (action === "collapse-ai-shadow-evaluations") return resetPageLimit("recentAIShadowEvaluations");
  if (action === "load-more-replay-validations") return adjustPageLimit("recentReplayValidations", PAGE_LOAD_STEP);
  if (action === "collapse-replay-validations") return resetPageLimit("recentReplayValidations");
  if (action === "set-replay-parent-filter") return setReplayParentFilter(value);
  const domainHandler = riskActionHandlers[action] || executionActionHandlers[action] || adminActionHandlers[action];
  if (domainHandler) {
    return domainHandler(value, target);
  }
}

async function inspectDecision(decisionId) {
  if (!decisionId) return;
  try {
    const detail = await requestJson(`/decision/${encodeURIComponent(decisionId)}`);
    openDrawer(buildDecisionDrawer(detail));
  } catch (error) {
    setFlash(state, "danger", error instanceof Error ? error.message : String(error));
    renderBanners();
  }
}

async function adjustPageLimit(key, delta) {
  const current = Number(state.pageLimits?.[key] || DEFAULT_PAGE_LIMITS[key] || 0);
  const nextValue = current + delta;
  // Guard against non-positive limits. A negative or zero limit would be
  // serialized into the bundle URL and rejected by the backend, wasting a
  // round-trip and leaving the UI in a confusing state.
  if (!Number.isFinite(nextValue) || nextValue < 1) return;
  state.pageLimits[key] = nextValue;
  // pageLimits is embedded in the bundle URL only for the views that render
  // the affected panel. PAGE_LIMIT_AFFECTED_VIEWS narrows the invalidation
  // to that subset so unrelated views retain their stale-while-revalidate
  // cache entries.
  invalidateCachedViews(state, state.activeView, PAGE_LIMIT_AFFECTED_VIEWS[key] || null);
  await refreshDashboard();
}

async function resetPageLimit(key) {
  state.pageLimits[key] = DEFAULT_PAGE_LIMITS[key] || state.pageLimits[key];
  invalidateCachedViews(state, state.activeView, PAGE_LIMIT_AFFECTED_VIEWS[key] || null);
  await refreshDashboard();
}

function setReplayParentFilter(value) {
  state.ui.replay.parentFilter = coerceReplayParentFilter(value);
  renderShell();
}

function handleExitExecutionHistoryFilterEvent(event) {
  const target = event.target;
  if (!(target instanceof HTMLInputElement) && !(target instanceof HTMLSelectElement)) {
    return;
  }
  const filterKey = target.dataset.exitHistoryFilter;
  if (!filterKey) return;
  const activeHistoryState = activeExitExecutionHistoryState();
  if (filterKey === "action") {
    activeHistoryState.action = EXIT_EXECUTION_HISTORY_ACTION_FILTERS.has(target.value) ? target.value : "all";
  } else if (filterKey === "parent") {
    activeHistoryState.parent = target.value || "";
  } else if (filterKey === "actor") {
    activeHistoryState.actor = target.value || "";
  } else if (filterKey === "windowHours") {
    activeHistoryState.windowHours = EXIT_EXECUTION_HISTORY_WINDOW_FILTERS.has(target.value) ? target.value : "all";
  } else {
    return;
  }
  activeHistoryState.offset = 0;
  syncExitExecutionHistoryFiltersAcrossViews(activeExitExecutionHistoryView());
  if (state.activeView === "exitExecution") {
    syncActiveViewLocationState({ pushHistory: false });
  }
  syncExitExecutionHistoryFilterRoots();
  // The filter mutation above ALSO mutates the OTHER (non-active) exit-execution
  // view's filter state via syncExitExecutionHistoryFiltersAcrossViews. Both
  // views' bundle URLs depend on these filters, so the cached "ready" marker
  // for the non-active view (built with the previous filters AND the previous
  // offset, which we just reset to 0) is now stale. Invalidate it so the next
  // view switch refetches with the new filters instead of hitting the
  // fast-path with mismatched data. The active view is exempted because the
  // applyExitExecutionHistoryFilters() invocation inside
  // syncExitExecutionHistoryFilterRoots() already re-applies the filter
  // visually on the cached DOM.
  invalidateCachedViews(state, state.activeView, EXIT_EXECUTION_FILTER_AFFECTED_VIEWS);
}

function applyExitExecutionHistoryFilters(root) {
  const actionFilter = normalizeExitExecutionHistoryFilterValue(
    root.querySelector('[data-exit-history-filter="action"]')?.value,
  );
  const parentFilter = normalizeExitExecutionHistoryFilterValue(
    root.querySelector('[data-exit-history-filter="parent"]')?.value,
  );
  const actorFilter = normalizeExitExecutionHistoryFilterValue(
    root.querySelector('[data-exit-history-filter="actor"]')?.value,
  );
  const windowHoursFilter = normalizeExitExecutionHistoryFilterValue(
    root.querySelector('[data-exit-history-filter="windowHours"]')?.value,
  );
  const thresholdMs = exitExecutionHistoryWindowThresholdMs(windowHoursFilter);
  const entries = Array.from(root.querySelectorAll("[data-exit-history-entry]"));
  let visibleCount = 0;
  entries.forEach((entry) => {
    if (!(entry instanceof HTMLElement)) return;
    const matchesAction = !actionFilter || actionFilter === "all"
      || normalizeExitExecutionHistoryFilterValue(entry.dataset.actionKind) === actionFilter;
    const matchesParent = !parentFilter
      || normalizeExitExecutionHistoryFilterValue(entry.dataset.parentIntentId).includes(parentFilter);
    const matchesActor = !actorFilter
      || normalizeExitExecutionHistoryFilterValue(entry.dataset.actorSearch).includes(actorFilter);
    const entryCreatedAtMs = Number(entry.dataset.createdAtMs || "0");
    const matchesWindow = thresholdMs === null || (Number.isFinite(entryCreatedAtMs) && entryCreatedAtMs >= thresholdMs);
    const visible = matchesAction && matchesParent && matchesActor && matchesWindow;
    entry.hidden = !visible;
    if (visible) {
      visibleCount += 1;
    }
  });
  const emptyState = root.querySelector("[data-exit-history-empty]");
  if (emptyState instanceof HTMLElement) {
    emptyState.hidden = visibleCount > 0;
  }
}

function syncExitExecutionHistoryFilterRoots() {
  const filters = activeExitExecutionHistoryState();
  syncExitExecutionNavigationLinks();
  const roots = Array.from(document.querySelectorAll("[data-exit-history-root]"));
  roots.forEach((root) => {
    if (!(root instanceof HTMLElement)) return;
    const actionInput = root.querySelector('[data-exit-history-filter="action"]');
    const parentInput = root.querySelector('[data-exit-history-filter="parent"]');
    const actorInput = root.querySelector('[data-exit-history-filter="actor"]');
    const windowInput = root.querySelector('[data-exit-history-filter="windowHours"]');
    if (actionInput instanceof HTMLSelectElement) {
      actionInput.value = String(filters.action || "all");
    }
    if (parentInput instanceof HTMLInputElement) {
      parentInput.value = String(filters.parent || "");
    }
    if (actorInput instanceof HTMLInputElement) {
      actorInput.value = String(filters.actor || "");
    }
    if (windowInput instanceof HTMLSelectElement) {
      windowInput.value = String(filters.windowHours || "all");
    }
    applyExitExecutionHistoryFilters(root);
  });
}

function scrollExitExecutionWorkspaceIntoView(target = null) {
  const workspace = document.getElementById(state.activeView === "exitExecution" ? "exit-execution-workspace" : "risk-exit-workspace");
  if (workspace instanceof HTMLElement) {
    workspace.scrollIntoView({ behavior: "smooth", block: "start" });
    return;
  }
  if (target instanceof HTMLElement) {
    target.scrollIntoView({ behavior: "smooth", block: "center" });
  }
}

function setActionPending(target, pendingLabel) {
  if (!(target instanceof HTMLElement)) return () => {};
  const originalLabel = target.textContent || "";
  target.classList.add("is-pending");
  target.setAttribute("aria-busy", "true");
  if ("disabled" in target) {
    target.disabled = true;
  }
  target.textContent = pendingLabel;
  return () => {
    target.classList.remove("is-pending");
    target.removeAttribute("aria-busy");
    if ("disabled" in target) {
      target.disabled = false;
    }
    target.textContent = originalLabel;
  };
}

async function activateStrategyProfile(profileId, target = null) {
  if (!profileId) return;
  const profileLabel = target instanceof HTMLElement ? (target.textContent || "").trim() : profileId;
  // Order: confirm → ensureNotBusy → beginAction. Confirming first means
  //   1. Cancel exits without leaving a redundant "busy" flash on screen.
  //   2. ensureNotBusy is re-checked AFTER the (potentially slow) confirm
  //      dialog, which catches the race where another action lands while
  //      the user was pondering at the dialog.
  //   3. Cancelling never flips actionInFlight / cancels the scheduled
  //      refresh / triggers the pending-style indicator just to undo it.
  // This matches the order used by admin-actions.js handlers.
  if (!window.confirm(`确认立即切换到“${profileLabel}”这个已注册策略档位吗？`)) return;
  if (!ensureNotBusy(state, renderBanners)) return;
  const finishAction = beginAction(target, "正在切换策略档位…");
  try {
    const result = await requestJson(`/strategy-profiles/profiles/${encodeURIComponent(profileId)}/activate`, {
      method: "POST",
      body: { reason: "ui_manual_activate_strategy_profile" },
    });
    setFlash(
      state,
      "info",
      `当前策略档位已手动切换为 ${readableProfileName(result?.active_revision?.profile_label || result?.active_revision?.profile_id)}。`,
    );
    await refreshDashboard({ manual: true });
  } catch (error) {
    setFlash(state, "danger", error instanceof Error ? error.message : String(error));
    renderBanners();
  } finally {
    finishAction();
  }
}

async function restoreStrategyProfileAutomaticControl(target = null) {
  // confirm → ensureNotBusy → beginAction; see activateStrategyProfile.
  if (!window.confirm("确认开启自动切档吗？开启后下面 6 个档位按钮会锁定，由系统自动决定是否换档。")) return;
  if (!ensureNotBusy(state, renderBanners)) return;
  const finishAction = beginAction(target, "正在恢复自动切档…");
  try {
    const result = await requestJson("/strategy-profiles/restore-auto", {
      method: "POST",
      body: { reason: "ui_restore_auto_strategy_profile_control" },
    });
    const activation = result?.activation || {};
    setFlash(
      state,
      "info",
      activation?.active_profile_id
        ? `策略档位已恢复自动切档逻辑，当前仍保持 ${readableProfileName(result?.active_revision?.profile_label || activation.active_profile_id)}。`
        : "策略档位已恢复自动切档逻辑。",
    );
    await refreshDashboard({ manual: true });
  } catch (error) {
    setFlash(state, "danger", error instanceof Error ? error.message : String(error));
    renderBanners();
  } finally {
    finishAction();
  }
}

async function pauseStrategyProfileAutomaticControl(target = null) {
  // confirm → ensureNotBusy → beginAction; see activateStrategyProfile.
  if (!window.confirm("确认关闭自动切档吗？关闭后下面 6 个档位按钮会解锁，由你手动切换。")) return;
  if (!ensureNotBusy(state, renderBanners)) return;
  const finishAction = beginAction(target, "正在切到手动切档…");
  try {
    const result = await requestJson("/strategy-profiles/pause-auto", {
      method: "POST",
      body: { reason: "ui_pause_auto_strategy_profile_control" },
    });
    const activation = result?.activation || {};
    setFlash(
      state,
      "info",
      activation?.active_profile_id
        ? `当前已切到手动切档，系统会保持 ${readableProfileName(result?.active_revision?.profile_label || activation.active_profile_id)}。`
        : "当前已切到手动切档。",
    );
    await refreshDashboard({ manual: true });
  } catch (error) {
    setFlash(state, "danger", error instanceof Error ? error.message : String(error));
    renderBanners();
  } finally {
    finishAction();
  }
}

async function selectAIOperatingMode(mode, target = null) {
  if (!mode) return;
  const modeLabel = target instanceof HTMLElement ? (target.textContent || "").trim() : mode;
  // confirm → ensureNotBusy → beginAction; see activateStrategyProfile.
  if (!window.confirm(`确认立即把 AI 当前运行模式切换为“${modeLabel}”吗？`)) return;
  if (!ensureNotBusy(state, renderBanners)) return;
  const finishAction = beginAction(target, "正在切换运行模式…");
  try {
    const result = await requestJson("/ai/operating-mode/select", {
      method: "POST",
      body: { mode, reason: "ui_select_ai_operating_mode" },
    });
    const runtime = result?.ai_runtime || {};
    setFlash(
      state,
      "info",
      `AI 当前运行模式已切换为 ${readableState(runtime.effective_operating_mode || mode, "目标模式")}。`,
    );
    await refreshDashboard({ manual: true });
  } catch (error) {
    setFlash(state, "danger", error instanceof Error ? error.message : String(error));
    renderBanners();
  } finally {
    finishAction();
  }
}

function readableProfileName(value, fallback = "未知档位") {
  if (value === null || value === undefined || value === "") return fallback;
  return readableState(String(value), fallback);
}

function setStrategyProfileControlMode(value, target = null) {
  if (value === "auto") {
    void restoreStrategyProfileAutomaticControl(target);
    return;
  }
  if (value === "manual") {
    void pauseStrategyProfileAutomaticControl(target);
  }
}

function operatorCanWrite() {
  const session = state.data.session || {};
  const runtimeAuth = state.data.runtime?.operator_auth || {};
  const authProviders = state.data.authProviders || {};
  if (!authProviders.auth_enabled) return Boolean(runtimeAuth.unsafe_write_without_auth);
  return session.role === "operator" || session.role === "admin" || session.identity === "api_key_write";
}

// hasResolvedAuthContext / hasResolvedPanel are PANEL-level "has any response
// ever landed?" predicates — they operate on state.data / state.errors which
// are keyed by bundle panel key. They are intentionally distinct from
// readyViews (VIEW-level) and pendingPanels (deferred fill-in). Rough usage:
//
//   - hasResolvedPanel(key)       → "this specific panel has ever produced
//                                    data or an error; safe to render it"
//   - hasResolvedAuthContext()    → specialized two-key check used by banner
//                                    and auth-gated buttons
//   - readyViews[view]            → "primary bundle for this view has landed
//                                    at least once; stale-while-revalidate
//                                    can show cached content on re-entry"
//   - pendingPanels[key]          → "a deferred fetch for this panel is in
//                                    flight; display shimmer and lock
//                                    panel-scoped buttons"
//
// Do NOT collapse these into a single concept — panel-level and view-level
// resolution are both needed and serve different renderers.
function hasResolvedAuthContext() {
  return Object.prototype.hasOwnProperty.call(state.data, "authProviders") && Object.prototype.hasOwnProperty.call(state.data, "session");
}

function hasResolvedPanel(key) {
  return Object.prototype.hasOwnProperty.call(state.data, key) || Object.prototype.hasOwnProperty.call(state.errors, key);
}

function shouldRedirectToLogin() {
  const authProviders = state.data.authProviders || {};
  const session = state.data.session || {};
  return Boolean(authProviders.auth_enabled) && !session.authenticated;
}

function controlPermissionMessage() {
  if (!hasResolvedAuthContext()) {
    return "";
  }
  const session = state.data.session || {};
  const runtimeAuth = state.data.runtime?.operator_auth || {};
  const authProviders = state.data.authProviders || {};
  if (!authProviders.auth_enabled) {
    return runtimeAuth.unsafe_write_without_auth
      ? ""
      : "当前环境不允许未认证写入，所以人工操作按钮会置灰。";
  }
  if (!session.authenticated) {
    return "当前未登录，所以恢复交易等人工操作按钮会置灰。请先用 operator 或 admin 账号登录。";
  }
  if (session.role === "viewer") {
    return "当前账号是只读 viewer，只能查看，不能执行恢复交易等人工操作。请切换为 operator 或 admin。";
  }
  return "";
}

function effectiveRecoveryReasons() {
  const recovery = state.data.systemRecovery?.recovery || {};
  const onlyReduceReasons = Array.isArray(recovery.only_reduce_reasons) ? recovery.only_reduce_reasons.filter(Boolean) : [];
  if (onlyReduceReasons.length > 0) {
    return onlyReduceReasons;
  }
  const explicitReasons = Array.isArray(recovery.resume_blocked_reasons) ? recovery.resume_blocked_reasons.filter(Boolean) : [];
  if (explicitReasons.length > 0) {
    return explicitReasons;
  }
  const blockerControl = state.data.blockerControl || {};
  if (blockerControl.primary_blocker?.blocker) {
    return [blockerControl.primary_blocker.blocker];
  }
  if (recovery.resume_eligible) {
    return [];
  }
  const blockers = Array.isArray(state.data.blockers?.blockers)
    ? state.data.blockers.blockers
        .filter((item) => item && item.blocker && item.affects_execution !== false)
        .map((item) => item.blocker)
    : [];
  if (blockers.length > 0) {
    return Array.from(new Set(blockers));
  }
  if (state.data.health?.halted) {
    return ["kill_switch_active"];
  }
  return [];
}

function localizedRecoveryReasons() {
  return listOrDash(effectiveRecoveryReasons().map((item) => localizeError(item)));
}

function activePhase1ShadowBlocker() {
  const blockerControl = state.data.blockerControl || {};
  const candidates = [];
  if (blockerControl.primary_blocker) candidates.push(blockerControl.primary_blocker);
  if (Array.isArray(blockerControl.secondary_blockers)) candidates.push(...blockerControl.secondary_blockers);
  if (Array.isArray(blockerControl.blockers)) candidates.push(...blockerControl.blockers);
  return candidates.find((item) => String(item?.blocker || "").startsWith("phase1_shadow")) || null;
}

function openDrawer({ eyebrow, title, summary, body }) {
  if (!nodes.detailDrawer || !nodes.drawerBackdrop) return;
  nodes.drawerEyebrow.textContent = eyebrow;
  nodes.drawerTitle.textContent = title;
  nodes.drawerSummary.textContent = summary;
  nodes.drawerBody.innerHTML = body;
  nodes.detailDrawer.classList.add("is-open");
  nodes.detailDrawer.setAttribute("aria-hidden", "false");
  nodes.drawerBackdrop.hidden = false;
}

function closeDrawer() {
  if (!nodes.detailDrawer || !nodes.drawerBackdrop) return;
  nodes.detailDrawer.classList.remove("is-open");
  nodes.detailDrawer.setAttribute("aria-hidden", "true");
  nodes.drawerBackdrop.hidden = true;
}

window.refreshDashboard = refreshDashboard;

