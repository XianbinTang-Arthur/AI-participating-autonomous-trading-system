import { fetchPanels, requestJson } from "./modules/api-client.js";
import { createAdminActions } from "./modules/actions/admin-actions.js";
import { fetchDashboardBundle } from "./modules/api-client.js";
import { createExecutionActionHandlers } from "./modules/actions/execution-actions.js";
import { createRiskActionHandlers } from "./modules/actions/risk-actions.js";
import { createDashboardRefreshController } from "./modules/dashboard-refresh.js";
import {
  listOrDash,
} from "./modules/formatters.js";
import { buildDecisionDrawer } from "./modules/detail-drawers.js";
import { createDashboardShellRenderer } from "./modules/shell-renderer.js";
import {
  DEFAULT_EXIT_EXECUTION_HISTORY_FILTERS,
  DEFAULT_PAGE_LIMITS,
  PAGE_LOAD_STEP,
  createState,
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
} = shellRenderer;

state.activeView = resolveViewFromLocation();
hydrateViewStateFromLocation(state.activeView);
state.loadingView = state.activeView;

refreshController = createDashboardRefreshController({
  state,
  nodes,
  fetchDashboardBundle,
  renderShell,
  renderBanners,
  applyPanelResults,
  shouldRedirectToLogin,
});
const {
  cancelScheduledRefresh,
  handleVisibilityChange,
  isBackgroundRefreshingView,
  isBootstrapping,
  isViewFresh,
  refreshDashboard,
  scheduleRefresh,
  shouldRenderLoadingState,
} = refreshController;

const riskActionHandlers = createRiskActionHandlers({
  activeExitExecutionHistoryState,
  activeExitExecutionHistoryView,
  activePhase1ShadowBlocker,
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
  setActionPending,
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
    setActiveView(nextView, { refresh: true });
  });

  window.addEventListener("pageshow", (event) => {
    if (!event.persisted) return;
    const nextView = resolveViewFromLocation();
    hydrateViewStateFromLocation(nextView);
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
  if (refresh) {
    if (changed && isViewFresh(nextView) && state.readyViews[nextView]) {
      // Data is very fresh — skip network request entirely
      state.loadingView = null;
      renderActiveView();
    } else {
      // Stale-while-revalidate: render cached data immediately, then refresh
      if (changed && state.readyViews[nextView]) {
        state.loadingView = null;
      }
      void refreshDashboard();
    }
  }
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
    if (state.pendingRefresh && !state.refreshing) {
      state.pendingRefresh = false;
      void refreshDashboard();
      return;
    }
    scheduleRefresh();
  };
}

async function runAction(path, body, successMessage, { target = null, pendingLabel = "正在提交请求…" } = {}) {
  if (state.actionInFlight) return;
  const finishAction = beginAction(target, pendingLabel);
  try {
    const result = await requestJson(path, { method: "POST", body });
    state.flash = { tone: "info", message: result?.message || successMessage };
    await refreshDashboard({ manual: true });
  } catch (error) {
    state.flash = { tone: "danger", message: error instanceof Error ? error.message : String(error) };
    renderBanners();
  } finally {
    finishAction();
  }
}

async function runDangerousAction({ path, body, successMessage, confirmMessage, target = null, pendingLabel = "正在提交请求…" }) {
  if (!window.confirm(confirmMessage)) return;
  await runAction(path, body, successMessage, { target, pendingLabel });
}


async function logoutOperator() {
  try {
    await requestJson("/auth/logout", { method: "POST" });
    window.location.assign("/login");
  } catch (error) {
    state.flash = { tone: "danger", message: error instanceof Error ? error.message : String(error) };
    renderBanners();
  }
}

