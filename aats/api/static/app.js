import { fetchPanels, requestJson } from "./modules/api-client.js";
import { notice, pill, primaryStatusPanel } from "./modules/components.js";
import { fetchDashboardBundle } from "./modules/api-client.js";
import { textOrFallback } from "./modules/copy.js";
import { syncRefreshDisabledButtons } from "./modules/refresh-interactivity.js";
import {
  emptyState,
  formatMaybeTimestamp,
  formatNumber,
  formatRelativeAge,
  listOrDash,
  middleEllipsis,
} from "./modules/formatters.js";
import {
  buildDecisionDrawer,
  buildFillDrawer,
  buildOrderDrawer,
  buildReconciliationDrawer,
} from "./modules/detail-drawers.js";
import { buildPhase1ShadowDrawer } from "./modules/shadow-drawer.js";
import {
  AUTO_REFRESH_MS,
  CORE_SPECS,
  DEFAULT_EXIT_EXECUTION_HISTORY_FILTERS,
  DEFAULT_EXIT_EXECUTION_HISTORY_PAGING,
  DEFAULT_PAGE_LIMITS,
  PAGE_LOAD_STEP,
  buildDashboardBundleRequestPlan,
  createState,
  viewSpecs,
} from "./modules/store.js";
import {
  readableFamilyExecutionSummary,
  localizeError,
  operationalStatusCopy,
  operationalStatusHeadline,
  readableOverlayParentSignalSummary,
  readableState,
  reviewStatusLabel,
  toneForOrderStatus,
  toneForRuntimeState,
  tradingStatusLabel,
} from "./modules/terms.js";
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

const VIEW_ROUTES = {
  home: "/ui",
  overview: "/ui/overview",
  strategy: "/ui/strategy",
  execution: "/ui/execution",
  risk: "/ui/risk",
  exitExecution: "/ui/exit-execution",
  replay: "/ui/replay",
  aiAnalysis: "/ui/ai-analysis",
  aiConfig: "/ui/ai-config",
  admin: "/ui/settings",
};

const VIEW_META = {
  home: {
    docTitle: "AATS 自动交易监控台 | 主页",
    eyebrow: "主页",
    heading: "系统主控台",
    copy: "这里集中查看状态、执行路径和最新风险提示。",
    hidePageHead: true,
  },
  overview: {
    docTitle: "AATS 自动交易监控台 | 交易总览",
    eyebrow: "交易总览",
    heading: "交易总览",
    copy: "",
    hidePageHead: false,
  },
  strategy: {
    docTitle: "AATS 自动交易监控台 | 策略判断",
    eyebrow: "策略解释",
    heading: "为什么现在不做或要做这笔交易",
    copy: "先看策略结论，再看门禁和阻断原因。",
    hidePageHead: false,
  },
  execution: {
    docTitle: "AATS 自动交易监控台 | 委托与成交",
    eyebrow: "委托与成交",
    heading: "",
    copy: "查看最近委托、成交、报错和卡单，确认执行链路有没有真正闭环。",
    hidePageHead: false,
  },
  risk: {
    docTitle: "AATS 自动交易监控台 | 风险与恢复",
    eyebrow: "风险、对账、恢复",
    heading: "系统现在为什么能交易或不能交易",
    copy: "关注阻断原因、对账结论、恢复状态、账户快照和是否需要人工确认。",
    hidePageHead: false,
  },
  exitExecution: {
    docTitle: "AATS 自动交易监控台 | 退出任务工作台",
    eyebrow: "退出任务工作台",
    heading: "独立排查 parent-exit 的处理历史与剩余阻断",
    copy: "这里专门查看 parent-exit 的长历史、分页和可恢复人工动作，不再和风险页的其他卡片混在一起。",
    hidePageHead: false,
  },
  replay: {
    docTitle: "AATS 自动交易监控台 | 回放与复盘",
    eyebrow: "回放与复盘",
    heading: "Replay 工作区",
    copy: "这里专门对读 replay 父腿复盘、历史校验和腿级对账异常。",
    hidePageHead: false,
  },
  aiAnalysis: {
    docTitle: "AATS 自动交易监控台 | AI 分析",
    eyebrow: "AI 分析",
    heading: "先看 AI 怎么运行，再看它有没有价值",
    copy: "这里集中展示 AI 当前状态、决策解释、策略层 shadow、执行层 shadow 和长期表现。",
    hidePageHead: false,
  },
  aiConfig: {
    docTitle: "AATS 自动交易监控台 | AI 配置",
    eyebrow: "AI 配置",
    heading: "这里管理 AI 决策模式与策略换档方式",
    copy: "左侧决定 AI 在交易里扮演什么角色，右侧决定 6 个策略档位是自动切换还是手动固定。",
    hidePageHead: false,
  },
  admin: {
    docTitle: "AATS 自动交易控制台 | 账户与权限",
    eyebrow: "控制面",
    heading: "账户与权限工作区",
    copy: "这里专门处理登录、角色、账号启停和控制台访问权限。",
    hidePageHead: false,
  },
};

const VIEW_LABELS = {
  home: "主页",
  overview: "交易总览",
  strategy: "策略判断",
  execution: "委托与成交",
  risk: "风险与恢复",
  exitExecution: "退出任务工作台",
  replay: "回放与复盘",
  aiAnalysis: "AI 分析",
  aiConfig: "AI 配置",
  admin: "账户与权限",
};

const EXIT_EXECUTION_HISTORY_ACTION_FILTERS = new Set(["all", "refresh_exchange_state", "retry_limit_lookup", "safe_cancel"]);
const EXIT_EXECUTION_HISTORY_WINDOW_FILTERS = new Set(["all", "1", "6", "24", "168", "720"]);

const state = createState();
state.activeView = resolveViewFromLocation();
hydrateViewStateFromLocation(state.activeView);
state.loadingView = state.activeView;

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
const renderCache = new WeakMap();

function ensureExitExecutionHistoryState(view = "risk") {
  if (view === "exitExecution") {
    state.ui.exitExecution = state.ui.exitExecution || {};
    state.ui.exitExecution.exitExecutionHistory = {
      ...DEFAULT_EXIT_EXECUTION_HISTORY_FILTERS,
      ...DEFAULT_EXIT_EXECUTION_HISTORY_PAGING.exitExecution,
      ...(state.ui.exitExecution.exitExecutionHistory || {}),
    };
    return state.ui.exitExecution.exitExecutionHistory;
  }
  state.ui.risk = state.ui.risk || {};
  state.ui.risk.exitExecutionHistory = {
    ...DEFAULT_EXIT_EXECUTION_HISTORY_FILTERS,
    ...DEFAULT_EXIT_EXECUTION_HISTORY_PAGING.risk,
    ...(state.ui.risk.exitExecutionHistory || {}),
  };
  return state.ui.risk.exitExecutionHistory;
}

