import { fetchPanels, requestJson } from "./modules/api-client.js";
import { actionButton, kvList, notice, pill, primaryStatusPanel, surfaceCard } from "./modules/components.js";
import {
  booleanWord,
  emptyState,
  escapeHtml,
  formatMaybeTimestamp,
  formatNumber,
  formatRelativeAge,
  formatSigned,
  listOrDash,
  middleEllipsis,
  rawJson,
} from "./modules/formatters.js";
import { AUTO_REFRESH_MS, CORE_SPECS, DEFAULT_PAGE_LIMITS, PAGE_LOAD_STEP, createState, viewSpecs } from "./modules/store.js";
import {
  localizeError,
  operationalStatusCopy,
  operationalStatusHeadline,
  readableState,
  reviewStatusLabel,
  toneForOrderStatus,
  toneForRuntimeState,
  tradingStatusLabel,
} from "./modules/terms.js";
import {
  decisionDrawerRows,
  fillDrawerRows,
  fillSceneSummary,
  orderDrawerRows,
  orderSceneSummary,
} from "./modules/trade-display.js";
import { renderAISections, renderAIView } from "./modules/views/ai-view.js";
import { renderAIConfigView } from "./modules/views/ai-config-view.js";
import { renderAdminView } from "./modules/views/admin-view.js";
import { renderExecutionSections, renderExecutionView } from "./modules/views/execution-view.js";
import { renderHomeView } from "./modules/views/home-view.js";
import { renderOverviewView } from "./modules/views/overview-view.js";
import {
  reconciliationActionCopy,
  renderReconciliationControls,
  renderRiskSections,
  renderRiskView,
} from "./modules/views/risk-view.js";
import { renderStrategySections, renderStrategyView } from "./modules/views/strategy-view.js";

const VIEW_ROUTES = {
  home: "/ui",
  overview: "/ui/overview",
  strategy: "/ui/strategy",
  execution: "/ui/execution",
  risk: "/ui/risk",
  ai: "/ui/ai",
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
  ai: {
    docTitle: "AATS 自动交易监控台 | AI 工作台",
    eyebrow: "AI 决策链路",
    heading: "AI 当前有效模式、决策门禁和影子回放",
    copy: "先看模式和最新决策链结果，再看影子回放是否真的优于基础策略。",
    hidePageHead: false,
  },
  aiConfig: {
    docTitle: "AATS 自动交易监控台 | AI 配置",
    eyebrow: "AI 配置",
    heading: "",
    copy: "",
    hidePageHead: true,
  },
  admin: {
    docTitle: "AATS 自动交易控制台 | 账户与权限",
    eyebrow: "控制面",
    heading: "账户与权限工作区",
    copy: "这里专门处理登录、角色、账号启停和控制台访问权限。",
    hidePageHead: false,
  },
};

const state = createState();
state.activeView = resolveViewFromLocation();
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
  reconcileButton: document.getElementById("reconcileButton"),
  rebaselineButton: document.getElementById("rebaselineButton"),
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
  aiContent: document.getElementById("aiContent"),
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
    setActiveView(resolveViewFromLocation(), { refresh: true });
  });

  window.addEventListener("pageshow", (event) => {
    if (!event.persisted) return;
    setActiveView(resolveViewFromLocation(), { refresh: true });
  });

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
}

