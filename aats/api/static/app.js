// #1 修复：api-client.js 原本被 import 了两次（fetchPanels/requestJson 一行，
// fetchDashboardBundle 另一行），中间还夹着 admin-actions，显然是陆续加新功能时
// 在头部追加的，没有人回头合并。这里按"同模块合并 + 分组注释"整理一次：
// 1) 底层传输层（api-client / formatters / terms）
// 2) 领域操作（actions/*、dashboard-refresh）
// 3) 应用层（flash、store、navigation-state、shell-renderer）
// 4) 视图模块（views/*）
// 后续要加 import 时按分组追加，不要再像以前那样散着放。

// --- 底层传输 / 通用 helper ---
import { fetchDashboardBundle, fetchPanels, requestJson } from "./modules/api-client.js";
import { listOrDash } from "./modules/formatters.js";
import {
  localizeError,
  operationalStatusCopy,
  readableState,
} from "./modules/terms.js";

// --- 领域 action handler ---
import { createAdminActions } from "./modules/actions/admin-actions.js";
import { createExecutionActionHandlers } from "./modules/actions/execution-actions.js";
import { createRdpActionHandlers } from "./modules/actions/rdp-actions.js";
import { createRiskActionHandlers } from "./modules/actions/risk-actions.js";

// --- 应用层（流程 / 状态 / 渲染壳） ---
import { createDashboardRefreshController } from "./modules/dashboard-refresh.js";
import { buildDecisionDrawer } from "./modules/detail-drawers.js";
import { runDevSelfChecks } from "./modules/dev-self-check.js";
import { ensureNotBusy, setFlash } from "./modules/flash.js";
import {
  EXIT_EXECUTION_HISTORY_ACTION_FILTERS,
  EXIT_EXECUTION_HISTORY_WINDOW_FILTERS,
  coerceReplayParentFilter,
  createNavigationStateController,
  exitExecutionHistoryWindowThresholdMs,
  normalizeExitExecutionHistoryFilterValue,
} from "./modules/navigation-state.js";
import { createDashboardShellRenderer } from "./modules/shell-renderer.js";
import {
  DEFAULT_EXIT_EXECUTION_HISTORY_FILTERS,
  DEFAULT_PAGE_LIMITS,
  EXIT_EXECUTION_FILTER_AFFECTED_VIEWS,
  PAGE_LIMIT_AFFECTED_VIEWS,
  PAGE_LOAD_STEP,
  createState,
  viewSpecs,
  invalidateCachedViews,
} from "./modules/store.js";
import {
  VIEW_LABELS,
  resolveKnownView,
  resolveViewFromLocation,
} from "./modules/view-router.js";

// --- 视图模块 ---
import { renderAIAnalysisView } from "./modules/views/ai-analysis-view.js";
import { renderAIConfigView } from "./modules/views/ai-config-view.js";
import { renderAdminView } from "./modules/views/admin-view.js";
import { renderExecutionSections, renderExecutionView } from "./modules/views/execution-view.js";
import { renderExitExecutionView } from "./modules/views/exit-execution-view.js";
import { renderHomeView } from "./modules/views/home-view.js";
import { renderOverviewView } from "./modules/views/overview-view.js";
import { renderProtectedAuthBlockedView } from "./modules/views/protected-auth-view.js";
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
  isProtectedViewAuthBlocked,
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
const rdpActionHandlers = createRdpActionHandlers({
  beginAction,
  renderBanners,
  refreshDashboard,
  requestJson,
  state,
});

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

  // #41 修复：原本这里写 form.id === "operatorCreateForm"，是整个前端唯一一处
  // 用 DOM id 做事件路由的入口。改成读 form.dataset.action（管理表单已经在
  // admin-view.js 里写成 <form data-action="submit-create-operator">），和其它
  // 按钮 / 表单的分发约定保持一致；未来再加一个 admin 表单，只要新增一个
  // case 即可，不再需要 DOM id 字符串匹配。
  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    const formAction = form.dataset?.action || "";
    if (formAction === "submit-create-operator") {
      event.preventDefault();
      void adminActions.createOperatorUser();
    }
  });

  document.addEventListener("input", handleExitExecutionHistoryFilterEvent);
  document.addEventListener("change", handleExitExecutionHistoryFilterEvent);
}