async function dispatchAction(action, value, target = null) {
  if (action === "refresh-dashboard") return refreshDashboard({ manual: true });
  if (action === "navigate-view") {
    const nextView = resolveKnownView(value);
    if (state.activeView === nextView) {
      state.flash = { tone: "info", message: `当前已在${VIEW_LABELS[nextView] || "当前页面"}，已刷新当前状态。` };
      renderBanners();
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
    state.flash = { tone: "danger", message: error instanceof Error ? error.message : String(error) };
    renderBanners();
  }
}

async function adjustPageLimit(key, delta) {
  const current = Number(state.pageLimits?.[key] || DEFAULT_PAGE_LIMITS[key] || 0);
  state.pageLimits[key] = current + delta;
  await refreshDashboard();
}

async function resetPageLimit(key) {
  state.pageLimits[key] = DEFAULT_PAGE_LIMITS[key] || state.pageLimits[key];
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

async function applyExitExecutionHistoryWorkspaceFilters(target = null) {
  const historyState = activeExitExecutionHistoryState();
  historyState.offset = 0;
  syncExitExecutionHistoryFiltersAcrossViews(activeExitExecutionHistoryView());
  if (state.activeView === "exitExecution") {
    syncActiveViewLocationState({ pushHistory: false });
  }
  await refreshDashboard({ manual: true });
  scrollExitExecutionWorkspaceIntoView(target);
}

async function resetExitExecutionHistoryWorkspaceFilters(target = null) {
  const riskHistoryState = ensureExitExecutionHistoryState("risk");
  const exitExecutionHistoryState = ensureExitExecutionHistoryState("exitExecution");
  Object.assign(riskHistoryState, DEFAULT_EXIT_EXECUTION_HISTORY_FILTERS, { offset: 0 });
  Object.assign(exitExecutionHistoryState, DEFAULT_EXIT_EXECUTION_HISTORY_FILTERS, { offset: 0 });
  if (state.activeView === "exitExecution") {
    syncActiveViewLocationState({ pushHistory: false });
  }
  syncExitExecutionHistoryFilterRoots();
  await refreshDashboard({ manual: true });
  scrollExitExecutionWorkspaceIntoView(target);
}

async function paginateExitExecutionHistory(direction, target = null) {
  const historyState = activeExitExecutionHistoryState();
  const limit = Math.max(Number(historyState.limit) || 20, 1);
  const currentOffset = Math.max(Number(historyState.offset) || 0, 0);
  let nextOffset = currentOffset;
  if (direction === "next") {
    nextOffset = currentOffset + limit;
  } else if (direction === "prev") {
    nextOffset = Math.max(currentOffset - limit, 0);
  } else {
    nextOffset = 0;
  }
  historyState.offset = nextOffset;
  if (state.activeView === "exitExecution") {
    syncActiveViewLocationState({ pushHistory: false });
  }
  await refreshDashboard({ manual: true });
  scrollExitExecutionWorkspaceIntoView(target);
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
  const clearPending = setActionPending(target, "正在切换策略档位…");
  try {
    if (!window.confirm(`确认立即切换到“${profileLabel}”这个已注册策略档位吗？`)) return;
    const result = await requestJson(`/strategy-profiles/profiles/${encodeURIComponent(profileId)}/activate`, {
      method: "POST",
      body: { reason: "ui_manual_activate_strategy_profile" },
    });
    state.flash = {
      tone: "info",
      message: `当前策略档位已手动切换为 ${readableProfileName(result?.active_revision?.profile_label || result?.active_revision?.profile_id)}。`,
    };
    await refreshDashboard({ manual: true });
  } catch (error) {
    state.flash = { tone: "danger", message: error instanceof Error ? error.message : String(error) };
    renderBanners();
  } finally {
    clearPending();
  }
}

async function restoreStrategyProfileAutomaticControl(target = null) {
  const clearPending = setActionPending(target, "正在恢复自动切档…");
  try {
    if (!window.confirm("确认开启自动切档吗？开启后下面 6 个档位按钮会锁定，由系统自动决定是否换档。")) return;
    const result = await requestJson("/strategy-profiles/restore-auto", {
      method: "POST",
      body: { reason: "ui_restore_auto_strategy_profile_control" },
    });
    const activation = result?.activation || {};
    state.flash = {
      tone: "info",
      message: activation?.active_profile_id
        ? `策略档位已恢复自动切档逻辑，当前仍保持 ${readableProfileName(result?.active_revision?.profile_label || activation.active_profile_id)}。`
        : "策略档位已恢复自动切档逻辑。",
    };
    await refreshDashboard({ manual: true });
  } catch (error) {
    state.flash = { tone: "danger", message: error instanceof Error ? error.message : String(error) };
    renderBanners();
  } finally {
    clearPending();
  }
}

async function pauseStrategyProfileAutomaticControl(target = null) {
  const clearPending = setActionPending(target, "正在切到手动切档…");
  try {
    if (!window.confirm("确认关闭自动切档吗？关闭后下面 6 个档位按钮会解锁，由你手动切换。")) return;
    const result = await requestJson("/strategy-profiles/pause-auto", {
      method: "POST",
      body: { reason: "ui_pause_auto_strategy_profile_control" },
    });
    const activation = result?.activation || {};
    state.flash = {
      tone: "info",
      message: activation?.active_profile_id
        ? `当前已切到手动切档，系统会保持 ${readableProfileName(result?.active_revision?.profile_label || activation.active_profile_id)}。`
        : "当前已切到手动切档。",
    };
    await refreshDashboard({ manual: true });
  } catch (error) {
    state.flash = { tone: "danger", message: error instanceof Error ? error.message : String(error) };
    renderBanners();
  } finally {
    clearPending();
  }
}

async function selectAIOperatingMode(mode, target = null) {
  if (!mode) return;
  const modeLabel = target instanceof HTMLElement ? (target.textContent || "").trim() : mode;
  const clearPending = setActionPending(target, "正在切换运行模式…");
  try {
    if (!window.confirm(`确认立即把 AI 当前运行模式切换为“${modeLabel}”吗？`)) return;
    const result = await requestJson("/ai/operating-mode/select", {
      method: "POST",
      body: { mode, reason: "ui_select_ai_operating_mode" },
    });
    const runtime = result?.ai_runtime || {};
    state.flash = {
      tone: "info",
      message: `AI 当前运行模式已切换为 ${readableState(runtime.effective_operating_mode || mode, "目标模式")}。`,
    };
    await refreshDashboard({ manual: true });
  } catch (error) {
    state.flash = { tone: "danger", message: error instanceof Error ? error.message : String(error) };
    renderBanners();
  } finally {
    clearPending();
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

function defaultBlockerActionReason(actionId) {
  const map = {
    "reconcile-now": "operator_validate_from_blocker_panel",
    "accept-rebaseline": "operator_rebaseline_from_blocker_panel",
    "resume-system": "operator_resume_from_blocker_panel",
    "halt-system": "operator_keep_halted_from_blocker_panel",
    "refresh-exchange-state": "operator_refresh_exchange_state_from_blocker_panel",
    "acknowledge-phase1-shadow": "operator_review_phase1_shadow_from_blocker_panel",
    "ai-review-restore": "operator_restore_ai_from_blocker_panel",
    "ai-review-degrade-to-baseline": "operator_degrade_to_baseline_from_blocker_panel",
  };
  return map[actionId] || `operator_${actionId}`;
}

function blockerActionPendingLabel(actionId) {
  const map = {
    "reconcile-now": "正在重新对账…",
    "accept-rebaseline": "正在确认新基线…",
    "resume-system": "正在恢复自动运行…",
    "halt-system": "正在保持暂停状态…",
    "refresh-exchange-state": "正在刷新交易所状态…",
    "acknowledge-phase1-shadow": "正在记录影子核查结果…",
    "ai-review-restore": "正在恢复 AI 决策…",
    "ai-review-degrade-to-baseline": "正在切到仅基础策略运行…",
  };
  return map[actionId] || "正在执行阻断处理动作…";
}

function blockerActionSuccessMessage(actionId) {
  const map = {
    "reconcile-now": "对账已刷新。",
    "accept-rebaseline": "新基线已确认。",
    "resume-system": "恢复自动运行请求已提交。",
    "halt-system": "系统会继续保持暂停状态。",
    "refresh-exchange-state": "交易所状态已刷新。",
    "acknowledge-phase1-shadow": "已记录影子兼容层人工核查结果。",
    "ai-review-restore": "AI 复核已处理，已恢复 AI 决策资格。",
    "ai-review-degrade-to-baseline": "AI 复核已处理，系统将以仅基础策略继续运行。",
  };
  return map[actionId] || "阻断处理动作已完成。";
}

function blockerActionConfirmMessage(actionId) {
  const map = {
    "accept-rebaseline": "确认把当前状态接受为新基线吗？这会覆盖旧的恢复参照。",
    "halt-system": "确认继续保持暂停状态吗？这会阻止系统继续自动交易。",
    "acknowledge-phase1-shadow": "确认已完成人工核查吗？这会留下当前影子兼容层状态记录，但不会解除阻断。",
    "ai-review-restore": "确认恢复 AI 决策链路吗？这会清除当前 AI 结果复核阻断。",
    "ai-review-degrade-to-baseline": "确认改为仅基础策略继续运行吗？这会解除当前 AI 复核阻断，并把 AI 决策权降为仅基础策略。",
  };
  return map[actionId] || "";
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