async function refreshDashboard({ manual = false } = {}) {
  if (state.actionInFlight && !manual) {
    state.pendingRefresh = true;
    return;
  }
  if (state.refreshing) {
    state.pendingRefresh = true;
    return;
  }
  const refreshingView = state.activeView;
  cancelScheduledRefresh();
  state.refreshing = true;
  renderShell();
  try {
    const specs = dedupeSpecs([...CORE_SPECS, ...viewSpecs(refreshingView, state)]);
    const results = await fetchPanels(specs);
    for (const [key, result] of Object.entries(results)) {
      state.data[key] = result.data;
      state.errors[key] = result.error;
    }
    state.readyViews[refreshingView] = true;
    if (shouldRedirectToLogin()) {
      window.location.replace("/login");
      return;
    }
    state.lastRefreshAt = new Date();
    if (manual) {
      state.flash = { tone: "info", message: "页面数据已刷新。" };
    }
  } finally {
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

function renderShell() {
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
        { label: "最近决策", value: latestDecision.decision_id ? readableState(latestDecision.position_target?.position_intent || "hold") : "暂无", meta: formatMaybeTimestamp(latestDecision.decision_time || latestDecision.decision_context?.as_of_ts), tone: latestDecision.decision_id ? "info" : "neutral" },
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
    patchRenderedSections(renderRiskSections(viewData), () => nodes.riskContent, () => renderRiskView(viewData));
    return;
  }
  if (state.activeView === "ai") {
    patchRenderedSections(renderAISections(viewData), () => nodes.aiContent, () => renderAIView(viewData));
    return;
  }
  if (state.activeView === "aiConfig" && nodes.aiConfigContent) {
    patchHtml(
      nodes.aiConfigContent,
      renderAIConfigView({
        session: state.data.session || {},
        summary: state.data.aiConfigModel || {},
        error: state.errors.aiConfigModel || null,
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

function setActiveView(view, { pushHistory = false, refresh = true } = {}) {
  const nextView = VIEW_ROUTES[view] ? view : "home";
  const changed = state.activeView !== nextView;
  if (changed) {
    state.activeView = nextView;
    state.loadingView = state.readyViews[nextView] ? null : nextView;
  }
  if (pushHistory) {
    const targetPath = VIEW_ROUTES[nextView];
    if (window.location.pathname !== targetPath) {
      window.history.pushState({ view: nextView }, "", targetPath);
    }
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
    await requestJson(path, { method: "POST", body });
    state.flash = { tone: "info", message: successMessage };
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
  if (action === "navigate-view") return setActiveView(value || "home", { pushHistory: true });
  if (action === "inspect-decision") return inspectDecision(value);
  if (action === "inspect-order") return inspectOrder(value);
  if (action === "inspect-fill") return inspectFill(value);
  if (action === "inspect-reconciliation") return inspectReconciliation(value);
  if (action === "trigger-reconciliation-validate") return triggerReconciliationValidate(target);
  if (action === "trigger-rebaseline") return triggerRebaseline(target);
  if (action === "trigger-resume") return triggerResume(target);
  if (action === "trigger-halt") return triggerHalt(target);
  if (action === "trigger-blocker-action") return triggerBlockerAction(value, target);
  if (action === "resolve-stuck-order") return resolveStuckOrder(value);
  if (action === "manual-activate-strategy-profile") return activateStrategyProfile(value, target);
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
  if (action === "toggle-user") return toggleOperatorUser(value);
  if (action === "change-user-role") return updateOperatorUserRole(value);
  if (action === "reset-user-password") return resetOperatorPassword(value);
  if (action === "delete-user") return deleteOperatorUser(value);
}

async function inspectDecision(decisionId) {
  if (!decisionId) return;
  try {
    const detail = await requestJson(`/decision/${encodeURIComponent(decisionId)}`);
    const aiEconomic = detail.ai_economic_actionability || null;
    const aiDecisionAudit = detail.ai_decision_audit || null;
    const aiExecutionSuggestion = detail.ai_execution_suggestion || null;
    const decisionOutcome = detail.decision_outcome || null;
    const aiEconomicRows = aiEconomic
      ? [
          ["AI 经济可执行性", booleanWord(aiEconomic.economically_actionable), `最低净边际 ${formatNumber(aiEconomic.min_required_net_edge_bps ?? 0, 2)} 个基点`],
          ["本轮评估边际", `${formatNumber(aiEconomic.estimated_edge_bps ?? 0, 2)} 个基点`, `成本 ${formatNumber(aiEconomic.estimated_cost_bps ?? 0, 2)} / 净边际 ${formatNumber(aiEconomic.estimated_net_edge_bps ?? 0, 2)} 个基点`],
          ["目标边际估计", `${formatNumber(aiEconomic.target_expected_signal_edge_bps ?? 0, 2)} 个基点`, `成本 ${formatNumber(aiEconomic.target_expected_cost_bps ?? 0, 2)} / 净边际 ${formatNumber(aiEconomic.target_expected_net_edge_bps ?? 0, 2)} 个基点`],
          ["总门槛", `${formatNumber(aiEconomic.required_total_edge_bps ?? 0, 2)} 个基点`, `噪声缓冲 ${formatNumber(aiEconomic.noise_buffer_bps ?? 0, 2)} / 最低净边际 ${formatNumber(aiEconomic.min_required_net_edge_bps ?? 0, 2)} 个基点`],
          ["决策来源", readableState(decisionOutcome?.decision_source || "baseline"), drawerListText((decisionOutcome?.decision_blocked_reasons || []).map(localizeError), "当前没有额外决策链路阻断项")],
          ["新鲜度与安全", `市场快照 ${booleanWord(aiEconomic.market_snapshot_fresh)} / 账户快照 ${booleanWord(aiEconomic.account_snapshot_fresh)}`, `允许交易 ${booleanWord(aiEconomic.safe_to_trade)} / ${drawerText(aiEconomic.execution_condition, "当前没有额外执行条件")}`],
          ["近期执行健康", `手续费拖累 ${formatNumber(aiEconomic.recent_fee_drag_ratio ?? 0, 3)} / 来回交易 ${formatNumber(aiEconomic.recent_churn_ratio ?? 0, 3)}`, `低边际连续次数 ${formatNumber(aiEconomic.recent_low_edge_trade_streak ?? 0, 0)} / 活动委托 ${formatNumber(aiEconomic.current_open_order_count ?? 0, 0)}`],
          ["校验标记", drawerListText(aiEconomic.validation_flags, "当前没有额外校验标记"), drawerListText(aiEconomic.rejection_flags, "当前没有额外拒绝标记")],
        ]
      : [];
    const aiAuditRows = aiDecisionAudit
      ? [
          ["配置与评估模式", `${readableState(aiDecisionAudit.configured_mode || "unknown")} / ${readableState(aiDecisionAudit.assessment_operating_mode || "unknown")}`, drawerText(aiDecisionAudit.provider_name, "当前没有模型服务说明")],
          ["方向链", `基础策略 ${readableState(aiDecisionAudit.baseline_direction || "unknown")} / AI ${readableState(aiDecisionAudit.ai_direction || "unknown")}`, `最终结论 ${readableState(aiDecisionAudit.final_direction || "unknown")}`],
          ["决策来源", readableState(aiDecisionAudit.decision_source || "baseline"), readableState(aiDecisionAudit.decision_authority || "reference_only")],
          ["决策链路阻断与保护", drawerListText((decisionOutcome?.decision_blocked_reasons || []).map(localizeError), "当前没有决策链路阻断项"), drawerListText(aiDecisionAudit.guardrail_flags, "当前没有额外保护规则")],
          ["新鲜度与安全", `市场快照 ${booleanWord(aiDecisionAudit.market_snapshot_fresh)} / 账户快照 ${booleanWord(aiDecisionAudit.account_snapshot_fresh)}`, `允许交易 ${booleanWord(aiDecisionAudit.safe_to_trade)} / ${drawerText(aiDecisionAudit.execution_condition, "当前没有额外执行条件")}`],
          ["近期执行健康", `手续费拖累 ${formatNumber(aiDecisionAudit.recent_fee_drag_ratio ?? 0, 3)} / 来回交易 ${formatNumber(aiDecisionAudit.recent_churn_ratio ?? 0, 3)}`, `低边际连续次数 ${formatNumber(aiDecisionAudit.recent_low_edge_trade_streak ?? 0, 0)} / 活动委托 ${formatNumber(aiDecisionAudit.current_open_order_count ?? 0, 0)}`],
        ]
      : [];
    const aiExecutionRows = aiExecutionSuggestion
      ? [
          ["建议模式", readableState(aiExecutionSuggestion.configured_mode || "disabled"), aiExecutionSuggestion.translation_present ? "已有翻译结果" : aiExecutionSuggestion.suggestion_present ? "已有建议结果" : "最近没有生成建议"],
          ["翻译器状态", readableState(aiExecutionSuggestion.status || "absent"), aiExecutionSuggestion.latest_translation?.applied_to_live_execution ? "已进入真实执行" : "当前不会改写真实执行"],
          ["实盘应用", booleanWord(aiExecutionSuggestion.latest_translation?.applied_to_live_execution), aiExecutionSuggestion.latest_translation?.applied_to_live_execution ? drawerListText(aiExecutionSuggestion.latest_translation?.applied_live_fields, "当前没有额外实盘字段") : drawerListText([aiExecutionSuggestion.latest_translation?.live_translation_fallback_reason], "当前没有进入实盘应用")],
          ["建议姿态", drawerListText(Object.entries(aiExecutionSuggestion.assessment_suggestion?.suggestion || {}).filter(([, value]) => value !== null && value !== undefined).map(([key, value]) => `${key}=${value}`), "当前没有额外建议姿态"), drawerListText(aiExecutionSuggestion.latest_translation?.clipped_fields, "当前没有裁剪字段")],
          ["翻译预览", drawerListText(Object.entries(aiExecutionSuggestion.latest_translation?.translation_preview || {}).filter(([, value]) => value !== null && value !== undefined).map(([key, value]) => `${key}=${value}`), "当前没有翻译预览"), drawerListText(aiExecutionSuggestion.latest_translation?.rejection_reasons, "当前没有拒绝原因")],
          ["实盘字段", `订单类型=${drawerText(aiExecutionSuggestion.live_order_type, "待确认")} / 时效=${drawerText(aiExecutionSuggestion.live_time_in_force, "待确认")} / 限价=${formatNumber(aiExecutionSuggestion.live_limit_price ?? 0, 2)}`, drawerText(aiExecutionSuggestion.live_execution_style, "当前没有执行风格说明")],
        ]
      : [];
    openDrawer({
      eyebrow: "决策链详情",
      title: detail.decision_id ? `当前记录：${detail.decision_id}` : "当前记录：决策链详情",
      summary: strategySummary(detail),
      body: [
        surfaceCard({
          title: "决策摘要",
          content: kvList(decisionDrawerRows(detail, describeDecisionIntent)),
        }),
        surfaceCard({
          title: "交易解释",
          content: `<div class="callout"><p>${escapeHtml(strategySummary(detail))}</p></div>`,
        }),
        aiEconomic
          ? surfaceCard({
              title: "AI 经济可行动性",
              content: kvList(aiEconomicRows),
            })
          : "",
        aiDecisionAudit
          ? surfaceCard({
              title: "AI 决策审计链",
              content: kvList(aiAuditRows),
            })
          : "",
        aiExecutionSuggestion
          ? surfaceCard({
              title: "AI 受限执行建议",
              content: kvList(aiExecutionRows),
            })
          : "",
        surfaceCard({
          title: "原始记录",
          content: rawJson(detail),
        }),
      ].join(""),
    });
  } catch (error) {
    state.flash = { tone: "danger", message: error instanceof Error ? error.message : String(error) };
    renderBanners();
  }
}

async function inspectOrder(orderId) {
  if (!orderId) return;
  try {
    const detail = await requestJson(`/orders/${encodeURIComponent(orderId)}`);
    const order = detail.order || {};
    const fills = detail.fills || [];
    openDrawer({
      eyebrow: `${orderSceneSummary(order)}详情`,
      title: order.client_order_id ? `当前记录：${order.client_order_id}` : `当前记录：${orderSceneSummary(order)}详情`,
      summary: `这笔${orderSceneSummary(order)}当前状态：${readableState(order.status)}。${fills.length ? ` 已关联 ${fills.length} 笔成交。` : " 目前还没有关联成交。"} `,
      body: [
        surfaceCard({
          title: `${orderSceneSummary(order)}摘要`,
          content: kvList([
            ...orderDrawerRows(order),
            ["最后更新时间", formatMaybeTimestamp(order.last_update_ts || order.created_at), formatRelativeAge(order.last_update_ts || order.created_at)],
          ]),
        }),
        surfaceCard({
          title: "关联成交",
          content: fills.length
            ? kvList(
                fills.map((fill) => [
                  fill.fill_id || "成交编号待同步",
                  `${formatNumber(fill.fill_qty)} @ ${formatNumber(fill.fill_price)}`,
                  `${readableState(fill.side)} | 手续费 ${formatNumber(fill.fee_amount)} ${fill.fee_currency || ""}`,
                ])
              )
            : emptyState("这笔委托暂时还没有对应成交。"),
        }),
        surfaceCard({
          title: "原始记录",
          content: rawJson(detail),
        }),
      ].join(""),
    });
  } catch (error) {
    state.flash = { tone: "danger", message: error instanceof Error ? error.message : String(error) };
    renderBanners();
  }
}

async function inspectFill(fillId) {
  if (!fillId) return;
  try {
    const detail = await requestJson(`/fills/${encodeURIComponent(fillId)}`);
    const fill = detail.fill || {};
    openDrawer({
      eyebrow: `${fillSceneSummary(fill)}详情`,
      title: fill.fill_id ? `当前记录：${fill.fill_id}` : `当前记录：${fillSceneSummary(fill)}详情`,
      summary: `这笔${fillSceneSummary(fill)}是 ${readableState(fill.side)} ${formatNumber(fill.fill_qty)}，成交价 ${formatNumber(fill.fill_price)}。`,
      body: [
        surfaceCard({
          title: `${fillSceneSummary(fill)}摘要`,
          content: kvList([
            ...fillDrawerRows(fill),
            ["落库时间", formatMaybeTimestamp(fill.ingestion_timestamp), formatRelativeAge(fill.ingestion_timestamp)],
          ]),
        }),
        surfaceCard({
          title: "原始记录",
          content: rawJson(detail),
        }),
      ].join(""),
    });
  } catch (error) {
    state.flash = { tone: "danger", message: error instanceof Error ? error.message : String(error) };
    renderBanners();
  }
}

async function inspectReconciliation(reconciliationId) {
  if (!reconciliationId) return;
  try {
    const detail = await requestJson(`/reconciliation/${encodeURIComponent(reconciliationId)}`);
    const reconciliation = detail.reconciliation || {};
    const billsSummary = detail.exchange_bills_summary || {};
    const billsExplanations = Array.isArray(detail.exchange_bills_explanations) ? detail.exchange_bills_explanations : [];
    const recovery = state.data.systemRecovery?.recovery || {};
    const latestReconciliationId = state.data.reconciliationLatest?.reconciliation?.reconciliation_id || "";
    const isHistorical = Boolean(latestReconciliationId) && latestReconciliationId !== reconciliation.reconciliation_id;
    const uiHints = {
      recoveryReasonsText: localizedRecoveryReasons(),
      controlPermissionMessage: controlPermissionMessage(),
    };
    openDrawer({
      eyebrow: "对账详情",
      title: reconciliation.reconciliation_id ? `当前记录：${reconciliation.reconciliation_id}` : "当前记录：对账详情",
      summary: `这次对账结论是 ${readableState(reconciliation.severity)}。${reconciliation.halt_required ? " 系统已要求暂停自动运行。" : ""}`,
      body: [
        surfaceCard({
          title: "核对摘要",
          content: kvList([
            ["核对级别", readableState(reconciliation.severity), reconciliation.exchange_comparison_enabled ? "已比对交易所" : "仅校验本地记录"],
            ["是否要求停机", booleanWord(reconciliation.halt_required), booleanWord(reconciliation.review_required)],
            ["差异原因", drawerListText(detail.mismatch_summary?.mismatch_reasons, "当前没有额外差异原因"), drawerListText(detail.mismatch_summary?.mismatch_categories, "当前没有额外差异分类")],
            ["建议动作", detail.mismatch_summary?.recommended_operator_action ? localizeError(detail.mismatch_summary.recommended_operator_action) : "当前没有额外建议动作", drawerListText(detail.mismatch_summary?.safety_impacts, "当前没有额外安全影响说明")],
            ["核对时间", formatMaybeTimestamp(reconciliation.as_of_ts), formatRelativeAge(reconciliation.as_of_ts)],
          ]),
        }),
        surfaceCard({
          title: "账单解释链",
          content: kvList([
            ["最近账单数量", formatNumber(billsSummary.count || 0), drawerText(billsSummary.latest_bill_id, "当前暂无最新账单编号")],
            ["涉及币种", drawerListText(billsSummary.currencies, "当前没有账单币种摘要"), "最近交易所侧账务变动范围"],
            ["高频账务类别", renderReconciliationBillsCategories(billsSummary.top_categories), "已按类型、子类型和币种聚合"],
            ["可能解释当前差异", renderReconciliationBillExplanations(billsExplanations), billsExplanations.length ? "这些账务事件更可能解释余额、仓位或执行偏差" : "当前没有明确的账单解释链"],
            ["识别类型", renderReconciliationBillCases(billsExplanations), billsExplanations.length ? "系统按账单语义和对账差异归纳出的处理场景" : "当前没有可归类的账单处理场景"],
            ["建议处理", renderReconciliationBillActions(billsExplanations), billsExplanations.length ? "这是给操作员的下一步动作建议，不会直接改动交易所账单" : "当前没有额外账单处理建议"],
          ]),
        }),
        surfaceCard({
          title: "可执行操作",
          content: `
            <p class="meta-copy">${escapeHtml(reconciliationActionCopy({ reconciliation, recovery, isHistorical }))}</p>
            ${renderReconciliationControls({ reconciliation, recovery, uiHints, compact: true })}
          `,
        }),
        surfaceCard({
          title: "原始记录",
          content: rawJson(detail),
        }),
      ].join(""),
    });
  } catch (error) {
    state.flash = { tone: "danger", message: error instanceof Error ? error.message : String(error) };
    renderBanners();
  }
}

function renderReconciliationBillsCategories(rows) {
  if (!Array.isArray(rows) || rows.length === 0) return "当前没有账单分类";
  return rows
    .slice(0, 4)
    .map((item) => `${item.human_label || `${item.type}/${item.sub_type}`}${item.count ? ` x${formatNumber(item.count)}` : ""}`)
    .join(" | ");
}

function renderReconciliationBillExplanations(rows) {
  if (!Array.isArray(rows) || rows.length === 0) return "当前没有账单解释";
  return rows
    .slice(0, 3)
    .map((item) => `${item.title}: ${drawerListText(item.likely_explains, "当前没有额外解释")}`)
    .join(" | ");
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

function renderReconciliationBillCases(rows) {
  if (!Array.isArray(rows) || rows.length === 0) return "当前没有账单处理分类";
  return rows
    .slice(0, 3)
    .map((item) => `${item.title}: ${localizeError(item.operator_case || "manual_activity")}`)
    .join(" | ");
}

function renderReconciliationBillActions(rows) {
  if (!Array.isArray(rows) || rows.length === 0) return "当前没有账单处理建议";
  return rows
    .slice(0, 3)
    .map((item) => `${item.title}: ${localizeError(item.operator_action || "observe_only")}`)
    .join(" | ");
}

function drawerText(value, fallback = "待确认") {
  if (value === null || value === undefined) return fallback;
  const text = String(value).trim();
  return text ? text : fallback;
}

function drawerListText(value, fallback = "当前没有额外说明") {
  if (Array.isArray(value)) {
    const filtered = value.map((item) => String(item ?? "").trim()).filter(Boolean);
    return filtered.length ? filtered.join(" / ") : fallback;
  }
  return drawerText(value, fallback);
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

function readableProfileName(value, fallback = "未知档位") {
  if (value === null || value === undefined || value === "") return fallback;
  return readableState(String(value), fallback);
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
  if (state.activeView === "ai" && nodes.aiContent) {
    patchHtml(nodes.aiContent, html);
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
    ["ai", nodes.aiContent],
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

  if (view === "strategy" || view === "execution" || view === "risk" || view === "ai" || view === "aiConfig" || view === "admin") {
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

function scheduleRefresh() {
  cancelScheduledRefresh();
  if (state.actionInFlight) return;
  if (nodes.autoRefreshToggle && !nodes.autoRefreshToggle.checked) return;
  state.refreshTimer = window.setTimeout(() => void refreshDashboard(), AUTO_REFRESH_MS);
}

function defaultBlockerActionReason(actionId) {
  const map = {
    "reconcile-now": "operator_validate_from_blocker_panel",
    "accept-rebaseline": "operator_rebaseline_from_blocker_panel",
    "resume-system": "operator_resume_from_blocker_panel",
    "halt-system": "operator_keep_halted_from_blocker_panel",
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
    "ai-review-restore": "AI 复核已处理，已恢复 AI 决策资格。",
    "ai-review-degrade-to-baseline": "AI 复核已处理，系统将以仅基础策略继续运行。",
  };
  return map[actionId] || "阻断处理动作已完成。";
}

function blockerActionConfirmMessage(actionId) {
  const map = {
    "accept-rebaseline": "确认把当前状态接受为新基线吗？这会覆盖旧的恢复参照。",
    "halt-system": "确认继续保持暂停状态吗？这会阻止系统继续自动交易。",
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

function dedupeSpecs(specs) {
  const seen = new Set();
  return specs.filter(([key]) => {
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
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

function strategySummary(detail) {
  const policy = detail.policy_decision || {};
  const risk = detail.risk_decision || {};
  if (!detail.decision_id) return "当前暂无新的策略详情。";
  return `系统当前对 ${detail.decision_context?.symbol || "当前标的"} 的交易结论是 ${describeDecisionIntent(detail)}。` +
    `${policy.execution_allowed ? "策略门禁已通过，" : "策略门禁未通过，"}` +
    `${risk.approved ? "风控也允许继续执行。" : `风控仍在拦截：${listOrDash(risk.rejection_reasons)}。`}`;
}

function describeDecisionIntent(detail) {
  const target = detail.position_target || {};
  const rawIntent = String(target.position_intent || "hold").toLowerCase();
  const currentQty = Number(target.current_position_qty ?? detail.decision_context?.current_position_qty ?? 0);
  const targetQty = Number(target.target_position_qty ?? 0);
  const openOrders = Array.isArray(detail.decision_context?.current_open_orders) ? detail.decision_context.current_open_orders : [];
  if (rawIntent === "hold" && currentQty === 0 && targetQty === 0 && openOrders.length === 0) {
    return "继续观望";
  }
  return readableState(rawIntent);
}

window.refreshDashboard = refreshDashboard;