function applyPanelResults(results) {
  const normalizedResults =
    results && typeof results === "object" && typeof results.panels === "object" && results.panels !== null
      ? results
      : { panels: results || {}, auth: null };
  for (const [key, result] of Object.entries(normalizedResults.panels || {})) {
    state.data[key] = result.data;
    state.errors[key] = result.error;
  }
  if (Object.prototype.hasOwnProperty.call(normalizedResults, "auth")) {
    state.bundleAuth = normalizedResults.auth || null;
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

  if (isProtectedViewAuthBlocked(state.activeView)) {
    renderProtectedAuthBlockedCurrentView();
    return;
  }

  const viewData = {
    ...state.data,
    errors: state.errors,
    uiState: state.ui,
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
    // 历史回归修复：a5218fb 在 ai-config-view.js 里新增了 rdp*Workbench* /
    // rdp*Tuning* / errors / authProviders 依赖（`resolveRdpAuthError` 要
    // 看 errors + authProviders），并把 uiState 的契约改成 `data.uiState?.aiConfig`；
    // 但当时忘了同步这里的调用方，所以无论后端返回什么，workbench/tuning
    // 相关字段一律是 undefined→{}，Object.keys 长度永远 = 0，`renderAIConfigView`
    // 就恒定走到“RDP 数据暂未就绪”callout。这里把 5 个 workbench/tuning panel、
    // errors 和 authProviders 都透传过去，并用 state.ui 整体作为 uiState，
    // 让视图内部的 `data.uiState?.aiConfig` 契约成立。
    patchHtml(
      nodes.aiConfigContent,
      renderAIConfigView({
        session: state.data.session || {},
        authProviders: state.data.authProviders || {},
        aiRuntime: state.data.aiRuntime || {},
        summary: state.data.aiConfigModel || {},
        rdpControl: state.data.rdpControl || {},
        rdpWorkbenchOverview: state.data.rdpWorkbenchOverview || {},
        rdpWorkbenchItems: state.data.rdpWorkbenchItems || {},
        rdpWorkbenchAlerts: state.data.rdpWorkbenchAlerts || {},
        rdpTuningOverview: state.data.rdpTuningOverview || {},
        rdpTuningProposals: state.data.rdpTuningProposals || {},
        error: state.errors.aiConfigModel || null,
        errors: state.errors,
        uiState: state.ui,
      }),
    );
    return;
  }
  if (state.activeView === "admin" && nodes.adminContent) {
    patchHtml(nodes.adminContent, renderAdminView(viewData));
  }
}

const PROTECTED_DASHBOARD_VIEWS = new Set([
  "home",
  "overview",
  "strategy",
  "execution",
  "risk",
  "exitExecution",
  "replay",
  "aiAnalysis",
  "aiConfig",
  "admin",
]);

const DASHBOARD_AUTH_ERROR_CODES = [
  "operator_auth_required",
  "operator_write_auth_required",
  "operator_write_access_required",
  "operator_admin_access_required",
  "operator_https_required_for_secure_session",
];

function currentBundleAuthSummary() {
  return state.bundleAuth || null;
}

function rawAuthErrorCode(value) {
  const text = typeof value === "string" ? value : "";
  if (!text) return null;
  const match = DASHBOARD_AUTH_ERROR_CODES.find((code) => text === code || text === localizeError(code));
  return match || null;
}

function viewOwnedPanelKeys(view = state.activeView) {
  return viewSpecs(view, state).map(([key]) => key);
}

function currentViewAuthErrorCode(view = state.activeView) {
  const auth = currentBundleAuthSummary();
  if (PROTECTED_DASHBOARD_VIEWS.has(view)) {
    const summaryError = rawAuthErrorCode(auth?.auth_blocked_reason) || rawAuthErrorCode(auth?.primary_error);
    if (summaryError && (auth?.access_state === "transport_blocked" || auth?.access_state === "auth_required")) {
      return summaryError;
    }
  }
  const panelKey = viewOwnedPanelKeys(view).find((key) => rawAuthErrorCode(state.errors[key]));
  return panelKey ? rawAuthErrorCode(state.errors[panelKey]) : null;
}

function isProtectedViewAuthBlocked(view = state.activeView) {
  if (!PROTECTED_DASHBOARD_VIEWS.has(view)) return false;
  const auth = currentBundleAuthSummary();
  if (auth?.access_state === "transport_blocked" || auth?.access_state === "auth_required") {
    return true;
  }
  return Boolean(currentViewAuthErrorCode(view));
}

function activeViewContainerNode(view = state.activeView) {
  if (view === "home") return nodes.homeContent;
  if (view === "overview") return nodes.overviewContent;
  if (view === "strategy") return nodes.strategyContent;
  if (view === "execution") return nodes.executionContent;
  if (view === "risk") return nodes.riskContent;
  if (view === "exitExecution") return nodes.exitExecutionContent;
  if (view === "replay") return nodes.replayContent;
  if (view === "aiAnalysis") return nodes.aiAnalysisContent;
  if (view === "aiConfig") return nodes.aiConfigContent;
  if (view === "admin") return nodes.adminContent;
  return null;
}

function renderProtectedAuthBlockedCurrentView() {
  const container = activeViewContainerNode();
  if (!container) return;
  patchHtml(
    container,
    renderProtectedAuthBlockedView({
      viewLabel: VIEW_LABELS[state.activeView] || "当前页面",
      authSummary: currentBundleAuthSummary() || {},
      session: state.data.session || {},
      authProviders: state.data.authProviders || {},
    }),
  );
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


// #4 修复：原本这里用一个本地的 logoutInFlight 作为互斥锁，旁边配了一大段
// 英文注释，但没讲清楚：
//   1) 为什么不走常规的 ensureNotBusy() / beginAction() 路线；
//   2) 成功路径为什么故意把锁留在 true 而不是放开；
//   3) 为什么 shell-renderer 要刻意让 logoutButton 在其他动作忙时也可点。
// 现在把这三点补齐（并翻译成项目约定的中文）。未来再有人动这段要很确信：
// 这是一个"有意设计的单向锁"，不是 ensureNotBusy 的漏写。
//
// ── 为什么不走 ensureNotBusy/beginAction：
// beginAction 会置位全局 state.actionInFlight，阻止其他按钮点击直到 finishAction。
// 但登出就是要立刻跳走页面，没有 finishAction 这一步：如果成功路径不调 finishAction，
// 全局锁就永远挂住；如果调，又破坏了我们想要的"锁死按钮到跳页为止"语义。
// 所以用一个完全本地的 latch 替代。
//
// ── 为什么 logoutButton 在忙时还要可点：
// shell-renderer.js 的 refreshButtonStates 明确跳过了 logoutButton，让它永远启用。
// 理由：用户若在一个长轮询/慢请求里点错了按钮想登出，不能因为上一个动作还没回来
// 就锁死登出入口——登出本身就是一种"放弃当前 pending 动作"的兜底。
//
// ── 为什么成功路径不重置 latch：
// location.assign("/login") 并不是立刻跳转，浏览器可能慢上数百毫秒。如果在此期间
// 把 latch 清零，用户连点两次就能发两次 POST /auth/logout，第二次会带着已经失效
// 的 session cookie 命中 401 处理分支。故意留 true 直到页面被替换为止。
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
    // 失败路径必须放开 latch，否则用户点一次失败以后就再也登不出了。只有成功
    // 路径才刻意保留 latch（见上方说明）。
    logoutInFlight = false;
  }
}