function copyExitExecutionHistoryFilters(source, target) {
  target.action = String(source?.action || "all");
  target.parent = String(source?.parent || "");
  target.actor = String(source?.actor || "");
  target.windowHours = String(source?.windowHours || "all");
}

function syncExitExecutionHistoryFiltersAcrossViews(sourceView = "risk") {
  const sourceState = ensureExitExecutionHistoryState(sourceView);
  const riskState = ensureExitExecutionHistoryState("risk");
  const exitExecutionState = ensureExitExecutionHistoryState("exitExecution");
  copyExitExecutionHistoryFilters(sourceState, riskState);
  copyExitExecutionHistoryFilters(sourceState, exitExecutionState);
  if (sourceView !== "risk") {
    riskState.offset = 0;
  }
  if (sourceView !== "exitExecution") {
    exitExecutionState.offset = 0;
  }
}

function activeExitExecutionHistoryView() {
  return state.activeView === "exitExecution" ? "exitExecution" : "risk";
}

function activeExitExecutionHistoryState() {
  return ensureExitExecutionHistoryState(activeExitExecutionHistoryView());
}

function readExitExecutionHistoryStateFromLocation() {
  const params = new URLSearchParams(window.location.search || "");
  const parsed = {
    ...DEFAULT_EXIT_EXECUTION_HISTORY_FILTERS,
    ...DEFAULT_EXIT_EXECUTION_HISTORY_PAGING.exitExecution,
  };
  const action = String(params.get("action") || "").trim();
  if (EXIT_EXECUTION_HISTORY_ACTION_FILTERS.has(action)) {
    parsed.action = action;
  }
  const parentIntentId = String(params.get("parent_intent_id") || "").trim();
  if (parentIntentId) {
    parsed.parent = parentIntentId;
  }
  const actor = String(params.get("actor") || "").trim();
  if (actor) {
    parsed.actor = actor;
  }
  const windowHours = String(params.get("window_hours") || "").trim();
  if (EXIT_EXECUTION_HISTORY_WINDOW_FILTERS.has(windowHours)) {
    parsed.windowHours = windowHours;
  }
  const offset = Number(params.get("offset") || "");
  if (Number.isFinite(offset) && offset >= 0) {
    parsed.offset = offset;
  }
  const limit = Number(params.get("limit") || "");
  if (Number.isFinite(limit) && limit > 0) {
    parsed.limit = limit;
  }
  return parsed;
}

function hydrateViewStateFromLocation(view = state.activeView) {
  if (view !== "exitExecution") {
    return;
  }
  const parsed = readExitExecutionHistoryStateFromLocation();
  state.ui.exitExecution = state.ui.exitExecution || {};
  state.ui.exitExecution.exitExecutionHistory = parsed;
  const riskState = ensureExitExecutionHistoryState("risk");
  copyExitExecutionHistoryFilters(parsed, riskState);
  riskState.offset = 0;
}

function buildExitExecutionViewPath() {
  const historyState = ensureExitExecutionHistoryState("exitExecution");
  const params = new URLSearchParams({
    offset: String(Math.max(Number(historyState.offset) || 0, 0)),
    limit: String(Math.max(Number(historyState.limit) || DEFAULT_EXIT_EXECUTION_HISTORY_PAGING.exitExecution.limit, 1)),
  });
  const parentIntentId = String(historyState.parent || "").trim();
  const actor = String(historyState.actor || "").trim();
  const action = String(historyState.action || "").trim();
  const windowHours = String(historyState.windowHours || "").trim();
  if (parentIntentId) {
    params.set("parent_intent_id", parentIntentId);
  }
  if (actor) {
    params.set("actor", actor);
  }
  if (action && action !== "all") {
    params.set("action", action);
  }
  if (windowHours && windowHours !== "all") {
    params.set("window_hours", windowHours);
  }
  return `${VIEW_ROUTES.exitExecution}?${params.toString()}`;
}

function buildViewPath(view = state.activeView) {
  if (view === "exitExecution") {
    return buildExitExecutionViewPath();
  }
  return VIEW_ROUTES[view] || VIEW_ROUTES.home;
}

function syncActiveViewLocationState({ pushHistory = false } = {}) {
  const targetPath = buildViewPath(state.activeView);
  const currentPath = `${window.location.pathname}${window.location.search}`;
  if (currentPath === targetPath) {
    return;
  }
  if (pushHistory) {
    window.history.pushState({ view: state.activeView }, "", targetPath);
    return;
  }
  window.history.replaceState({ view: state.activeView }, "", targetPath);
}

function syncExitExecutionNavigationLinks() {
  const exitExecutionHref = buildViewPath("exitExecution");
  viewLinks
    .filter((link) => link.dataset.view === "exitExecution")
    .forEach((link) => {
      if (link.getAttribute("href") !== exitExecutionHref) {
        link.setAttribute("href", exitExecutionHref);
      }
    });
}

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
  nodes.resumeButton?.addEventListener("click", () => void triggerResume(nodes.resumeButton));
  nodes.haltButton?.addEventListener("click", () => void triggerHalt(nodes.haltButton));
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
      void createOperatorUser();
    }
  });

  document.addEventListener("input", handleExitExecutionHistoryFilterEvent);
  document.addEventListener("change", handleExitExecutionHistoryFilterEvent);
}

async function refreshDashboard({ manual = false } = {}) {
  if (!manual && document.visibilityState !== "visible") {
    cancelScheduledRefresh();
    return;
  }
  if (state.actionInFlight && !manual) {
    state.pendingRefresh = true;
    return;
  }
  if (state.refreshing) {
    state.pendingRefresh = true;
    if (manual) {
      state.flash = { tone: "info", message: "当前正在刷新，已排队一次新的刷新请求。" };
      renderBanners();
    }
    return;
  }
  const refreshingView = state.activeView;
  const refreshPlan = buildDashboardBundleRequestPlan(refreshingView, state);
  const refreshGeneration = state.refreshGeneration + 1;
  let deferredRefreshStarted = false;
  state.refreshGeneration = refreshGeneration;
  setPendingPanels(refreshPlan.deferredPanels, Boolean(refreshPlan.deferredPath));
  cancelScheduledRefresh();
  state.refreshing = true;
  renderShell();
  try {
    const results = await fetchDashboardBundle(refreshPlan.primaryPath);
    if (state.refreshGeneration !== refreshGeneration) {
      return;
    }
    applyPanelResults(results);
    state.readyViews[refreshingView] = true;
    if (shouldRedirectToLogin()) {
      window.location.replace("/login");
      return;
    }
    state.lastRefreshAt = new Date();
    if (manual) {
      state.flash = { tone: "info", message: "页面数据已刷新。" };
    }
    if (refreshPlan.deferredPath) {
      deferredRefreshStarted = true;
      void refreshDeferredPanels({
        path: refreshPlan.deferredPath,
        panelKeys: refreshPlan.deferredPanels,
        refreshGeneration,
      });
    }
  } finally {
    if (!deferredRefreshStarted && state.refreshGeneration === refreshGeneration) {
      setPendingPanels(refreshPlan.deferredPanels, false);
    }
    state.refreshing = false;
    if (state.loadingView === refreshingView) {
      state.loadingView = null;
    }
    renderShell();
    if (state.pendingRefresh) {
      state.pendingRefresh = false;
      void refreshDashboard();
      return;
    }
    scheduleRefresh();
  }
}