// #3 修复：原本 dispatchAction 是一长串 if-return，末尾 domainHandler 找不到时
// 直接 fallthrough 到函数末尾，默认 return undefined，没有任何提示。一旦某个
// view 模板里的 data-action 拼错（例如 "collapse-ai-shadwo-decisions"），按钮
// 点了不会报错也不会动，只能靠盲测发现。这里做两件事：
//   1) 把本地 action 映射集中到一个 dispatch table，末尾再 fallback 到
//      risk/execution/admin 三个领域 handler 表；
//   2) 所有表都查不到时打一条 console.warn，把 action 名、value、target 一起
//      记下来，方便在 devtools 里直接定位是哪个按钮拼错了。
const LOCAL_DISPATCH_ACTIONS = {
  "refresh-dashboard": () => refreshDashboard({ manual: true }),
  "navigate-view": (value) => navigateToView(value),
  "inspect-decision": (value) => inspectDecision(value),
  "select-ai-operating-mode": (value, target) => selectAIOperatingMode(value, target),
  "manual-activate-strategy-profile": (value, target) => activateStrategyProfile(value, target),
  "restore-strategy-profile-auto": (_value, target) => restoreStrategyProfileAutomaticControl(target),
  "pause-strategy-profile-auto": (_value, target) => pauseStrategyProfileAutomaticControl(target),
  "set-profile-control-mode": (value, target) => setStrategyProfileControlMode(value, target),
  "load-more-decisions": () => adjustPageLimit("recentDecisions", PAGE_LOAD_STEP),
  "collapse-decisions": () => resetPageLimit("recentDecisions"),
  "load-more-ai-assessments": () => adjustPageLimit("recentAIAssessments", PAGE_LOAD_STEP),
  "collapse-ai-assessments": () => resetPageLimit("recentAIAssessments"),
  "load-more-ai-shadow-decisions": () => adjustPageLimit("recentAIShadowDecisions", PAGE_LOAD_STEP),
  "collapse-ai-shadow-decisions": () => resetPageLimit("recentAIShadowDecisions"),
  "load-more-ai-shadow-evaluations": () => adjustPageLimit("recentAIShadowEvaluations", PAGE_LOAD_STEP),
  "collapse-ai-shadow-evaluations": () => resetPageLimit("recentAIShadowEvaluations"),
  "load-more-replay-validations": () => adjustPageLimit("recentReplayValidations", PAGE_LOAD_STEP),
  "collapse-replay-validations": () => resetPageLimit("recentReplayValidations"),
  "set-replay-parent-filter": (value) => setReplayParentFilter(value),
};