async function refreshDeferredPanels({ path, panelKeys = [], refreshGeneration }) {
  try {
    const results = await fetchDashboardBundle(path);
    if (state.refreshGeneration !== refreshGeneration) {
      return;
    }
    applyPanelResults(results);
  } catch (error) {
    if (state.refreshGeneration !== refreshGeneration) {
      return;
    }
    panelKeys.forEach((key) => {
      state.errors[key] = error instanceof Error ? error.message : String(error);
    });
  } finally {
    if (state.refreshGeneration !== refreshGeneration) {
      return;
    }
    setPendingPanels(panelKeys, false);
    renderShell();
  }
}

function applyPanelResults(results) {
  for (const [key, result] of Object.entries(results || {})) {
    state.data[key] = result.data;
    state.errors[key] = result.error;
  }
}

function renderShell() {
  syncExitExecutionNavigationLinks();
  viewLinks.forEach((link) => link.classList.toggle("is-active", link.dataset.view === state.activeView));
  viewSections.forEach((section) => section.classList.toggle("is-active", section.dataset.view === state.activeView));
  renderPageChrome();
  renderSessionSummary();
  renderStatusRibbon();
  renderBanners();
  renderActiveView();
  renderRefreshIndicators();
  updateActionAccess();
  updateRefreshLabel();
  syncRefreshInteractivity();
}

function renderPageChrome() {
  const meta = VIEW_META[state.activeView] || VIEW_META.home;
  document.title = meta.docTitle;
  patchText(nodes.pageEyebrow, meta.eyebrow);
  patchText(nodes.pageHeading, meta.heading);
  patchText(nodes.pageCopy, meta.copy);
  patchClassName(nodes.pageHead, meta.hidePageHead ? "page-head is-hidden" : "page-head");
}

function renderSessionSummary() {
  const session = state.data.session || {};
  patchText(nodes.sessionIdentityValue, session.identity || "未登录");
  patchText(nodes.sessionRoleValue, `当前身份：${readableState(session.role || "anonymous")}`);
  patchClassName(nodes.authStateChip, `status-pill tone-${session.authenticated ? "positive" : "neutral"}`);
  patchText(nodes.authStateChip, session.authenticated ? "已登录" : "未登录");
}

function renderStatusRibbon() {
  if (state.activeView !== "home") {
    patchClassName(nodes.statusRibbon, "status-ribbon is-hidden");
    return;
  }

  if (shouldRenderLoadingState("home")) {
    patchClassName(nodes.statusRibbon, "status-ribbon status-ribbon--home");
    patchHtml(nodes.statusRibbon, renderHomeRibbonSkeleton());
    return;
  }

  const health = state.data.health || {};
  const mode = state.data.mode || {};
  const runtime = state.data.runtime || {};
  const recovery = state.data.systemRecovery?.recovery || {};
  const account = state.data.accountState || {};
  const reconciliation = state.data.reconciliationLatest?.reconciliation || null;
  const portfolio = state.data.portfolio?.portfolio || {};
  const blockerControl = state.data.blockerControl || {};
  const blockers = blockerControl.blockers || state.data.blockers?.blockers || [];
  const primaryBlocker = blockerControl.primary_blocker || blockers[0] || null;
  const latestDecision = state.data.latestDecision || {};
  const latestOrder = state.data.executionLatest?.latest_order || null;
  const metrics = state.data.metrics || {};

  if (!nodes.statusRibbon) return;
  patchClassName(nodes.statusRibbon, "status-ribbon status-ribbon--home");
  patchHtml(nodes.statusRibbon, [
    `<div class="status-ribbon__primary">${primaryStatusPanel({
      eyebrow: "主页状态总览",
      title: "",
      headline: homeRibbonHeadline({ health, recovery, blockers, reconciliation }),
      summary: "",
      tone: homeRibbonTone({ health, recovery, blockers, reconciliation }),
      pills: [
        pill(`运行状态 ${readableState(health.runtime_state || health.overall_status)}`, toneForRuntimeState(health.runtime_state || health.overall_status)),
        pill(`自动交易 ${tradingStatusLabel(recovery)}`, recovery.safe_to_trade ? "positive" : isPausedAwaitingResume(recovery) ? "warning" : "danger"),
        pill(`人工复核 ${reviewStatusLabel(recovery.review_required)}`, recovery.review_required ? "warning" : "outline"),
      ],
      metrics: [
        {
          label: "最近决策",
          value: latestDecision.decision_id ? readableFamilyExecutionSummary(latestDecision.position_target || {}, "保持当前仓位") : "暂无",
          meta: latestDecision.decision_id
            ? [
                formatMaybeTimestamp(latestDecision.decision_time || latestDecision.decision_context?.as_of_ts),
                readableOverlayParentSignalSummary(latestDecision.position_target || {}, ""),
              ].filter(Boolean).join(" | ")
            : formatMaybeTimestamp(latestDecision.decision_time || latestDecision.decision_context?.as_of_ts),
          tone: latestDecision.decision_id ? "info" : "neutral",
        },
        { label: "最新委托", value: readableState(latestOrder?.status || "unknown"), meta: middleEllipsis(latestOrder?.client_order_id, 10, 6, "暂未生成委托"), tone: toneForOrderStatus(latestOrder?.status) },
        { label: "恢复限制", value: isPausedAwaitingResume(recovery) ? "当前可手动恢复" : primaryBlocker ? (primaryBlocker.title || localizeError(primaryBlocker.blocker)) : recovery.safe_to_trade ? "当前无硬阻断" : localizedRecoveryReasons(), meta: middleEllipsis(reconciliation?.reconciliation_id, 10, 6, "恢复与对账共同决定交易资格"), tone: isPausedAwaitingResume(recovery) ? "warning" : blockers.length > 0 || reconciliation?.halt_required ? "danger" : recovery.safe_to_trade ? "positive" : "warning" },
        { label: "账户权益", value: formatNumber(portfolio.total_equity), meta: `活动委托 ${formatNumber(metrics.current_open_order_count)}`, tone: "info" },
      ],
    })}</div>`,
  ].join(""));
}

function renderHomeRibbonSkeleton() {
  return [
    `<div class="status-ribbon__primary">
      <section class="primary-status-panel skeleton-surface skeleton-panel" aria-hidden="true">
        <div class="skeleton-stack">
          <span class="skeleton-line skeleton-line--kicker"></span>
          <span class="skeleton-line skeleton-line--title"></span>
          <span class="skeleton-line skeleton-line--headline"></span>
          <span class="skeleton-line skeleton-line--body"></span>
        </div>
        <div class="skeleton-inline">
          ${Array.from({ length: 3 }, () => '<span class="skeleton-pill"></span>').join("")}
        </div>
        ${loadingTileGrid(3)}
      </section>
    </div>`,
  ].join("");
}