function navigateToView(value) {
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

async function dispatchAction(action, value, target = null) {
  const localHandler = LOCAL_DISPATCH_ACTIONS[action];
  if (localHandler) {
    return localHandler(value, target);
  }
  const domainHandler = riskActionHandlers[action] || executionActionHandlers[action] || adminActionHandlers[action] || rdpActionHandlers[action];
  if (domainHandler) {
    return domainHandler(value, target);
  }
  // 查不到任何 handler = data-action 很可能是拼错了或者新模板忘记注册。
  // 与其静默返回，不如明确打一条警告方便排查。生产环境只打 console（避免给
  // 终端用户看到调试信号），dev 环境下额外触发一条可视 banner，让开发者第
  // 一时间注意到 typo 而不是要去翻 devtools。dev mode 判定见 isDebugMode()。
  console.warn("[dispatchAction] unknown action", { action, value, target });
  if (isDebugMode()) {
    setFlash(state, "warning", `[dev] 未注册的 data-action：${action}（请检查模板拼写或 dispatch 表注册）。`);
    renderBanners();
  }
  return undefined;
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
  //
  // 完整的契约规范（含三种竞态场景的可观测后果）见
  // modules/actions/risk-actions.js::confirmResume 的 #28 修复注释。本处的
  // 三行 bullet 是它的精简引用——任何实现细节有疑问时请去那边读完整版，
  // 不要在两边各写一份，避免漂移。
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
  if (currentViewAuthErrorCode()) return false;
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
//   - pendingPanels[key]          → "a primary or deferred fetch for this
//                                    panel is in flight; display shimmer and
//                                    lock panel-scoped buttons until the
//                                    fetch resolves and the entry is cleared"
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
  return false;
}

function controlPermissionMessage() {
  const authBlockedCode = currentViewAuthErrorCode();
  if (authBlockedCode) {
    return localizeError(authBlockedCode);
  }
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

// #2 修复：原本无条件挂 `window.refreshDashboard = refreshDashboard`，相当于一个全局调试
// 后门——任意脚本、第三方注入、甚至用户在 DevTools 里敲一行都能绕过刷新节流。生产环境
// 应该只在显式开启调试标志时才暴露这个入口。这里用三重条件：
//   1. URL 含 `?debug=1` —— 开发/排障时临时启用；
//   2. localStorage 里手动写 `aats-debug=1` —— 开发者的持久化偏好；
//   3. 宿主域名是本地/回环 —— 开发环境缺省开启。
// 任一条件成立才挂载；生产部署下默认不暴露，也可以通过切换 localStorage flag 临时启用。
//
// 抽出 isDebugMode() 是因为同一组判定也被 dispatchAction 的"unknown action"分支
// 复用：dev 环境下不仅打 console.warn，还会触发一条 banner 提示，让 typo 的
// data-action 名第一时间被开发者注意到（生产环境保持静默以免泄露调试信号）。
function isDebugMode() {
  try {
    const params = new URLSearchParams(window.location.search || "");
    if (params.get("debug") === "1") return true;
    try {
      if (window.localStorage?.getItem("aats-debug") === "1") return true;
    } catch (storageError) {
      // localStorage 在某些隐私模式下会抛，不能视为致命。
      // eslint-disable-next-line no-console
      console.warn("[app] 读取 aats-debug localStorage 失败", storageError);
    }
    const host = window.location.hostname || "";
    return host === "localhost" || host === "127.0.0.1" || host === "::1" || host.endsWith(".local");
  } catch (error) {
    // 任何意外都按"非 debug"处理，避免 dev-only 路径误开。
    // eslint-disable-next-line no-console
    console.warn("[app] isDebugMode 判定失败", error);
    return false;
  }
}

(function installDebugHandle() {
  try {
    if (isDebugMode()) {
      window.refreshDashboard = refreshDashboard;
      // eslint-disable-next-line no-console
      console.info("[app] 调试入口 window.refreshDashboard 已挂载（仅在 debug 模式下可用）。");
      // dev mode 下顺便跑一次"production-safe 断言"，锁住 #21 / #22 这种
      // "看起来像 dead code 但其实是有意保留的 kludge"。生产环境完全跳过。
      // 任何失败既会进 console.error，也会触发一条 banner（不会阻塞渲染）。
      try {
        const result = runDevSelfChecks();
        if (result.failed > 0 && result.firstFailureMessage) {
          setFlash(state, "warning", `[dev] self-check 有 ${result.failed} 条断言失败：${result.firstFailureMessage}`);
          // 这个 IIFE 在 init() 之前求值，state 可能还没渲染过 banner——
          // 但 banner 是 sticky-flash 协议的一部分，下一次 renderBanners()
          // 会自然带上这条警告，无需在这里强行同步触发渲染。
        }
      } catch (selfCheckError) {
        // self-check 自身的 bug 不能阻塞 app 启动。
        // eslint-disable-next-line no-console
        console.warn("[app] dev self-check 跑炸了", selfCheckError);
      }
    }
  } catch (error) {
    // 挂调试入口失败不应阻塞主流程。
    // eslint-disable-next-line no-console
    console.warn("[app] 安装调试入口失败", error);
  }
})();