function homeRibbonHeadline({ health, recovery, blockers, reconciliation }) {
  return operationalStatusHeadline({ health, recovery, blockers, reconciliation });
}

function homeRibbonTone({ health, recovery, blockers, reconciliation }) {
  if (health.halted || blockers.length > 0 || reconciliation?.halt_required) return "danger";
  if (!recovery.safe_to_trade || recovery.review_required) return "warning";
  return "positive";
}

function renderBanners() {
  const banners = [];
  const recovery = state.data.systemRecovery?.recovery || {};
  const blockerControl = state.data.blockerControl || {};
  const blockers = blockerControl.blockers || state.data.blockers?.blockers || [];
  const primaryBlocker = blockerControl.primary_blocker || blockers[0] || null;
  const controlsMessage = controlPermissionMessage();

  if (!nodes.bannerContainer) return;
  if (isBootstrapping()) {
    patchHtml(nodes.bannerContainer, "");
    return;
  }
  if (hasResolvedPanel("systemRecovery") && recovery.safe_to_trade === false) {
    if (isPausedAwaitingResume(recovery)) {
      banners.push(notice(operationalStatusCopy({ recovery }), "info"));
    } else {
      banners.push(
        notice(
          operationalStatusCopy({ recovery, recoveryReasonText: localizedRecoveryReasons() }),
          "warning"
        )
      );
    }
  }
  if (blockers.length > 0) {
    const headline = primaryBlocker?.title || localizeError(primaryBlocker?.blocker || blockers[0].blocker);
    const detail = primaryBlocker?.recommended_next_step || localizeError(primaryBlocker?.blocker || blockers[0].blocker);
    banners.push(notice(`当前主要阻断原因：${headline}。${detail}`, (primaryBlocker || blockers[0]).affects_execution ? "danger" : "warning"));
  }
  if (controlsMessage) {
    banners.push(notice(controlsMessage, "info"));
  }
  if (state.flash) {
    banners.push(notice(state.flash.message, state.flash.tone));
    state.flash = null;
  }
  patchHtml(nodes.bannerContainer, banners.join(""));
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

function updateActionAccess() {
  const actionButtons = [nodes.resumeButton, nodes.haltButton];
  if (!hasResolvedAuthContext()) {
    actionButtons.forEach((node) => {
      if (!node) return;
      node.disabled = true;
      node.title = "正在确认当前账号权限。";
    });
    if (nodes.logoutButton) {
      nodes.logoutButton.disabled = false;
      nodes.logoutButton.title = "";
    }
    patchText(nodes.actionPermissionHint, "正在确认当前账号权限…");
    return;
  }

  if (state.actionInFlight) {
    [nodes.refreshButton, ...actionButtons].forEach((node) => {
      if (!node) return;
      node.disabled = true;
      node.title = "正在提交人工控制请求，请等待本次操作完成。";
    });
    if (nodes.logoutButton) {
      nodes.logoutButton.disabled = false;
      nodes.logoutButton.title = "";
    }
    patchText(nodes.actionPermissionHint, "正在提交人工控制请求，请等待当前操作完成。");
    return;
  }

  const canWrite = operatorCanWrite();
  const buttons = [nodes.refreshButton, ...actionButtons, nodes.logoutButton];
  const disabledReason = controlPermissionMessage() || "当前账号没有人工控制权限。";
  buttons.forEach((node) => {
    if (!node) return;
    const isWriteAction = node !== nodes.logoutButton && node !== nodes.refreshButton;
    if (node === nodes.resumeButton) {
      node.disabled = !canWrite || !resumeActionAvailable();
      node.title = !canWrite ? disabledReason : resumeActionHintText();
      return;
    }
    node.disabled = isWriteAction ? !canWrite : false;
    if (isWriteAction) {
      node.title = !canWrite ? disabledReason : "";
    } else if (node === nodes.refreshButton) {
      node.title = "";
    }
  });
  patchText(nodes.actionPermissionHint, canWrite ? "当前账号可以执行人工控制。" : disabledReason);
}

function updateRefreshLabel() {
  if (state.refreshing) {
    patchClassName(nodes.refreshStateChip, "status-pill tone-info refresh-state-chip is-loading");
    patchText(nodes.refreshStateChip, "刷新中");
    if (nodes.refreshStateChip) {
      nodes.refreshStateChip.setAttribute("aria-label", "正在刷新页面数据");
    }
    if (nodes.refreshButton) {
      nodes.refreshButton.disabled = true;
    }
    patchText(nodes.lastRefreshLabel, "正在刷新最新状态…");
    return;
  }
  if (!state.lastRefreshAt) {
    patchClassName(nodes.refreshStateChip, "status-pill tone-neutral refresh-state-chip");
    patchText(nodes.refreshStateChip, "待刷新");
    if (nodes.refreshStateChip) {
      nodes.refreshStateChip.setAttribute("aria-label", "页面尚未完成首次刷新");
    }
    if (nodes.refreshButton) {
      nodes.refreshButton.disabled = false;
    }
    patchText(nodes.lastRefreshLabel, "尚未刷新");
    return;
  }
  patchClassName(nodes.refreshStateChip, "status-pill tone-positive refresh-state-chip");
  patchText(nodes.refreshStateChip, "已同步");
  if (nodes.refreshStateChip) {
    nodes.refreshStateChip.setAttribute("aria-label", "页面数据已同步");
  }
  if (nodes.refreshButton) {
    nodes.refreshButton.disabled = false;
  }
  patchText(nodes.lastRefreshLabel, `最近刷新：${formatMaybeTimestamp(state.lastRefreshAt)}（${formatRelativeAge(state.lastRefreshAt)}）`);
}

function syncRefreshInteractivity() {
  syncRefreshDisabledButtons({
    roots: currentRefreshInteractivityRoots(),
    refreshing: state.refreshing,
    reason: "当前区域正在刷新，请等待刷新完成后再操作。",
  });
}

function currentRefreshInteractivityRoots() {
  const activeSection = viewSections.find((section) => section.dataset.view === state.activeView) || null;
  const openDrawer = nodes.detailDrawer?.classList.contains("is-open") ? nodes.detailDrawer : null;
  return [activeSection, openDrawer].filter(Boolean);
}

function setActiveView(view, { pushHistory = false, refresh = true } = {}) {
  const nextView = VIEW_ROUTES[view] ? view : "home";
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
    void refreshDashboard();
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

async function triggerReconciliationValidate(target = null) {
  await runAction("/reconciliation/validate", { reason: "ui_manual_validate" }, "已提交人工对账请求。", {
    target,
    pendingLabel: "正在重新对账…",
  });
}

async function triggerRebaseline(target = null) {
  await runDangerousAction({
    path: "/system/rebaseline",
    body: { reason: "ui_manual_rebaseline" },
    successMessage: "已把当前账户状态接受为新基线。",
    confirmMessage: "确认把当前账户、仓位和挂单状态接受为新的人工基线吗？这会覆盖旧的恢复参照。",
    target,
    pendingLabel: "正在重设基线…",
  });
}

async function triggerResume(target = null) {
  await runAction("/system/resume", { reason: "ui_manual_resume" }, "已提交恢复自动运行请求。", {
    target,
    pendingLabel: "正在恢复自动运行…",
  });
}

async function triggerHalt(target = null) {
  await runDangerousAction({
    path: "/system/halt",
    body: { reason: "ui_manual_halt" },
    successMessage: "系统已暂停自动运行。",
    confirmMessage: "确认立即暂停自动运行吗？",
    target,
    pendingLabel: "正在暂停自动运行…",
  });
}

async function recordScalingReview(verdict, target = null) {
  if (!verdict) return;
  const payloadMap = {
    approve_scale_up: {
      reason: "ui_scaling_review_approve_scale_up",
      successMessage: "已记录允许放量的人工评审结论。",
      pendingLabel: "正在记录放量评审…",
      confirmMessage: "确认记录“允许放量”评审结论吗？这表示系统已满足进入下一档资金评审的条件。",
    },
    continue_small_capital: {
      reason: "ui_scaling_review_continue_small_capital",
      successMessage: "已记录继续小资金试盘的人工评审结论。",
      pendingLabel: "正在记录评审结论…",
      confirmMessage: "",
    },
    shrink_trial: {
      reason: "ui_scaling_review_shrink_trial",
      successMessage: "已记录建议缩容试盘的人工评审结论。",
      pendingLabel: "正在记录缩容评审…",
      confirmMessage: "确认记录“建议缩容试盘”评审结论吗？",
    },
    pause_trial: {
      reason: "ui_scaling_review_pause_trial",
      successMessage: "已记录建议暂停试盘的人工评审结论。",
      pendingLabel: "正在记录暂停评审…",
      confirmMessage: "确认记录“建议暂停试盘”评审结论吗？",
    },
  };
  const payload = payloadMap[verdict];
  if (!payload) return;
  if (payload.confirmMessage && !window.confirm(payload.confirmMessage)) return;
  await runAction(
    "/system/scaling-review",
    {
      verdict,
      reason: payload.reason,
    },
    payload.successMessage,
    {
      target,
      pendingLabel: payload.pendingLabel,
    }
  );
}

async function recordTrialReview(target = null) {
  await runAction(
    "/system/trial-review/record",
    {
      reason: "ui_trial_review_snapshot",
    },
    "已记录本次试盘复盘摘要。",
    {
      target,
      pendingLabel: "正在记录复盘摘要…",
    }
  );
}

async function recordTrialReviewAction(actionType, target = null) {
  if (!actionType) return;
  const payloadMap = {
    review_snapshot: {
      reason: "ui_trial_review_snapshot",
      successMessage: "已记录本次试盘复盘摘要。",
      pendingLabel: "正在记录复盘摘要…",
      confirmMessage: "",
    },
    reset_trial_guard: {
      reason: "ui_trial_guard_manual_reset",
      successMessage: "已重置试盘守护，新的试盘样本窗口会从本次操作后重新开始。",
      pendingLabel: "正在重置试盘守护…",
      confirmMessage: "确认人工重置试盘守护吗？这会清空当前试盘守护的历史观察窗口，但系统仍会保持暂停，后续还需要你手动恢复自动运行。",
    },
    continue_small_capital: {
      reason: "ui_trial_review_continue_small_capital",
      successMessage: "已记录继续小资金试盘的处理结论。",
      pendingLabel: "正在记录处理结论…",
      confirmMessage: "",
    },
    shrink_trial: {
      reason: "ui_trial_review_shrink_trial",
      successMessage: "已记录缩小试盘规模的处理结论。",
      pendingLabel: "正在记录缩容结论…",
      confirmMessage: "确认记录“缩小试盘规模”处理结论吗？",
    },
    pause_trial: {
      reason: "ui_trial_review_pause_trial",
      successMessage: "已记录暂停试盘并复盘的处理结论。",
      pendingLabel: "正在记录暂停结论…",
      confirmMessage: "确认记录“暂停试盘并复盘”处理结论吗？",
    },
    approve_scale_up: {
      reason: "ui_trial_review_approve_scale_up",
      successMessage: "已记录进入下一档资金评审的处理结论。",
      pendingLabel: "正在记录放量评审…",
      confirmMessage: "确认记录“进入下一档资金评审”处理结论吗？",
    },
  };
  const payload = payloadMap[actionType];
  if (!payload) return;
  if (payload.confirmMessage && !window.confirm(payload.confirmMessage)) return;
  await runAction(
    "/system/trial-review/action",
    {
      action_type: actionType,
      reason: payload.reason,
    },
    payload.successMessage,
    {
      target,
      pendingLabel: payload.pendingLabel,
    }
  );
}

function normalizeExitExecutionParentIntentId(value) {
  const normalized = String(value || "").trim();
  return normalized || null;
}

async function triggerExitExecutionRefresh(value, target = null) {
  const parentIntentId = normalizeExitExecutionParentIntentId(value);
  await runExitExecutionAction({
    path: "/system/exit-execution/refresh",
    body: {
      reason: "ui_refresh_exit_execution_state",
      parent_intent_id: parentIntentId,
    },
    successMessage: "已提交退出任务状态刷新请求。",
    target,
    pendingLabel: "正在刷新退出任务状态…",
  });
}

async function triggerExitExecutionRetryLimitLookup(value, target = null) {
  const parentIntentId = normalizeExitExecutionParentIntentId(value);
  await runExitExecutionAction({
    path: "/system/exit-execution/retry-limit-lookup",
    body: {
      reason: "ui_retry_exit_execution_limit_lookup",
      parent_intent_id: parentIntentId,
    },
    successMessage: "已提交退出任务拆单上限重试请求。",
    target,
    pendingLabel: "正在重试拆单上限查询…",
  });
}

async function triggerExitExecutionSafeCancel(value, target = null) {
  const parentIntentId = normalizeExitExecutionParentIntentId(value);
  await runExitExecutionAction({
    path: "/system/exit-execution/safe-cancel",
    body: {
      reason: "ui_safe_cancel_exit_execution",
      parent_intent_id: parentIntentId,
    },
    successMessage: "已提交退出任务安全取消请求。",
    target,
    pendingLabel: "正在安全取消退出任务…",
    confirmMessage: "确认停止这条退出任务，并撤销当前仍可取消的子订单吗？",
  });
}

async function runExitExecutionAction({
  path,
  body,
  successMessage,
  target = null,
  pendingLabel = "正在提交请求…",
  confirmMessage = "",
} = {}) {
  const clearPending = setActionPending(target, pendingLabel);
  try {
    if (confirmMessage && !window.confirm(confirmMessage)) return;
    const result = await requestJson(path, { method: "POST", body });
    state.flash = {
      tone: "info",
      message: exitExecutionActionFlashMessage(result, successMessage),
    };
    await refreshDashboard({ manual: true });
  } catch (error) {
    state.flash = { tone: "danger", message: error instanceof Error ? error.message : String(error) };
    renderBanners();
  } finally {
    clearPending();
  }
}

function exitExecutionActionFlashMessage(result, fallback = "操作已提交。") {
  const base = textOrFallback(result?.message, fallback);
  const blocker = result?.details?.current_blocker_after_action;
  if (!blocker || typeof blocker !== "object") {
    return base;
  }
  const summary = textOrFallback(
    blocker.summary,
    localizeError(blocker.code, "当前还有未解除的退出任务阻断。")
  );
  return `${base} 当前仍卡在：${summary}`;
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
    const nextView = VIEW_ROUTES[value] ? value : "home";
    if (state.activeView === nextView) {
      state.flash = { tone: "info", message: `当前已在${VIEW_LABELS[nextView] || "当前页面"}，已刷新当前状态。` };
      renderBanners();
      window.scrollTo({ top: 0, behavior: "smooth" });
      return refreshDashboard({ manual: true });
    }
    return setActiveView(nextView, { pushHistory: true });
  }
  if (action === "inspect-decision") return inspectDecision(value);
  if (action === "inspect-order") return inspectOrder(value);
  if (action === "inspect-fill") return inspectFill(value);
  if (action === "inspect-reconciliation") return inspectReconciliation(value);
  if (action === "inspect-shadow") return inspectPhase1Shadow();
  if (action === "trigger-reconciliation-validate") return triggerReconciliationValidate(target);
  if (action === "trigger-rebaseline") return triggerRebaseline(target);
  if (action === "trigger-resume") return triggerResume(target);
  if (action === "trigger-halt") return triggerHalt(target);
  if (action === "trigger-exit-execution-refresh") return triggerExitExecutionRefresh(value, target);
  if (action === "trigger-exit-execution-retry-limit-lookup") return triggerExitExecutionRetryLimitLookup(value, target);
  if (action === "trigger-exit-execution-safe-cancel") return triggerExitExecutionSafeCancel(value, target);
  if (action === "apply-exit-execution-history-workspace") return applyExitExecutionHistoryWorkspaceFilters(target);
  if (action === "reset-exit-execution-history-workspace") return resetExitExecutionHistoryWorkspaceFilters(target);
  if (action === "paginate-exit-execution-history") return paginateExitExecutionHistory(value, target);
  if (action === "record-scaling-review") return recordScalingReview(value, target);
  if (action === "record-trial-review") return recordTrialReview(target);
  if (action === "record-trial-review-action") return recordTrialReviewAction(value, target);
  if (action === "trigger-blocker-action") return triggerBlockerAction(value, target);
  if (action === "resolve-stuck-order") return resolveStuckOrder(value);
  if (action === "select-ai-operating-mode") return selectAIOperatingMode(value, target);
  if (action === "manual-activate-strategy-profile") return activateStrategyProfile(value, target);
  if (action === "restore-strategy-profile-auto") return restoreStrategyProfileAutomaticControl(target);
  if (action === "pause-strategy-profile-auto") return pauseStrategyProfileAutomaticControl(target);
  if (action === "set-profile-control-mode") return setStrategyProfileControlMode(value, target);
  if (action === "load-more-orders") return adjustPageLimit("recentOrders", PAGE_LOAD_STEP);
  if (action === "collapse-orders") return resetPageLimit("recentOrders");
  if (action === "load-more-fills") return adjustPageLimit("recentFills", PAGE_LOAD_STEP);
  if (action === "collapse-fills") return resetPageLimit("recentFills");
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
  if (action === "toggle-user") return toggleOperatorUser(value);
  if (action === "change-user-role") return updateOperatorUserRole(value);
  if (action === "reset-user-password") return resetOperatorPassword(value);
  if (action === "delete-user") return deleteOperatorUser(value);
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

async function inspectOrder(orderId) {
  if (!orderId) return;
  try {
    const detail = await requestJson(`/orders/${encodeURIComponent(orderId)}`);
    openDrawer(buildOrderDrawer(detail));
  } catch (error) {
    state.flash = { tone: "danger", message: error instanceof Error ? error.message : String(error) };
    renderBanners();
  }
}

async function inspectFill(fillId) {
  if (!fillId) return;
  try {
    const detail = await requestJson(`/fills/${encodeURIComponent(fillId)}`);
    openDrawer(buildFillDrawer(detail));
  } catch (error) {
    state.flash = { tone: "danger", message: error instanceof Error ? error.message : String(error) };
    renderBanners();
  }
}

async function inspectReconciliation(reconciliationId) {
  if (!reconciliationId) return;
  try {
    const detail = await requestJson(`/reconciliation/${encodeURIComponent(reconciliationId)}`);
    openDrawer(
      buildReconciliationDrawer(detail, {
        recovery: state.data.systemRecovery?.recovery || {},
        latestReconciliationId: state.data.reconciliationLatest?.reconciliation?.reconciliation_id || "",
        uiHints: {
          recoveryReasonsText: localizedRecoveryReasons(),
          controlPermissionMessage: controlPermissionMessage(),
        },
      })
    );
  } catch (error) {
    state.flash = { tone: "danger", message: error instanceof Error ? error.message : String(error) };
    renderBanners();
  }
}

async function inspectPhase1Shadow() {
  try {
    const [detail, history] = await Promise.all([
      requestJson("/system/shadow"),
      requestJson("/system/shadow/history?limit=12"),
    ]);
    openDrawer(
      buildPhase1ShadowDrawer(detail, {
        shadowBlocker: activePhase1ShadowBlocker(),
        uiHints: {
          controlPermissionMessage: controlPermissionMessage(),
        },
        history: history?.history || [],
      })
    );
  } catch (error) {
    state.flash = { tone: "danger", message: error instanceof Error ? error.message : String(error) };
    renderBanners();
  }
}

async function triggerBlockerAction(value, target = null) {
  if (!value) return;
  const [actionId, blocker] = String(value).split("::");
  if (!actionId) return;
  const confirmMessage = blockerActionConfirmMessage(actionId);
  if (confirmMessage && !window.confirm(confirmMessage)) return;
  const blockerControl = state.data.blockerControl || {};
  const reason = defaultBlockerActionReason(actionId);
  await runAction(
    `/system/blocker-actions/${encodeURIComponent(actionId)}`,
    {
      panel_version: blockerControl.panel_version || null,
      blocker: blocker || null,
      reason,
    },
    blockerActionSuccessMessage(actionId),
    {
      target,
      pendingLabel: blockerActionPendingLabel(actionId),
    }
  );
}


async function resolveStuckOrder(orderId) {
  if (!orderId) return;
  await runDangerousAction({
    path: `/orders/${encodeURIComponent(orderId)}/resolve-stuck-submission`,
    body: { reason: "ui_resolve_stuck_submission" },
    successMessage: "已提交卡单处理请求。",
    confirmMessage: "确认对这笔长时间卡住的委托执行人工恢复处理吗？",
  });
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

const VIEW_REPLAY_FILTERS = new Set(["all", "inventory_only", "target_only", "target_and_inventory"]);

function setReplayParentFilter(value) {
  state.ui.replay.parentFilter = VIEW_REPLAY_FILTERS.has(value) ? value : "all";
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

function normalizeExitExecutionHistoryFilterValue(value) {
  return String(value || "").trim().toLowerCase();
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

function exitExecutionHistoryWindowThresholdMs(value) {
  const normalized = normalizeExitExecutionHistoryFilterValue(value);
  if (!normalized || normalized === "all") {
    return null;
  }
  const hours = Number(normalized);
  if (!Number.isFinite(hours) || hours <= 0) {
    return null;
  }
  return Date.now() - (hours * 60 * 60 * 1000);
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

async function createOperatorUser() {
  const username = document.getElementById("operatorCreateUsername")?.value.trim();
  const password = document.getElementById("operatorCreatePassword")?.value;
  const role = document.getElementById("operatorCreateRole")?.value;
  const enabled = document.getElementById("operatorCreateEnabled")?.value === "true";
  if (!username || !password || !role) {
    state.flash = { tone: "warning", message: "请完整填写用户名、密码和角色后再创建账号。" };
    renderBanners();
    return;
  }
  try {
    await requestJson("/auth/users", { method: "POST", body: { username, password, role, enabled } });
    state.flash = { tone: "info", message: `已创建账号 ${username}。` };
    await refreshDashboard({ manual: true });
  } catch (error) {
    state.flash = { tone: "danger", message: error instanceof Error ? error.message : String(error) };
    renderBanners();
  }
}

async function toggleOperatorUser(username) {
  const user = findOperatorUser(username);
  if (!user) return;
  try {
    await requestJson(`/auth/users/${encodeURIComponent(username)}`, {
      method: "PATCH",
      body: { enabled: !user.enabled },
    });
    state.flash = { tone: "info", message: `${username} 已${user.enabled ? "停用" : "启用"}。` };
    await refreshDashboard({ manual: true });
  } catch (error) {
    state.flash = { tone: "danger", message: error instanceof Error ? error.message : String(error) };
    renderBanners();
  }
}

async function updateOperatorUserRole(username) {
  const user = findOperatorUser(username);
  if (!user) return;
  const nextRole = window.prompt("请输入新的角色：viewer / operator / admin", user.role || "viewer");
  if (!nextRole || nextRole === user.role) return;
  try {
    await requestJson(`/auth/users/${encodeURIComponent(username)}`, {
      method: "PATCH",
      body: { role: nextRole },
    });
    state.flash = { tone: "info", message: `${username} 的角色已更新为 ${nextRole}。` };
    await refreshDashboard({ manual: true });
  } catch (error) {
    state.flash = { tone: "danger", message: error instanceof Error ? error.message : String(error) };
    renderBanners();
  }
}

async function resetOperatorPassword(username) {
  const password = window.prompt(`请输入 ${username} 的新密码`);
  if (!password) return;
  try {
    await requestJson(`/auth/users/${encodeURIComponent(username)}`, {
      method: "PATCH",
      body: { password },
    });
    state.flash = { tone: "info", message: `${username} 的密码已重置。` };
    await refreshDashboard({ manual: true });
  } catch (error) {
    state.flash = { tone: "danger", message: error instanceof Error ? error.message : String(error) };
    renderBanners();
  }
}

async function deleteOperatorUser(username) {
  if (!window.confirm(`确认删除账号 ${username} 吗？`)) return;
  try {
    await requestJson(`/auth/users/${encodeURIComponent(username)}`, { method: "DELETE" });
    state.flash = { tone: "info", message: `${username} 已删除。` };
    await refreshDashboard({ manual: true });
  } catch (error) {
    state.flash = { tone: "danger", message: error instanceof Error ? error.message : String(error) };
    renderBanners();
  }
}

function findOperatorUser(username) {
  return (state.data.operatorUsers?.users || []).find((item) => item.username === username) || null;
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

function hasReadyView(view) {
  return Boolean(state.readyViews[view]);
}

function isBackgroundRefreshingView(view) {
  return Boolean(view) && state.refreshing && !shouldRenderLoadingState(view) && hasReadyView(view) && state.activeView === view;
}

function isBootstrapping() {
  return !state.lastRefreshAt && state.refreshing;
}

function shouldRenderLoadingState(view) {
  if (!view) return false;
  if (state.loadingView === view) return true;
  return isBootstrapping() && !hasReadyView(view);
}

function setPendingPanels(panelKeys = [], pending = true) {
  panelKeys.forEach((key) => {
    if (!key) return;
    if (pending) {
      state.pendingPanels[key] = true;
      return;
    }
    delete state.pendingPanels[key];
  });
}

function renderLoadingView() {
  const html = loadingMarkupForView(state.activeView);
  if (state.activeView === "overview" && nodes.overviewContent) {
    patchHtml(nodes.overviewContent, html);
    return;
  }
  if (state.activeView === "home" && nodes.homeContent) {
    patchHtml(nodes.homeContent, html);
    return;
  }
  if (state.activeView === "strategy" && nodes.strategyContent) {
    patchHtml(nodes.strategyContent, html);
    return;
  }
  if (state.activeView === "execution" && nodes.executionContent) {
    patchHtml(nodes.executionContent, html);
    return;
  }
  if (state.activeView === "risk" && nodes.riskContent) {
    patchHtml(nodes.riskContent, html);
    return;
  }
  if (state.activeView === "exitExecution" && nodes.exitExecutionContent) {
    patchHtml(nodes.exitExecutionContent, html);
    return;
  }
  if (state.activeView === "replay" && nodes.replayContent) {
    patchHtml(nodes.replayContent, html);
    return;
  }
  if (state.activeView === "aiAnalysis" && nodes.aiAnalysisContent) {
    patchHtml(nodes.aiAnalysisContent, html);
    return;
  }
  if (state.activeView === "aiConfig" && nodes.aiConfigContent) {
    patchHtml(nodes.aiConfigContent, html);
    return;
  }
  if (state.activeView === "admin" && nodes.adminContent) {
    patchHtml(nodes.adminContent, html);
  }
}

function renderRefreshIndicators() {
  const contentNodes = [
    ["home", nodes.homeContent],
    ["overview", nodes.overviewContent],
    ["strategy", nodes.strategyContent],
    ["execution", nodes.executionContent],
    ["risk", nodes.riskContent],
    ["exitExecution", nodes.exitExecutionContent],
    ["replay", nodes.replayContent],
    ["aiAnalysis", nodes.aiAnalysisContent],
    ["aiConfig", nodes.aiConfigContent],
    ["admin", nodes.adminContent],
  ];
  contentNodes.forEach(([view, node]) => {
    patchClassName(node, isBackgroundRefreshingView(view) ? "view-layout is-refreshing" : "view-layout");
  });
  if (nodes.statusRibbon) {
    nodes.statusRibbon.classList.toggle("is-refreshing", isBackgroundRefreshingView("home") && !shouldRenderLoadingState("home"));
  }
}

function loadingMarkupForView(view) {
  if (view === "home" || view === "overview") {
    return `
      <div class="panel-grid skeleton-grid" aria-hidden="true">
        <section class="primary-status-panel skeleton-surface skeleton-panel span-12">
          <div class="skeleton-stack">
            <span class="skeleton-line skeleton-line--kicker"></span>
            <span class="skeleton-line skeleton-line--title"></span>
            <span class="skeleton-line skeleton-line--headline"></span>
            <span class="skeleton-line skeleton-line--body"></span>
            <span class="skeleton-line skeleton-line--body-short"></span>
          </div>
          <div class="skeleton-inline">
            ${Array.from({ length: 3 }, () => '<span class="skeleton-pill"></span>').join("")}
          </div>
          ${loadingTileGrid(4)}
        </section>
        <section class="surface-card skeleton-surface skeleton-card span-4">
          <div class="skeleton-stack">
            <span class="skeleton-line skeleton-line--kicker"></span>
            <span class="skeleton-line skeleton-line--title"></span>
            <span class="skeleton-line skeleton-line--body"></span>
          </div>
          ${loadingList(3)}
        </section>
        <section class="surface-card skeleton-surface skeleton-card span-4">
          <div class="skeleton-stack">
            <span class="skeleton-line skeleton-line--kicker"></span>
            <span class="skeleton-line skeleton-line--title"></span>
            <span class="skeleton-line skeleton-line--body-short"></span>
          </div>
          ${loadingTileGrid(4)}
        </section>
        <section class="surface-card skeleton-surface skeleton-card span-4">
          <div class="skeleton-stack">
            <span class="skeleton-line skeleton-line--kicker"></span>
            <span class="skeleton-line skeleton-line--title"></span>
            <span class="skeleton-line skeleton-line--body-short"></span>
          </div>
          ${loadingTileGrid(4)}
        </section>
      </div>
    `;
  }

  if (view === "strategy" || view === "execution" || view === "risk" || view === "exitExecution" || view === "replay" || view === "aiAnalysis" || view === "aiConfig" || view === "admin") {
    return `
      <div class="panel-grid skeleton-grid" aria-hidden="true">
        <section class="surface-card hero-card skeleton-surface skeleton-card span-7">
          <div class="skeleton-stack">
            <span class="skeleton-line skeleton-line--kicker"></span>
            <span class="skeleton-line skeleton-line--title"></span>
            <span class="skeleton-line skeleton-line--headline"></span>
            <span class="skeleton-line skeleton-line--body"></span>
          </div>
          ${loadingTileGrid(4)}
        </section>
        <section class="surface-card skeleton-surface skeleton-card span-5">
          <div class="skeleton-stack">
            <span class="skeleton-line skeleton-line--kicker"></span>
            <span class="skeleton-line skeleton-line--title"></span>
            <span class="skeleton-line skeleton-line--body-short"></span>
          </div>
          ${loadingList(4)}
        </section>
        <section class="surface-card skeleton-surface skeleton-card span-12">
          <div class="skeleton-stack">
            <span class="skeleton-line skeleton-line--kicker"></span>
            <span class="skeleton-line skeleton-line--title"></span>
          </div>
          ${loadingList(4)}
        </section>
      </div>
    `;
  }

  return emptyState("正在刷新页面数据…");
}

function loadingTileGrid(count) {
  return `
    <div class="skeleton-tile-grid">
      ${Array.from({ length: count }, () => `
        <article class="skeleton-tile">
          <span class="skeleton-tile__label"></span>
          <span class="skeleton-line skeleton-tile__value"></span>
          <span class="skeleton-line skeleton-tile__meta"></span>
        </article>
      `).join("")}
    </div>
  `;
}

function loadingList(count) {
  return `
    <div class="skeleton-list">
      ${Array.from({ length: count }, () => `
        <article class="skeleton-row">
          <div class="skeleton-row__head">
            <span class="skeleton-row__title"></span>
            <span class="skeleton-row__badge"></span>
          </div>
          <span class="skeleton-row__value"></span>
          <span class="skeleton-row__value is-short"></span>
        </article>
      `).join("")}
    </div>
  `;
}

function resolveViewFromLocation() {
  const pathname = window.location.pathname.replace(/\/+$/, "") || "/ui";
  if (pathname === "/" || pathname === "/ui" || pathname === "/ui/home") return "home";
  if (pathname === "/ui/ai") return "aiAnalysis";
  if (pathname === "/ui/exit-execution") return "exitExecution";
  const match = Object.entries(VIEW_ROUTES).find(([, route]) => route === pathname);
  return match?.[0] || "home";
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

function scheduleRefresh() {
  cancelScheduledRefresh();
  if (state.actionInFlight) return;
  if (nodes.autoRefreshToggle && !nodes.autoRefreshToggle.checked) return;
  if (document.visibilityState !== "visible") return;
  state.refreshTimer = window.setTimeout(() => void refreshDashboard(), AUTO_REFRESH_MS);
}

function handleVisibilityChange() {
  if (document.visibilityState !== "visible") {
    cancelScheduledRefresh();
    return;
  }
  if (nodes.autoRefreshToggle && !nodes.autoRefreshToggle.checked) return;
  if (state.refreshing) {
    state.pendingRefresh = true;
    return;
  }
  void refreshDashboard();
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

function cancelScheduledRefresh() {
  if (!state.refreshTimer) return;
  window.clearTimeout(state.refreshTimer);
  state.refreshTimer = null;
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

function patchRenderedSections(sections, containerGetter, fallbackRenderer) {
  const entries = Object.entries(sections || {});
  const hasSectionNodes = entries.length > 0 && entries.every(([key]) => document.getElementById(key));
  if (!hasSectionNodes) {
    const container = containerGetter();
    if (container) {
      patchHtml(container, fallbackRenderer());
    }
    return;
  }
  entries.forEach(([key, html]) => {
    patchHtml(document.getElementById(key), html);
  });
}

function patchHtml(node, html) {
  if (!node) return;
  if (renderCache.get(node) === html) return;
  node.innerHTML = html;
  renderCache.set(node, html);
}

function patchText(node, text) {
  if (!node) return;
  if (node.textContent === text) return;
  node.textContent = text;
}

function patchClassName(node, className) {
  if (!node) return;
  if (node.className === className) return;
  node.className = className;
}

window.refreshDashboard = refreshDashboard;
